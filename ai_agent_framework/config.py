"""
config.py — Central configuration for the Mine automation framework.

All tunable settings, persona identifiers, and environment-variable-driven
secrets live here so every other module imports from a single source of truth.
"""

import os
from dataclasses import dataclass, field
from typing import List, Optional

# ---------------------------------------------------------------------------
# Persona identifiers
# ---------------------------------------------------------------------------

AGENT_NAME = "Mine"
USER_NAME = "Minetallest"

# ---------------------------------------------------------------------------
# Environment / API keys
# ---------------------------------------------------------------------------

# Provider selection: "openai" | "anthropic" | "local"
LLM_PROVIDER = os.getenv("MINE_LLM_PROVIDER", "openai")

OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "")
ANTHROPIC_API_KEY = os.getenv("ANTHROPIC_API_KEY", "")

# OpenAI-compatible local endpoint (Ollama / vLLM / llama.cpp server, etc.)
LOCAL_LLM_BASE_URL = os.getenv("MINE_LOCAL_LLM_BASE_URL", "http://localhost:11434/v1")
LOCAL_LLM_MODEL = os.getenv("MINE_LOCAL_LLM_MODEL", "llama3.1")

# Model names per provider
OPENAI_MODEL = os.getenv("MINE_OPENAI_MODEL", "gpt-4o")
ANTHROPIC_MODEL = os.getenv("MINE_ANTHROPIC_MODEL", "claude-sonnet-4-20250514")

# Vision support: send screenshots to the model alongside DOM state
USE_VISION = os.getenv("MINE_USE_VISION", "1") not in ("0", "false", "False")

# Vision-only mode: send ONLY the screenshot (skip the DOM element list).
# Much smaller prompt; the model reads numbered tags from the annotated image.
VISION_ONLY = os.getenv("MINE_VISION_ONLY", "0") in ("1", "true", "True")

# Batched actions: how many LLM-visible "cycles" between fresh screenshots.
# After a screenshot, the model may output up to MAX_ACTIONS_PER_DECISION
# actions that execute back-to-back without a new screenshot in between.
OBSERVE_EVERY_N_STEPS = int(os.getenv("MINE_OBSERVE_EVERY_N", "1"))
MAX_ACTIONS_PER_DECISION = int(os.getenv("MINE_MAX_BATCH", "5"))

# LLM request settings
LLM_TEMPERATURE = float(os.getenv("MINE_LLM_TEMPERATURE", "0.0"))
LLM_MAX_TOKENS = int(os.getenv("MINE_LLM_MAX_TOKENS", "1024"))
LLM_TIMEOUT_SECONDS = int(os.getenv("MINE_LLM_TIMEOUT", "90"))

# ---------------------------------------------------------------------------
# Agent loop limits
# ---------------------------------------------------------------------------

# Hard cap on perceive-decide-act iterations per task (per spec: max 20)
MAX_STEPS_PER_TASK = int(os.getenv("MINE_MAX_STEPS", "20"))

# Seconds to wait after each action before re-observing (lets UI settle)
OBSERVE_DELAY_SECONDS = float(os.getenv("MINE_OBSERVE_DELAY", "1.0"))

# ---------------------------------------------------------------------------
# Browser (Playwright) settings
# ---------------------------------------------------------------------------

BROWSER_HEADLESS = os.getenv("MINE_HEADLESS", "0") in ("1", "true", "True")
BROWSER_USER_AGENT = os.getenv(
    "MINE_USER_AGENT",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36",
)
# Milliseconds Playwright waits for navigation / selectors
BROWSER_DEFAULT_TIMEOUT_MS = int(os.getenv("MINE_BROWSER_TIMEOUT", "15000"))

# Viewport used for screenshots and element indexing
VIEWPORT_WIDTH = int(os.getenv("MINE_VIEWPORT_WIDTH", "1280"))
VIEWPORT_HEIGHT = int(os.getenv("MINE_VIEWPORT_HEIGHT", "800"))

# ---------------------------------------------------------------------------
# Desktop (PyAutoGUI) settings
# ---------------------------------------------------------------------------

# Safety trigger: moving the mouse into the screen's top-left corner aborts
PYAUTOGUI_FAILSAFE = True
PYAUTOGUI_PAUSE = 0.25  # seconds between PyAutoGUI calls

# ---------------------------------------------------------------------------
# Safety / boundaries
# ---------------------------------------------------------------------------

# When non-empty, browser navigation is restricted to these domains
# (suffix match, e.g. "example.com" allows "sub.example.com").
# Comma-separated env var: MINE_ALLOWED_DOMAINS=example.com,bank.example
ALLOWED_DOMAINS: List[str] = [
    d.strip().lower()
    for d in os.getenv("MINE_ALLOWED_DOMAINS", "").split(",")
    if d.strip()
]

# Glob patterns of commands that always require explicit confirmation
HIGH_RISK_KEYWORDS: List[str] = [
    "rm ", "del ", "rmdir", "format ", "shutdown", "taskkill",
    "sudo", "reg delete", "remove-item",
]

# Keywords in task text or action targets that trigger the confirmation gate
CONFIRMATION_TRIGGERS: List[str] = [
    "delete", "remove", "submit", "purchase", "pay", "checkout",
    "transfer", "sign", "close", "terminate", "format", "send",
]

# Directory for screenshots, logs, and session state
ARTIFACTS_DIR = os.getenv("MINE_ARTIFACTS_DIR", os.path.join(os.getcwd(), "mine_artifacts"))

# ---------------------------------------------------------------------------
# Remote control bridge (phone dashboard <-> PC agent)
# ---------------------------------------------------------------------------
# Firebase Realtime Database URL, e.g. https://mine-agent-xxxx.firebaseio.com
# (regioned instances look like https://name-region1.firebasedatabase.app)
FIREBASE_DB_URL = os.getenv("MINE_FIREBASE_DB_URL", "").rstrip("/")

# Shared secret. The phone dashboard stores it in its URL; the PC bridge sends
# it with every write. Simple shared-secret auth is enough for personal use.
BRIDGE_TOKEN = os.getenv("MINE_BRIDGE_TOKEN", "")

# How often the bridge polls Firebase for new commands (seconds)
BRIDGE_POLL_SECONDS = float(os.getenv("MINE_BRIDGE_POLL", "2.0"))

# ---------------------------------------------------------------------------
# Logging
# ---------------------------------------------------------------------------

LOG_LEVEL = os.getenv("MINE_LOG_LEVEL", "INFO")


def banner() -> str:
    """Return the CLI startup banner for Mine."""
    return (
        f"\n{'=' * 62}\n"
        f"  {AGENT_NAME} — AI Computer & Browser Automation Agent\n"
        f"  Operating on behalf of: {USER_NAME}\n"
        f"  Provider: {LLM_PROVIDER} | Vision: {'on' if USE_VISION else 'off'}\n"
        f"  Max steps/task: {MAX_STEPS_PER_TASK} | Fail-safe: corner move or Ctrl+C\n"
        f"{'=' * 62}\n"
    )
