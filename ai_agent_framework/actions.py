"""
actions.py — Execution primitives for web (Playwright) and desktop (PyAutoGUI).

Every primitive is defensive: it retries on transient failures, re-resolves
stale selectors against the current element index, and returns a structured
result dict so the agent loop can log and reason about outcomes.
"""

import time
from typing import Any, Dict, List, Optional

import config

# Playwright's sync API must run in the main thread; import lazily so the
# desktop-only mode works on machines without Playwright installed.
try:
    from playwright.sync_api import (
        Page,
        TimeoutError as PlaywrightTimeoutError,
        sync_playwright,
    )
    PLAYWRIGHT_AVAILABLE = True
except ImportError:
    PLAYWRIGHT_AVAILABLE = False


class ActionError(Exception):
    """Raised when an action cannot be completed after retries."""


def _log(message: str) -> None:
    """Uniform CLI logging prefix per spec: [Mine] ..."""
    print(f"[{config.AGENT_NAME}] {message}")


# ===========================================================================
# Web Driver (Playwright)
# ===========================================================================

class WebDriver:
    """Owns the Playwright browser context and exposes action primitives."""

    def __init__(self) -> None:
        if not PLAYWRIGHT_AVAILABLE:
            raise ActionError(
                "Playwright is not installed. Run: pip install playwright && playwright install chromium"
            )
        self._pw = sync_playwright().start()
        self._browser = self._pw.chromium.launch(
            headless=config.BROWSER_HEADLESS,
        )
        self._context = self._browser.new_context(
            user_agent=config.BROWSER_USER_AGENT,
            viewport={"width": config.VIEWPORT_WIDTH, "height": config.VIEWPORT_HEIGHT},
        )
        self.page: Page = self._context.new_page()
        self.page.set_default_timeout(config.BROWSER_DEFAULT_TIMEOUT_MS)

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        """Tear down browser and driver. Safe to call more than once."""
        try:
            self._context.close()
            self._browser.close()
            self._pw.stop()
        except Exception:
            pass

    # -- primitives -----------------------------------------------------------

    def navigate(self, url: str) -> Dict[str, Any]:
        """Navigate the active page to `url` and wait for it to settle."""
        _log(f"Executing navigate action for {config.USER_NAME} → {url}")
        try:
            self.page.goto(url, wait_until="domcontentloaded", timeout=config.BROWSER_DEFAULT_TIMEOUT_MS)
            # Give SPAs a beat to render after DOM-ready.
            self.page.wait_for_timeout(800)
            return {"ok": True, "url": self.page.url, "title": self.page.title()}
        except PlaywrightTimeoutError:
            return {"ok": False, "error": f"Navigation timed out after {config.BROWSER_DEFAULT_TIMEOUT_MS} ms"}

    def _resolve_selector(self, selector: str) -> Optional[str]:
        """Translate an indexed selector like `index=12` into a CSS selector
        using the current element index built by perception.py.

        The index map is stored on the page object after each observe() pass:
            self.page._mine_index_map = {12: "[data-mine-idx='12']", ...}
        """
        if not selector.startswith("index="):
            return selector  # already a CSS/XPath selector
        try:
            idx = int(selector.split("=", 1)[1])
        except ValueError:
            return None
        mapping = getattr(self.page, "_mine_index_map", {})
        return mapping.get(idx)

    def click_element(self, selector: str, retries: int = 3) -> Dict[str, Any]:
        """Click an element by CSS/XPath selector or by perception index.

        Retries handle the two common failure modes:
          - element not yet interactable (waits briefly between attempts)
          - stale index after DOM change (caller should re-observe)
        """
        _log(f"Executing click action for {config.USER_NAME} → {selector}")
        resolved = self._resolve_selector(selector)
        if not resolved:
            return {"ok": False, "error": f"Selector '{selector}' could not be resolved (stale index?)"}

        last_err: Optional[str] = None
        for attempt in range(1, retries + 1):
            try:
                self.page.locator(resolved).first.click(timeout=5000)
                return {"ok": True}
            except PlaywrightTimeoutError as e:
                last_err = str(e).splitlines()[0]
                time.sleep(0.6 * attempt)  # linear backoff
            except Exception as e:
                last_err = str(e)
                time.sleep(0.6 * attempt)
        return {"ok": False, "error": f"Click failed after {retries} attempts: {last_err}"}

    def type_text(self, selector: str, text: str, clear_first: bool = True) -> Dict[str, Any]:
        """Type text into an input/textarea. Clears existing content by default."""
        _log(f"Executing type action for {config.USER_NAME} → {selector}")
        resolved = self._resolve_selector(selector)
        if not resolved:
            return {"ok": False, "error": f"Selector '{selector}' could not be resolved"}

        try:
            loc = self.page.locator(resolved).first
            loc.wait_for(state="visible", timeout=5000)
            if clear_first:
                loc.fill("")  # clear without firing extra keystrokes
            loc.type(text, delay=25)  # human-like delay per keystroke
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Type failed: {str(e).splitlines()[0]}"}

    def scroll(self, direction: str = "down", amount_px: int = 600) -> Dict[str, Any]:
        """Scroll the page. direction: 'up' | 'down'."""
        _log(f"Executing scroll action ({direction} {amount_px}px) for {config.USER_NAME}")
        try:
            delta = -amount_px if direction.lower() == "up" else amount_px
            self.page.mouse.wheel(0, delta)
            self.page.wait_for_timeout(300)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Scroll failed: {e}"}

    def hover(self, selector: str) -> Dict[str, Any]:
        """Hover the mouse over an element (reveals dropdown menus etc.)."""
        _log(f"Executing hover action for {config.USER_NAME} → {selector}")
        resolved = self._resolve_selector(selector)
        if not resolved:
            return {"ok": False, "error": f"Selector '{selector}' could not be resolved"}
        try:
            self.page.locator(resolved).first.hover(timeout=5000)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Hover failed: {str(e).splitlines()[0]}"}

    def press_key(self, key_combination: List[str]) -> Dict[str, Any]:
        """Press a key combination, e.g. ['Control', 'c'] → Ctrl+C."""
        combo = "+".join(key_combination)
        _log(f"Executing shortcut action for {config.USER_NAME} → {combo}")
        try:
            self.page.keyboard.press(combo)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": f"Key press failed: {e}"}

    def capture_state(self) -> Dict[str, Any]:
        """Capture current page state: URL, title, and the pruned DOM tree text
        produced by perception.build_dom_index (stored on the page object)."""
        return {
            "url": self.page.url,
            "title": self.page.title(),
            "dom_tree": getattr(self.page, "_mine_dom_tree", "(not yet observed)"),
            "element_count": len(getattr(self.page, "_mine_index_map", {})),
        }

    def screenshot(self, path: str, full_page: bool = False) -> str:
        """Save a screenshot to `path` and return the path."""
        self.page.screenshot(path=path, full_page=full_page)
        return path

    # -- HTML extraction for perception --------------------------------------

    def get_page_html(self) -> str:
        """Return the rendered outer HTML of <body> for DOM indexing."""
        try:
            return self.page.evaluate("() => document.body ? document.body.outerHTML : ''")
        except Exception:
            return ""


# ===========================================================================
# Desktop Controller (PyAutoGUI / OS native)
# ===========================================================================

class DesktopController:
    """Screen-level control via PyAutoGUI. Used when tasks leave the browser."""

    def __init__(self) -> None:
        try:
            import pyautogui

            pyautogui.FAILSAFE = config.PYAUTOGUI_FAILSAFE
            pyautogui.PAUSE = config.PYAUTOGUI_PAUSE
            self.pyautogui = pyautogui
        except Exception as e:
            raise ActionError(f"PyAutoGUI unavailable (no display?): {e}")

    def screenshot(self, path: str) -> str:
        """Capture the whole screen to a PNG file."""
        img = self.pyautogui.screenshot()
        img.save(path)
        return path

    def move_mouse(self, x: int, y: int, duration: float = 0.3) -> Dict[str, Any]:
        _log(f"Executing move_mouse for {config.USER_NAME} → ({x}, {y})")
        try:
            self.pyautogui.moveTo(x, y, duration=duration)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def click_coordinates(self, x: int, y: int, button: str = "left", clicks: int = 1) -> Dict[str, Any]:
        _log(f"Executing click at ({x}, {y}) for {config.USER_NAME}")
        try:
            self.pyautogui.click(x=x, y=y, button=button, clicks=clicks, interval=0.1)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def type_keystrokes(self, text: str, interval: float = 0.03) -> Dict[str, Any]:
        _log(f"Executing type_keystrokes for {config.USER_NAME} ({len(text)} chars)")
        try:
            self.pyautogui.typewrite(text, interval=interval)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    def shortcut_keys(self, key_combination: List[str]) -> Dict[str, Any]:
        combo = "+".join(key_combination)
        _log(f"Executing shortcut_keys for {config.USER_NAME} → {combo}")
        try:
            # hotkey() accepts *keys, e.g. hotkey('ctrl', 'c')
            self.pyautogui.hotkey(*key_combination)
            return {"ok": True}
        except Exception as e:
            return {"ok": False, "error": str(e)}
