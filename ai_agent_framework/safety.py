"""
safety.py — Security checks, confirmation gates, and fail-safe mechanisms.

Protects Minetallest by:
  1. Confirmation Gate: high-risk actions require explicit y/N confirmation.
  2. Domain & Boundary Constraints: browser navigation is restricted to
     ALLOWED_DOMAINS when configured in config.py.
  3. Fail-Safe Mechanism: PyAutoGUI corner fail-safe + Ctrl+C handling, and
     an emergency "STOP" keyword check on every proposed action.
"""

import fnmatch
import re
from typing import Optional
from urllib.parse import urlparse

import config


class SafetyViolation(Exception):
    """Raised when an action is blocked by a safety boundary."""


# ---------------------------------------------------------------------------
# Fail-safe trigger detection
# ---------------------------------------------------------------------------

# Emergency keywords: if the LLM's thought text or task text contains these,
# the agent halts immediately.
EMERGENCY_PATTERNS = re.compile(
    r"\b(stop|abort|halt|cancel task|emergency)\b", re.IGNORECASE
)


def check_emergency_text(text: str) -> bool:
    """Return True if the text contains an explicit emergency-stop keyword.

    The LLM is instructed to place the literal token `EMERGENCY_STOP` in its
    thought_process field when it observes the user (or environment) requesting
    an immediate halt.
    """
    if "EMERGENCY_STOP" in text:
        return True
    # Do NOT treat ordinary words like "stop" in page content as emergencies;
    # only the explicit token or a task-level abort command triggers here.
    return False


def is_failsafe_triggered() -> bool:
    """Return True if PyAutoGUI's corner fail-safe has been tripped.

    PyAutoGUI raises pyautogui.FailSafeException automatically when the mouse
    reaches the top-left corner (if enabled in config). This helper lets the
    loop *poll* the mouse position so we can abort *before* an action fires.
    """
    try:
        import pyautogui

        if not config.PYAUTOGUI_FAILSAFE:
            return False
        x, y = pyautogui.position()
        return x <= 1 and y <= 1  # top-left corner region
    except Exception:
        # No display / pyautogui unavailable (e.g. headless server) — never
        # block execution because of a missing desktop environment.
        return False


# ---------------------------------------------------------------------------
# Confirmation gate
# ---------------------------------------------------------------------------

def is_high_risk(action: str, parameters: dict, task_text: str = "") -> bool:
    """Classify an action as high-risk and therefore requiring confirmation.

    High-risk categories (per spec):
      - executing terminal/shell commands
      - deleting files or closing main windows
      - submitting financial/personal forms (purchase, pay, transfer, submit)
    """
    target = str(parameters.get("selector", "")).lower()
    url = str(parameters.get("url", "")).lower()
    text = str(parameters.get("text", "")).lower()
    keys = "+".join(str(k) for k in parameters.get("key_combination", [])).lower()

    combined = f"{action} {target} {url} {text} {keys} {task_text}".lower()

    # 1. Explicit shell execution
    if action in ("execute", "shell", "terminal", "run_command"):
        return True

    # 2. Keyboard shortcuts that destroy content or close windows
    destructive_shortcuts = {"ctrl+w", "cmd+w", "alt+f4", "ctrl+shift+delete"}
    if action == "shortcut" and keys in destructive_shortcuts:
        return True

    # 3. Trigger keywords appearing in the target/text of the action
    for trigger in config.CONFIRMATION_TRIGGERS:
        if trigger in target or trigger in text:
            return True

    # 4. Navigation to anything that looks like finance/auth submission
    financial_markers = ("checkout", "payment", "billing", "confirm-order")
    if action == "navigate" and any(m in url for m in financial_markers):
        return True

    # 5. Any action whose own text contains a shell command pattern
    for kw in config.HIGH_RISK_KEYWORDS:
        if kw in combined:
            return True

    return False


def confirm_with_user(action: str, parameters: dict, agent_name: str = config.AGENT_NAME,
                      user_name: str = config.USER_NAME) -> bool:
    """Prompt Minetallest for explicit y/N confirmation in the terminal.

    Returns True to proceed. Any input other than y/yes (case-insensitive)
    aborts the individual action (but not the whole task).
    """
    print(f"\n[{agent_name}] ⚠  HIGH-RISK ACTION REQUIRES YOUR CONFIRMATION, {user_name}")
    print(f"     Action     : {action}")
    print(f"     Parameters : {parameters}")
    answer = input(f"     Proceed? (y/N): ").strip().lower()
    return answer in ("y", "yes")


# ---------------------------------------------------------------------------
# Domain & boundary constraints
# ---------------------------------------------------------------------------

def _normalize_domain(host: str) -> str:
    return host.strip().lower().removeprefix("www.")


def is_url_allowed(url: str) -> bool:
    """Check a URL against the configured ALLOWED_DOMAINS list.

    - If ALLOWED_DOMAINS is empty, everything is allowed (open mode).
    - Otherwise the URL's host must suffix-match one of the allowed domains.
      Glob-style patterns also work (e.g. '*.internal.company.com').
    """
    if not config.ALLOWED_DOMAINS:
        return True  # open mode — no domain restrictions configured

    try:
        host = _normalize_domain(urlparse(url).netloc.split(":")[0])
    except Exception:
        return False
    if not host:
        return False

    for allowed in config.ALLOWED_DOMAINS:
        allowed = _normalize_domain(allowed)
        if fnmatch.fnmatch(host, allowed):
            return True
        # Suffix match: "example.com" allows "sub.example.com"
        if host == allowed or host.endswith("." + allowed):
            return True
    return False


def validate_action(action: str, parameters: dict, task_text: str = "",
                    agent_name: str = config.AGENT_NAME,
                    user_name: str = config.USER_NAME) -> bool:
    """Full safety pipeline for a proposed action.

    Returns True if the action may proceed; False if Minetallest declined the
    confirmation gate. Raises SafetyViolation for hard blocks.
    """
    # Hard block: navigation outside allowed domains
    if action == "navigate":
        url = str(parameters.get("url", ""))
        if url and not is_url_allowed(url):
            raise SafetyViolation(
                f"Navigation to '{url}' is outside the allowed domains "
                f"{config.ALLOWED_DOMAINS}. Blocked by boundary constraints."
            )

    # Hard block: emergency tokens in the action text
    if check_emergency_text(str(parameters)):
        raise SafetyViolation("Emergency stop token detected — halting.")

    # Soft gate: high-risk actions need Minetallest's confirmation
    if is_high_risk(action, parameters, task_text):
        return confirm_with_user(action, parameters, agent_name, user_name)

    return True


# ---------------------------------------------------------------------------
# Ctrl+C / graceful interruption helpers
# ---------------------------------------------------------------------------

class GracefulInterrupt(Exception):
    """Raised when Minetallest interrupts the agent (Ctrl+C) mid-task."""


def describe_boundaries() -> str:
    """Human-readable summary of active safety boundaries for the CLI banner."""
    domains = ", ".join(config.ALLOWED_DOMAINS) if config.ALLOWED_DOMAINS else "ALL (open mode)"
    return (
        f"  Safety: confirmation gate ON | domains: {domains} | "
        f"fail-safe: {'corner+Ctrl+C' if config.PYAUTOGUI_FAILSAFE else 'Ctrl+C only'}"
    )
