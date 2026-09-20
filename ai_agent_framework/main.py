"""
main.py — CLI entrance point for the Mine automation framework.

Usage examples (for Minetallest):

  # Interactive prompt loop
  python main.py

  # One-shot task
  python main.py --task "Search for weather in Tokyo and report the temperature"

  # Desktop mode (PyAutoGUI screen control instead of the browser)
  python main.py --mode desktop --task "Open Notepad and type hello"

  # Headless browser run
  python main.py --headless --task "Go to example.com and list the links"
"""

import argparse
import os
import subprocess
import sys

# Ensure the CLI can print Unicode (element labels etc.) on Windows cp1252 consoles
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")

import config
from agent_loop import AgentLoop
from safety import describe_boundaries


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        prog="Mine",
        description=f"{config.AGENT_NAME} — AI computer & browser automation agent for {config.USER_NAME}",
    )
    parser.add_argument("--task", "-t", type=str, default=None,
                        help="Task goal to execute. Omit to enter interactive mode.")
    parser.add_argument("--mode", "-m", choices=["web", "desktop"], default="web",
                        help="web = Playwright browser control; desktop = PyAutoGUI screen control")
    parser.add_argument("--headless", action="store_true",
                        help="Run the browser headless (web mode only)")
    parser.add_argument("--max-steps", type=int, default=config.MAX_STEPS_PER_TASK,
                        help=f"Step limit per task (default {config.MAX_STEPS_PER_TASK})")
    return parser.parse_args(argv)


def interactive_session(mode: str, headless: bool, max_steps: int) -> None:
    """Read goals from the terminal until Minetallest types exit/quit.

    Each task runs in a fresh subprocess: this isolates the Playwright driver
    so a Ctrl+C interruption (or any crash) can never poison the next task
    with a stuck asyncio event loop.
    """
    print(config.banner())
    print(describe_boundaries())
    print("Type your goal and press Enter. Commands: exit/quit to leave.\n")

    while True:
        try:
            goal = input(f"{config.USER_NAME} > ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n[Mine] Session ended by user. Goodbye!")
            break
        if goal.lower() in ("exit", "quit", "q"):
            print(f"[{config.AGENT_NAME}] Session closed. Until next time, {config.USER_NAME}!")
            break
        if not goal:
            continue

        # Build a child command that reproduces this session's settings
        cmd = [sys.executable, os.path.join(os.path.dirname(os.path.abspath(__file__)), "main.py"),
               "--task", goal, "--mode", mode, "--max-steps", str(max_steps)]
        if headless:
            cmd.append("--headless")
        try:
            # run_task.py resolves to main.py itself; the child executes the
            # one-shot branch and inherits env + stdio so logging looks identical.
            exit_code = subprocess.call(cmd)
        except KeyboardInterrupt:
            # Ctrl+C during a task: the child gets it too and cleans itself up
            print(f"\n[{config.AGENT_NAME}] Task interrupted. Ready for the next goal, {config.USER_NAME}.")
        print()


def main() -> int:
    args = parse_args()

    if args.task:
        print(config.banner())
        print(describe_boundaries())
        loop = AgentLoop(mode=args.mode, headless=args.headless)
        loop.max_steps = args.max_steps
        result = loop.run(args.task)
        return 0 if result["status"] == "completed" else 1

    interactive_session(args.mode, args.headless, args.max_steps)
    return 0


if __name__ == "__main__":
    sys.exit(main())
