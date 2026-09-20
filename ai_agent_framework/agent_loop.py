"""
agent_loop.py — Main perceive → decide → act → observe orchestration loop.

Runs a single task for Minetallest with:
  - state persistence (history + last observation saved to disk each step)
  - a hard step limit (config.MAX_STEPS_PER_TASK, default 20)
  - graceful interruption (Ctrl+C → GracefulInterrupt, corner fail-safe)
  - safety validation through safety.validate_action before every action
"""

import json
import os
import time
from typing import Any, Dict, List, Optional

import config
import perception
from actions import ActionError, DesktopController, WebDriver
from llm_client import LLMClient, LLMProtocolError
from safety import GracefulInterrupt, SafetyViolation, is_failsafe_triggered, validate_action


def _log(message: str) -> None:
    print(f"[{config.AGENT_NAME}] {message}")


class AgentLoop:
    """Orchestrates one task from goal to completion (or step-limit abort)."""

    def __init__(self, mode: str = "web", headless: Optional[bool] = None) -> None:
        self.mode = mode  # "web" or "desktop"
        self.driver: Optional[WebDriver] = None
        self.desktop: Optional[DesktopController] = None
        self.llm = LLMClient()
        self.history: List[Dict[str, Any]] = []
        self.state: Dict[str, Any] = {}
        self.max_steps = config.MAX_STEPS_PER_TASK
        self.state_path = os.path.join(config.ARTIFACTS_DIR, "session_state.json")

        # Allow one-off headless override without touching global config
        if headless is not None:
            config.BROWSER_HEADLESS = headless

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        """Initialize the execution backend for the chosen mode.
        Reuses a still-alive browser across tasks (keeps tabs, cookies and
        logins); relaunches only if the previous one died."""
        os.makedirs(config.ARTIFACTS_DIR, exist_ok=True)
        if self.mode == "web":
            if self.driver is not None:
                try:
                    _ = self.driver.page.url  # liveness probe
                    _log("Reusing existing browser session")
                    return
                except Exception:
                    _log("Previous browser session is dead — relaunching")
                    self.shutdown()
            _log(f"Launching browser for {config.USER_NAME} (headless={config.BROWSER_HEADLESS})")
            self.driver = WebDriver()
        else:
            _log(f"Initializing desktop controller for {config.USER_NAME}")
            self.desktop = DesktopController()

    def shutdown(self) -> None:
        """Release resources. Safe to call multiple times."""
        if self.driver:
            self.driver.close()
            self.driver = None

    # -- state persistence --------------------------------------------------------

    def persist_state(self) -> None:
        """Write history + last observation to disk so tasks are resumable /
        auditable after a crash or interrupt."""
        try:
            with open(self.state_path, "w", encoding="utf-8") as f:
                json.dump(
                    {
                        "agent_name": config.AGENT_NAME,
                        "user_name": config.USER_NAME,
                        "history": self.history,
                        "last_state": {k: v for k, v in self.state.items() if k != "screenshot_base64"},
                    },
                    f,
                    indent=2,
                    default=str,
                )
        except OSError:
            pass  # persistence is best-effort; never crash the loop for it

    # -- loop stages ----------------------------------------------------------------

    def perceive(self) -> Dict[str, Any]:
        """Gather current screen/DOM state via the perception system."""
        if self.mode == "web" and self.driver:
            return perception.observe_web(self.driver)
        return perception.observe_desktop(self.desktop)  # type: ignore[arg-type]

    def decide(self, task: str) -> Dict[str, Any]:
        """Ask the LLM for the next action given task + history + state."""
        return self.llm.decide(
            task=task,
            state=self.state,
            history=self.history,
            step=len(self.history) + 1,
            max_steps=self.max_steps,
        )

    def act(self, decision: Dict[str, Any], task_text: str) -> Dict[str, Any]:
        """Execute a validated decision through the safety pipeline, returning
        a result dict {ok, summary, error?}."""
        action = decision["action"]
        params = decision.get("parameters", {})

        # Safety first: hard blocks raise, soft gate may return False
        try:
            allowed = validate_action(action, params, task_text)
        except SafetyViolation as e:
            return {"ok": False, "summary": "blocked by safety", "error": str(e)}
        if not allowed:
            return {"ok": False, "summary": "declined by user", "error": f"{config.USER_NAME} declined the action"}

        # Desktop mode: map coordinate-based actions onto PyAutoGUI
        if self.mode == "desktop":
            return self._act_desktop(action, params)

        assert self.driver is not None  # web mode
        try:
            if action == "navigate":
                return self.driver.navigate(params["url"])
            if action == "click":
                return self.driver.click_element(params["selector"])
            if action == "type":
                sel = params.get("selector")
                if sel:
                    clicked = self.driver.click_element(sel)
                    if not clicked.get("ok"):
                        return clicked
                return self.driver.type_text(sel, params["text"]) if sel else \
                    self.driver.type_text("body", params["text"], clear_first=False)
            if action == "scroll":
                return self.driver.scroll(params.get("direction", "down"), int(params.get("amount_px", 600)))
            if action == "hover":
                return self.driver.hover(params["selector"])
            if action == "shortcut":
                return self.driver.press_key(params["key_combination"])
            if action == "finish":
                return {"ok": True, "summary": "task marked complete by model"}
            if action == "error":
                return {"ok": False, "summary": "model reported task error",
                        "error": decision.get("thought_process", "")}
        except ActionError as e:
            return {"ok": False, "summary": "action error", "error": str(e)}
        return {"ok": False, "summary": "unhandled action", "error": f"No handler for '{action}'"}

    def _act_desktop(self, action: str, params: Dict[str, Any]) -> Dict[str, Any]:
        """Route desktop-mode actions through PyAutoGUI primitives."""
        assert self.desktop is not None
        coords = params.get("coordinates") or {}
        x, y = int(coords.get("x", 0)), int(coords.get("y", 0))
        try:
            if action == "click":
                return self.desktop.click_coordinates(x, y)
            if action == "type":
                return self.desktop.type_keystrokes(params["text"])
            if action == "scroll":
                direction = 1 if params.get("direction", "down") == "down" else -1
                self.desktop.pyautogui.scroll(direction * int(params.get("amount_px", 600)))
                return {"ok": True}
            if action == "shortcut":
                return self.desktop.shortcut_keys(params["key_combination"])
            if action in ("navigate", "hover", "finish", "error"):
                if action == "hover":
                    return self.desktop.move_mouse(x, y)
                return {"ok": action == "finish",
                        "summary": f"desktop {action}"}
        except Exception as e:
            return {"ok": False, "summary": "desktop action failed", "error": str(e)}
        return {"ok": False, "summary": "unhandled desktop action", "error": f"No desktop handler for '{action}'"}

    # -- main loop ----------------------------------------------------------------------

    def run(self, task: str, progress_cb=None, keep_alive: bool = False) -> Dict[str, Any]:
        """Run the perceive→decide→act→observe loop until finish/limit/interrupt.

        progress_cb: optional callback(dict) invoked after each step with
        {step, max_steps, action, summary, result, status} — used by the
        remote bridge to stream progress to the phone dashboard.
        keep_alive: when True, the browser stays open after the task so the
        next task continues in the same session (used by the remote bridge).

        Returns a summary dict: {status, steps, final_message}.
        Statuses: completed | max_steps_reached | interrupted | blocked
        """
        _log(f"New task from {config.USER_NAME}: {task}")
        self.history = []  # each task is independent; step numbering restarts
        try:
            self.start()
        except ActionError as e:
            _log(f"Startup failed: {e}")
            return {"status": "blocked", "steps": 0, "final_message": str(e)}

        outcome = {"status": "max_steps_reached", "steps": 0, "final_message": "Step limit reached."}
        try:
            for step in range(1, self.max_steps + 1):
                # -- fail-safe poll (corner trigger aborts before acting) --
                if is_failsafe_triggered():
                    raise GracefulInterrupt("PyAutoGUI corner fail-safe triggered")

                # -- remote stop check (phone dashboard can halt the task) --
                if progress_cb and progress_cb({"type": "step_start", "step": step,
                                                "max_steps": self.max_steps}) is False:
                    raise GracefulInterrupt("Stopped remotely from dashboard")

                # -- PERCEIVE --
                # OBSERVE_EVERY_N: optionally reuse the previous observation to
                # save a screenshot+LLM-vision round-trip. The model is told the
                # image may be stale.
                reuse_observation = (
                    self.state
                    and config.OBSERVE_EVERY_N_STEPS > 1
                    and step % config.OBSERVE_EVERY_N_STEPS != 1
                )
                if reuse_observation:
                    self.state = {**self.state, "stale": True}
                    _log(f"Step {step}/{self.max_steps}: reusing last observation (no new screenshot)")
                else:
                    _log(f"Step {step}/{self.max_steps}: observing environment…")
                    self.state = self.perceive()
                self.persist_state()

                # -- DECIDE --
                try:
                    decision = self.decide(task)
                except LLMProtocolError as e:
                    _log(f"Decision protocol error: {e}")
                    outcome = {"status": "blocked", "steps": step,
                               "final_message": f"LLM output invalid: {e}"}
                    break

                _log(f"Thought: {decision.get('thought_process', '')[:160]}")
                if "EMERGENCY_STOP" in decision.get("thought_process", ""):
                    raise GracefulInterrupt("Model requested emergency stop")

                # -- ACT (execute the whole batch; no screenshots in between) --
                task_done = False
                for sub in decision.get("actions", []):
                    if is_failsafe_triggered():
                        raise GracefulInterrupt("PyAutoGUI corner fail-safe triggered")

                    _log(f"Executing {sub['action']} action for {config.USER_NAME}…")
                    result = self.act(sub, task)
                    self.history.append({
                        "step": step,
                        "action": sub["action"],
                        "summary": json.dumps(sub.get("parameters", {}))[:120],
                        "result": "ok" if result.get("ok") else f"failed: {result.get('error', 'unknown')}",
                    })
                    _log(f"Result: {result.get('summary', 'done')}"
                         + (f" ({result.get('error')})" if result.get("error") else ""))

                    # stream progress to the remote dashboard (if bridged)
                    if progress_cb:
                        progress_cb({"type": "step", "step": step, "max_steps": self.max_steps,
                                     "action": sub["action"], "thought": decision.get("thought_process", "")[:200],
                                     "summary": json.dumps(sub.get("parameters", {}))[:200],
                                     "result": "ok" if result.get("ok") else "failed",
                                     "error": result.get("error", "")[:200]})

                    # finish/error terminate the task; a FAILED action aborts
                    # the rest of the batch so the model re-plans on fresh state.
                    if sub["action"] == "finish" and result.get("ok"):
                        outcome = {"status": "completed", "steps": step,
                                   "final_message": decision.get("thought_process", "Task complete.")}
                        task_done = True
                        break
                    if sub["action"] == "error":
                        outcome = {"status": "blocked", "steps": step,
                                   "final_message": decision.get("thought_process", "Model reported an error.")}
                        task_done = True
                        break
                    if not result.get("ok"):
                        _log("Batch aborted after failed action — re-observing.")
                        break

                if task_done:
                    break

                time.sleep(config.OBSERVE_DELAY_SECONDS)

        except GracefulInterrupt as e:
            _log(f"⏹ Interrupted: {e}")
            outcome = {"status": "interrupted", "steps": len(self.history), "final_message": str(e)}
        except KeyboardInterrupt:
            _log(f"⏹ Ctrl+C received — halting gracefully for {config.USER_NAME}")
            outcome = {"status": "interrupted", "steps": len(self.history),
                       "final_message": "Interrupted by user (Ctrl+C)."}
        finally:
            if progress_cb:
                progress_cb({"type": "done", "status": outcome["status"], "steps": outcome["steps"],
                             "message": outcome["final_message"][:300]})
            self.persist_state()
            if not keep_alive:
                self.shutdown()

        _log(f"Task finished [{outcome['status']}] after {outcome['steps']} step(s): "
             f"{outcome['final_message'][:160]}")
        return outcome
