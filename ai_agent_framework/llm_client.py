"""
llm_client.py — Structured JSON / Tool-Calling model adapter.

Supports three backends behind one interface:
  - OpenAI (api.openai.com, JSON mode + vision)
  - Anthropic (api.anthropic.com, tool-use + vision)
  - Any OpenAI-compatible local endpoint (Ollama / vLLM / llama.cpp)

The decision output always conforms to the JSON ACTION PROTOCOL defined in
the system prompt, validated before it reaches agent_loop.
"""

import json
import re
from typing import Any, Dict, List, Optional

import config

try:
    from openai import OpenAI
    OPENAI_SDK_AVAILABLE = True
except ImportError:
    OPENAI_SDK_AVAILABLE = False

try:
    import anthropic
    ANTHROPIC_SDK_AVAILABLE = True
except ImportError:
    ANTHROPIC_SDK_AVAILABLE = False


# ---------------------------------------------------------------------------
# JSON ACTION PROTOCOL schema (advertised to the model & used for validation)
# ---------------------------------------------------------------------------

ACTION_PROTOCOL_DESCRIPTION = """{
  "agent_name": "Mine",
  "user_name": "Minetallest",
  "thought_process": "Detailed reasoning about current UI state and the plan for Minetallest.",
  "actions": [
    {"action": "click",   "parameters": {"selector": "index=12"}},
    {"action": "type",    "parameters": {"selector": "index=7", "text": "hello"}},
    {"action": "navigate", "parameters": {"url": "https://example.com"}},
    {"action": "scroll",  "parameters": {"direction": "down", "amount_px": 600}},
    {"action": "shortcut", "parameters": {"key_combination": ["ctrl", "c"]}},
    {"action": "finish",  "parameters": {}, "is_final_step": true}
  ]
}"""

# NOTE on batching: the model may output MULTIPLE actions per response. They
# execute immediately in order WITHOUT new screenshots between them, so the
# model should only batch actions whose targets are certain from the CURRENT
# screen (e.g. click a search box, then type). If the next step depends on
# what the screen will show AFTER an action, end the batch there.

VALID_ACTIONS = {"click", "type", "navigate", "scroll", "hover", "shortcut", "finish", "error"}


class LLMProtocolError(Exception):
    """Raised when the model's output cannot be parsed into the protocol."""


# ---------------------------------------------------------------------------
# System prompt formulation
# ---------------------------------------------------------------------------

def build_system_prompt() -> str:
    """Dynamic system prompt embedding identity, protocol, and rules."""
    provider_line = (
        f"You are {config.AGENT_NAME}, an autonomous computer-use agent working exclusively "
        f"for your user, {config.USER_NAME}."
    )
    return f"""{provider_line}

## Mission
Complete {config.USER_NAME}'s task step by step by observing the screen/DOM state
and choosing exactly ONE action per turn. Prefer the smallest action that makes
progress. Do not repeat actions that already succeeded.

## Observation format
Each turn you receive:
  - URL & page title
  - A numbered element index, e.g. `[12] Button: "Submit"` — use `index=12` as
    the selector to interact with that element.
  - {"An annotated screenshot with numbered red boxes matching the element indices." if config.USE_VISION else "No screenshot (vision disabled)."}

## Output protocol (STRICT)
Respond with ONLY one JSON object, no markdown fences, no prose:
{ACTION_PROTOCOL_DESCRIPTION}

Rules:
  - "actions" is a LIST of 1-{config.MAX_ACTIONS_PER_DECISION} action objects, executed in order.
  - Batch only actions you are CERTAIN about from the current screen. If an
    action's outcome changes what the screen shows (new page, popup), end the
    batch there so you get a fresh screenshot.
  - "action" must be one of: {", ".join(sorted(VALID_ACTIONS))}.
  - Use a final {{"action": "finish", "is_final_step": true}} ONLY when the whole
    task is complete.
  - If the task is impossible or blocked, use "error" and explain in thought_process.
  - In thought_process, include the literal token EMERGENCY_STOP if {config.USER_NAME}
    or the environment demands an immediate halt.
  - For type actions, prefer clicking the field first, then type with the same
    selector — those two CAN be batched together.
  - For scroll, set parameters.direction ("up"/"down") and parameters.amount_px.
  - For shortcut, set parameters.key_combination, e.g. ["ctrl", "l"].

## Current task status
History and the latest observation are provided in the conversation.
"""


# ---------------------------------------------------------------------------
# Response parsing & validation
# ---------------------------------------------------------------------------

def _extract_json(text: str) -> Dict[str, Any]:
    """Pull the first JSON object out of a model response, tolerating
    markdown fences and leading prose (small local models often add both)."""
    text = text.strip()
    # Strip ```json ... ``` fences if present
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if fence:
        text = fence.group(1)
    else:
        # Otherwise take the outermost {...} block
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            raise LLMProtocolError(f"No JSON object found in response: {text[:200]!r}")
        text = text[start:end + 1]
    try:
        return json.loads(text)
    except json.JSONDecodeError as e:
        raise LLMProtocolError(f"Invalid JSON: {e} — raw: {text[:200]!r}")


def _validate_single(step: Dict[str, Any]) -> Dict[str, Any]:
    """Validate one action object against the protocol."""
    if not isinstance(step, dict):
        raise LLMProtocolError("Action is not a JSON object")

    action = str(step.get("action", "")).lower().strip()
    if action not in VALID_ACTIONS:
        raise LLMProtocolError(f"Unknown action '{action}'. Valid: {sorted(VALID_ACTIONS)}")

    params = step.get("parameters") or {}
    if not isinstance(params, dict):
        params = {}

    # Action-specific parameter sanity checks
    if action == "navigate" and not params.get("url"):
        raise LLMProtocolError("navigate requires parameters.url")
    if action in ("click", "type", "hover") and not (params.get("selector") or params.get("coordinates")):
        raise LLMProtocolError(f"{action} requires parameters.selector or parameters.coordinates")
    if action == "type" and "text" not in params:
        raise LLMProtocolError("type requires parameters.text")
    if action == "shortcut" and not params.get("key_combination"):
        raise LLMProtocolError("shortcut requires parameters.key_combination")

    step["action"] = action
    step["parameters"] = params
    step.setdefault("thought_process", "")
    step["is_final_step"] = bool(step.get("is_final_step", action == "finish"))
    return step


def validate_decision(decision: Dict[str, Any]) -> Dict[str, Any]:
    """Validate and normalize a model decision.

    Accepts two shapes for backward compatibility:
      - {"actions": [{...}, {...}]}  (batched, preferred)
      - {"action": ..., "parameters": ...}  (single, auto-wrapped)
    Returns {"actions": [validated steps...], "thought_process": str}.
    """
    if not isinstance(decision, dict):
        raise LLMProtocolError("Decision is not a JSON object")

    if "actions" in decision:
        steps = decision["actions"]
        if not isinstance(steps, list) or not steps:
            raise LLMProtocolError("'actions' must be a non-empty list")
    elif "action" in decision:
        steps = [decision]  # legacy single-action shape
    else:
        raise LLMProtocolError("Decision must contain 'actions' (list) or 'action'")

    if len(steps) > config.MAX_ACTIONS_PER_DECISION:
        steps = steps[:config.MAX_ACTIONS_PER_DECISION]  # trim, don't fail

    validated = [_validate_single(s) for s in steps]

    return {
        "agent_name": decision.get("agent_name", config.AGENT_NAME),
        "user_name": decision.get("user_name", config.USER_NAME),
        "thought_process": decision.get("thought_process", "(no reasoning provided)"),
        "actions": validated,
        "raw_response": decision,
    }


# ---------------------------------------------------------------------------
# Unified client
# ---------------------------------------------------------------------------

class LLMClient:
    """Single entry point: client.decide(history, state) → validated decision."""

    def __init__(self) -> None:
        self.provider = config.LLM_PROVIDER.lower()
        if self.provider in ("openai", "local"):
            if not OPENAI_SDK_AVAILABLE:
                raise LLMProtocolError("openai package not installed (pip install openai)")
            base_url = config.LOCAL_LLM_BASE_URL if self.provider == "local" else None
            # Use the real key when provided (proxies like FreeLLMAPI validate it);
            # fall back to a placeholder only for keyless servers like plain Ollama.
            if self.provider == "local":
                api_key = config.OPENAI_API_KEY or "ollama"
            else:
                api_key = config.OPENAI_API_KEY
            self.client = OpenAI(api_key=api_key, base_url=base_url, timeout=config.LLM_TIMEOUT_SECONDS)
            self.model = config.LOCAL_LLM_MODEL if self.provider == "local" else config.OPENAI_MODEL
        elif self.provider == "anthropic":
            if not ANTHROPIC_SDK_AVAILABLE:
                raise LLMProtocolError("anthropic package not installed (pip install anthropic)")
            self.client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
            self.model = config.ANTHROPIC_MODEL
        else:
            raise LLMProtocolError(f"Unknown LLM_PROVIDER '{self.provider}' (use openai|anthropic|local)")

    # -- message assembly -----------------------------------------------------

    def _vision_block(self, state: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Build a multimodal image block from the annotated screenshot."""
        b64 = state.get("screenshot_base64")
        if not (config.USE_VISION and b64):
            return None
        return {"type": "image_url", "image_url": {"url": b64}}

    def _user_message(self, task: str, state: Dict[str, Any], history: List[Dict[str, Any]],
                      step: int, max_steps: int) -> List[Dict[str, Any]]:
        """Assemble the per-turn user message: task, history digest, state."""
        history_digest = "\n".join(
            f"  step {h.get('step')}: {h.get('action')} {h.get('summary')} → {h.get('result')}"
            for h in history[-8:]  # keep context bounded
        ) or "  (none yet — this is the first action)"

        # Vision-only mode: the annotated screenshot carries the element
        # indices, so we skip the DOM text list to keep the prompt tiny.
        elements_section = ""
        if not config.VISION_ONLY:
            elements_section = f"\n## Interactive elements\n{state.get('elements')}\n"

        text = (
            f"## Task (from {config.USER_NAME})\n{task}\n\n"
            f"## Action history (most recent last)\n{history_digest}\n\n"
            f"## Current observation (step {step}/{max_steps})\n"
            f"URL: {state.get('url')}\n"
            f"Title: {state.get('title')}\n"
            f"{elements_section}\n"
            f"Decide the next batch of actions as JSON per the protocol."
        )

        content: List[Dict[str, Any]] = [{"type": "text", "text": text}]
        image_block = self._vision_block(state)
        if image_block:
            content.append(image_block)
        return content

    # -- provider calls ---------------------------------------------------------

    def _decide_openai(self, system: str, content: Any) -> str:
        """OpenAI chat completion. Vision works for both API and local servers
        that support image_url content parts."""
        messages = [{"role": "system", "content": system},
                    {"role": "user", "content": content}]
        resp = self.client.chat.completions.create(
            model=self.model,
            messages=messages,
            temperature=config.LLM_TEMPERATURE,
            max_tokens=config.LLM_MAX_TOKENS,
            # JSON mode where supported; local servers ignore it harmlessly
            response_format={"type": "json_object"},
        )
        return resp.choices[0].message.content or ""

    def _decide_anthropic(self, system: str, content: Any) -> str:
        """Anthropic messages API. Converts OpenAI-style image blocks to
        Anthropic source-format blocks."""
        blocks = []
        for part in content:
            if part["type"] == "text":
                blocks.append({"type": "text", "text": part["text"]})
            elif part["type"] == "image_url":
                url = part["image_url"]["url"]
                # data URL → base64 source block
                media_type, b64 = url.split(";", 1)[0], url.split("base64,", 1)[1]
                blocks.append({"type": "image", "source": {"type": "base64", "media_type": media_type, "data": b64}})
        resp = self.client.messages.create(
            model=self.model,
            system=system,
            max_tokens=config.LLM_MAX_TOKENS,
            messages=[{"role": "user", "content": blocks}],
        )
        return "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")

    # -- public API ---------------------------------------------------------------

    def decide(self, task: str, state: Dict[str, Any], history: List[Dict[str, Any]],
               step: int, max_steps: int) -> Dict[str, Any]:
        """Ask the model for the next action. Returns a validated decision dict.

        Retries once on protocol violations (models occasionally emit prose).
        """
        system = build_system_prompt()
        content = self._user_message(task, state, history, step, max_steps)

        last_err: Optional[str] = None
        for attempt in range(2):
            raw = self._decide_anthropic(system, content) if self.provider == "anthropic" \
                else self._decide_openai(system, content)
            try:
                decision = validate_decision(_extract_json(raw))
                decision["raw_response"] = raw
                return decision
            except LLMProtocolError as e:
                last_err = str(e)
                # Append a corrective message so the retry can fix itself
                content.append({"type": "text",
                                "text": f"Your previous reply was invalid: {last_err}. "
                                        f"Respond with ONLY the JSON object."})
        raise LLMProtocolError(f"Model failed to produce valid protocol JSON after retries: {last_err}")
