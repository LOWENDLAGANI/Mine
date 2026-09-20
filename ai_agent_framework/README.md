# Mine — AI Computer & Browser Automation Agent

Mine is a local computer- and browser-control agent that operates on behalf of
**Minetallest** through a perceive → decide → act → observe loop, driven by a
vision/LLM decision engine with a strict JSON action protocol.

## Features

- **Web automation** (Playwright/Chromium): navigate, click, type, scroll,
  hover, keyboard shortcuts, screenshots.
- **Desktop automation** (PyAutoGUI): mouse movement, coordinate clicks,
  keystrokes, hotkeys, full-screen captures.
- **Perception**: DOM selector indexer producing a compact numbered element
  list (`[12] Button: "Submit"`), plus annotated screenshots with numbered red
  bounding boxes for vision models.
- **LLM backends**: OpenAI, Anthropic, or any OpenAI-compatible local endpoint
  (Ollama / vLLM).
- **Safety**: confirmation gate for high-risk actions, domain allow-list,
  PyAutoGUI corner fail-safe, Ctrl+C graceful interruption, and
  session-state persistence to `mine_artifacts/session_state.json`.

## Setup

Requires Python 3.11+.

```bash
cd ai_agent_framework

# 1. (optional but recommended) create a virtual environment
python -m venv .venv
# Windows:
.venv\Scripts\activate
# macOS/Linux:
source .venv/bin/activate

# 2. install dependencies
pip install -r requirements.txt

# 3. install the Playwright browser (Chromium)
playwright install chromium
```

## Environment variables

Set the variables matching your chosen provider:

| Variable | Purpose | Default |
|---|---|---|
| `MINE_LLM_PROVIDER` | `openai` \| `anthropic` \| `local` | `openai` |
| `OPENAI_API_KEY` | OpenAI key (provider `openai`) | — |
| `ANTHROPIC_API_KEY` | Anthropic key (provider `anthropic`) | — |
| `MINE_LOCAL_LLM_BASE_URL` | OpenAI-compatible base URL (provider `local`) | `http://localhost:11434/v1` |
| `MINE_LOCAL_LLM_MODEL` | Local model name | `llama3.1` |
| `MINE_OPENAI_MODEL` / `MINE_ANTHROPIC_MODEL` | Model override | `gpt-4o` / `claude-sonnet-4-20250514` |
| `MINE_USE_VISION` | `1` to send screenshots to the model | `1` |
| `MINE_ALLOWED_DOMAINS` | Comma-separated domain allow-list (empty = open) | empty |
| `MINE_MAX_STEPS` | Step limit per task | `20` |
| `MINE_HEADLESS` | `1` for headless browser | `0` |
| `MINE_ARTIFACTS_DIR` | Output dir for screenshots/state | `./mine_artifacts` |

Examples:

```bash
# OpenAI
export OPENAI_API_KEY="sk-..."

# Local Ollama
export MINE_LLM_PROVIDER=local
# Ollama: ollama pull llama3.1 && ollama serve

# Restrict browsing to specific domains
export MINE_ALLOWED_DOMAINS="wikipedia.org,docs.python.org"
```

## Usage

```bash
# Interactive session
python main.py

# One-shot task
python main.py --task "Go to wikipedia.org and find the population of Japan"

# Desktop mode
python main.py --mode desktop --task "Open the calculator and compute 42*7"

# Headless + step limit
python main.py --headless --max-steps 10 --task "List the links on example.com"
```

## Safety model

- **Confirmation gate** — actions involving deletion, submission/purchase,
  shell execution, or window-closing shortcuts pause and ask Minetallest for
  explicit `y/N` confirmation in the terminal.
- **Domain boundaries** — with `MINE_ALLOWED_DOMAINS` set, navigation outside
  the listed domains is hard-blocked.
- **Fail-safes** — slam the mouse into the screen's top-left corner (PyAutoGUI
  fail-safe) or press Ctrl+C to halt immediately; state is persisted so runs
  are auditable in `mine_artifacts/`.

## Project layout

```
ai_agent_framework/
├── config.py         # Settings, API keys, persona identifiers
├── perception.py     # DOM indexer + annotated screenshot engine
├── actions.py        # Playwright & PyAutoGUI primitives
├── llm_client.py     # OpenAI / Anthropic / local adapter + JSON protocol
├── safety.py         # Confirmation gate, domain rules, fail-safes
├── agent_loop.py     # perceive → decide → act → observe orchestration
├── main.py           # CLI entrance point
└── requirements.txt
```
