"""
perception.py — DOM Selector Indexer + Vision Snapshot Engine.

Turns a raw HTML page into a compact, token-efficient index the LLM can reason
over, and produces annotated screenshots with numbered tags over interactive
elements for multimodal decision-making.

Example index output:
    [12] Button: "Submit"
    [13] TextBox: "Email"
"""

import base64
import json
import os
import re
from typing import Any, Dict, List, Optional, Tuple

import config

# Elements treated as interactive for indexing purposes
INTERACTIVE_TAGS = {"a", "button", "input", "select", "textarea"}
INTERACTIVE_ROLES = {"button", "link", "checkbox", "radio", "tab", "menuitem", "textbox", "combobox", "switch"}

# Tags whose entire subtree is pruned (noise: hidden widgets, metadata, ads)
PRUNE_TAGS = {"script", "style", "noscript", "svg", "path", "iframe", "template", "link", "meta", "head"}

MAX_ELEMENT_LABEL_LEN = 60
MAX_TREE_TEXT_LEN = 12000  # token guard: keep the DOM summary bounded

try:
    from PIL import Image, ImageDraw, ImageFont
    PIL_AVAILABLE = True
except ImportError:
    PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# DOM parsing (stdlib html.parser — no external parser dependency)
# ---------------------------------------------------------------------------

from html.parser import HTMLParser


class _DomNode:
    """Lightweight DOM node used to build the pruned element tree."""

    __slots__ = ("tag", "attrs", "text", "children", "parent", "index")

    def __init__(self, tag: str, attrs: List[Tuple[str, Optional[str]]], parent: Optional["_DomNode"]):
        self.tag = tag.lower()
        # attrs values may be None for boolean attributes (e.g. <input required>)
        self.attrs: Dict[str, str] = {k: (v if v is not None else "") for k, v in attrs}
        self.text = ""
        self.children: List[_DomNode] = []
        self.parent = parent
        self.index: Optional[int] = None  # assigned when interactive


class _TreeBuilder(HTMLParser):
    """Builds a _DomNode tree while dropping pruned subtrees entirely."""

    VOID_TAGS = {"br", "hr", "img", "input", "meta", "link", "area", "base", "col", "embed", "source", "track", "wbr"}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _DomNode("#root", [], None)
        self.current = self.root
        self._prune_depth = 0  # >0 means we are inside a pruned subtree

    def handle_starttag(self, tag, attrs):
        if self._prune_depth > 0:
            if tag in PRUNE_TAGS:
                self._prune_depth += 1
            return
        if tag in PRUNE_TAGS:
            self._prune_depth = 1
            return
        node = _DomNode(tag, attrs, self.current)
        self.current.children.append(node)
        if tag not in self.VOID_TAGS:
            self.current = node

    def handle_endtag(self, tag):
        if self._prune_depth > 0:
            if tag in PRUNE_TAGS:
                self._prune_depth -= 1
            return
        # Walk back up to the matching open tag (tolerates malformed HTML)
        n = self.current
        while n is not None and n.tag != tag:
            n = n.parent
        if n is not None and n.parent is not None:
            self.current = n.parent

    def handle_data(self, data):
        if self._prune_depth == 0:
            # Collapse whitespace; keep meaningful inline text
            cleaned = re.sub(r"\s+", " ", data).strip()
            if cleaned:
                self.current.text = (self.current.text + " " + cleaned).strip()[:MAX_ELEMENT_LABEL_LEN]


def _is_interactive(node: _DomNode) -> bool:
    """Heuristics for whether a node is an actionable element."""
    if node.tag in INTERACTIVE_TAGS:
        # Skip hidden inputs & submit-type noise handled via buttons anyway
        if node.tag == "input" and node.attrs.get("type") in ("hidden",):
            return False
        # Elements that are visually invisible are skipped by CSS check later
        return True
    role = node.attrs.get("role", "").lower()
    if role in INTERACTIVE_ROLES:
        return True
    # Clickable JS-driven elements
    if "onclick" in node.attrs:
        return True
    return False


def _css_selector_for(node: _DomNode, idx: int) -> str:
    """Prefer the stable data-mine-idx attribute injected at runtime; fall back
    to a structural path if we're building purely from static HTML."""
    return f"[data-mine-idx='{idx}']"


def build_dom_index(page_html: str) -> Tuple[str, Dict[int, str], List[Dict[str, Any]]]:
    """Parse `page_html` and produce:

    1. tree_text — a compact textual element list for the LLM, e.g.
         [12] Button: "Submit"
         [13] TextBox: "Email" (value: a@b.com)
    2. index_map — {index: css_selector} for action resolution.
    3. element_boxes — [{index, tag, label}] for screenshot annotation.
    """
    builder = _TreeBuilder()
    try:
        builder.feed(page_html)
    except Exception:
        pass  # tolerate malformed HTML; use whatever tree we got

    tree_lines: List[str] = []
    index_map: Dict[int, str] = {}
    element_boxes: List[Dict[str, Any]] = []
    counter = 1

    def role_of(node: _DomNode) -> str:
        if node.tag == "a":
            return "Link"
        if node.tag == "button":
            return "Button"
        if node.tag == "input":
            t = node.attrs.get("type", "text")
            return {"text": "TextBox", "email": "TextBox", "password": "PasswordBox",
                    "checkbox": "CheckBox", "radio": "Radio", "submit": "Button",
                    "search": "SearchBox"}.get(t, f"Input[{t}]")
        if node.tag == "textarea":
            return "TextArea"
        if node.tag == "select":
            return "Dropdown"
        return node.attrs.get("role", node.tag).capitalize()

    def label_of(node: _DomNode) -> str:
        label = node.text or node.attrs.get("aria-label") or node.attrs.get("placeholder") \
            or node.attrs.get("title") or node.attrs.get("name") or node.attrs.get("value") or ""
        return label.strip()[:MAX_ELEMENT_LABEL_LEN]

    def walk(node: _DomNode) -> None:
        nonlocal counter
        for child in node.children:
            if _is_interactive(child):
                idx = counter
                counter += 1
                child.index = idx
                label = label_of(child)
                role = role_of(child)
                extra = ""
                if child.tag == "input" and child.attrs.get("value"):
                    extra = f" (value: {child.attrs['value'][:30]})"
                elif child.tag == "a" and child.attrs.get("href"):
                    extra = f" -> {child.attrs['href'][:60]}"
                tree_lines.append(f"[{idx}] {role}: \"{label}\"{extra}")
                index_map[idx] = _css_selector_for(child, idx)
                element_boxes.append({"index": idx, "tag": child.tag, "label": label})
            walk(child)

    walk(builder.root)

    tree_text = "\n".join(tree_lines)
    if len(tree_text) > MAX_TREE_TEXT_LEN:
        tree_text = tree_text[:MAX_TREE_TEXT_LEN] + f"\n… (truncated, {counter - 1} elements total)"

    return tree_text, index_map, element_boxes


# ---------------------------------------------------------------------------
# Runtime element tagging (injects data-mine-idx attributes into live DOM)
# ---------------------------------------------------------------------------

# JavaScript executed in the page: prunes invisible elements, assigns stable
# numeric indices, and reports bounding boxes back to Python in one pass.
_TAGGING_JS = """
() => {
  const selector = 'a, button, input, select, textarea, [role="button"], [role="link"], [role="checkbox"], [role="radio"], [role="tab"], [role="menuitem"], [role="textbox"], [role="combobox"], [onclick]';
  const elements = Array.from(document.querySelectorAll(selector));
  const visible = [];
  for (const el of elements) {
    if (el.type === 'hidden') continue;
    const style = window.getComputedStyle(el);
    if (style.display === 'none' || style.visibility === 'hidden' || parseFloat(style.opacity) === 0) continue;
    const rect = el.getBoundingClientRect();
    if (rect.width === 0 && rect.height === 0) continue;
    visible.push(el);
  }
  // Clear previous tags, then assign fresh sequential indices
  document.querySelectorAll('[data-mine-idx]').forEach(el => el.removeAttribute('data-mine-idx'));
  const boxes = [];
  visible.forEach((el, i) => {
    const idx = i + 1;
    el.setAttribute('data-mine-idx', String(idx));
    const rect = el.getBoundingClientRect();
    let label = (el.innerText || el.getAttribute('aria-label') || el.getAttribute('placeholder') || el.getAttribute('title') || el.getAttribute('name') || el.value || '').trim();
    boxes.push({
      index: idx,
      tag: el.tagName.toLowerCase(),
      label: label.slice(0, 60),
      x: Math.round(rect.x), y: Math.round(rect.y),
      w: Math.round(rect.width), h: Math.round(rect.height)
    });
  });
  return boxes;
}
"""


def tag_live_elements(page: Any) -> List[Dict[str, Any]]:
    """Inject data-mine-idx attributes into the live page and collect
    bounding-box metadata. `page` is a Playwright Page object."""
    try:
        boxes = page.evaluate(_TAGGING_JS)
        # Store the index→selector map on the page for actions._resolve_selector
        page._mine_index_map = {b["index"]: f"[data-mine-idx='{b['index']}']" for b in boxes}
        return boxes
    except Exception:
        page._mine_index_map = {}
        return []


# ---------------------------------------------------------------------------
# Vision snapshot engine
# ---------------------------------------------------------------------------

def annotate_screenshot(screenshot_path: str, boxes: List[Dict[str, Any]],
                        output_path: Optional[str] = None) -> str:
    """Draw numbered rectangle tags over interactive elements on a screenshot.

    Returns the path of the annotated image. Falls back to the original path
    if Pillow is unavailable or nothing needs annotating.
    """
    output_path = output_path or screenshot_path.replace(".png", "_annotated.png")
    if not PIL_AVAILABLE or not boxes:
        return screenshot_path
    if not os.path.exists(screenshot_path):
        return screenshot_path

    img = Image.open(screenshot_path).convert("RGB")
    draw = ImageDraw.Draw(img)
    try:
        font = ImageFont.load_default()
    except Exception:
        font = None

    for box in boxes:
        x, y, w, h = box["x"], box["y"], box["w"], box["h"]
        # Scale boxes if the screenshot was captured at device pixel ratio ≠ 1
        if img.width and box.get("viewport_width") and img.width != box["viewport_width"]:
            scale = img.width / box["viewport_width"]
            x, y, w, h = int(x * scale), int(y * scale), int(w * scale), int(h * scale)
        color = (255, 60, 60)  # red boxes stand out on most pages
        draw.rectangle([x, y, x + w, y + h], outline=color, width=2)
        # Tag badge above the box (clamped inside image bounds)
        tag_text = str(box["index"])
        ty = max(0, y - 14)
        draw.rectangle([x, ty, x + 14 + 6 * len(tag_text), ty + 14], fill=color)
        draw.text((x + 3, ty + 1), tag_text, fill="white", font=font)

    img.save(output_path)
    return output_path


def image_to_base64(path: str) -> str:
    """Encode an image file as a base64 data URL for vision-model input."""
    with open(path, "rb") as f:
        return "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")


def observe_web(driver: Any, screenshot_name: str = "screen.png") -> Dict[str, Any]:
    """Full perception pass over the current web page:

    1. Tag live DOM elements and gather their boxes.
    2. Build the compact textual element index.
    3. Capture and (optionally) annotate a screenshot.

    Returns a state dict consumed by agent_loop and llm_client.
    """
    os.makedirs(config.ARTIFACTS_DIR, exist_ok=True)
    shot_path = os.path.join(config.ARTIFACTS_DIR, screenshot_name)

    boxes = tag_live_elements(driver.page)
    tree_text, static_map, _ = build_dom_index(driver.get_page_html())
    # Prefer live-DOM indices (they match screenshot annotations exactly)
    tree_lines = [
        f"[{b['index']}] {b['tag'].capitalize()}: \"{b['label']}\""
        for b in boxes
    ]
    tree_text = "\n".join(tree_lines) if boxes else tree_text

    driver.page._mine_dom_tree = tree_text

    driver.screenshot(shot_path)
    annotated = annotate_screenshot(shot_path, boxes)

    state = {
        "url": driver.page.url,
        "title": driver.page.title(),
        "elements": tree_text or "(no interactive elements found)",
        "element_count": len(boxes),
        "screenshot": shot_path,
        "annotated_screenshot": annotated,
        "screenshot_base64": image_to_base64(annotated) if config.USE_VISION else None,
    }
    return state


def observe_desktop(desktop: Any, screenshot_name: str = "desktop.png") -> Dict[str, Any]:
    """Perception pass for desktop mode: screen-level screenshot only."""
    os.makedirs(config.ARTIFACTS_DIR, exist_ok=True)
    shot_path = os.path.join(config.ARTIFACTS_DIR, screenshot_name)
    desktop.screenshot(shot_path)
    return {
        "url": "(desktop)",
        "title": "(desktop)",
        "elements": "(desktop mode: use coordinates from screenshot)",
        "element_count": 0,
        "screenshot": shot_path,
        "annotated_screenshot": shot_path,
        "screenshot_base64": image_to_base64(shot_path) if config.USE_VISION else None,
    }
