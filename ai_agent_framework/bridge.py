"""
bridge.py — Remote-control bridge: your phone dashboard <-> this PC's Mine agent.

Architecture (Vercel can't hold live connections, so Firebase is the relay):

  Phone (Vercel site)  --HTTPS-->  Firebase Realtime DB  <--polling--  PC bridge

  - Phone writes  commands/task  = {"goal": "...", "token": "..."}
  - Bridge polls, validates the token, runs AgentLoop, streams:
        status/current   = {state, task, step, thought, last_action, ts}
        status/log       = push-edited step entries (newest last)
        status/image     = base64 of latest annotated screenshot
  - Phone writes  commands/stop = true  → bridge halts the running task.

Run it on the PC (keep main.py free for local use):

    set MINE_FIREBASE_DB_URL=https://YOUR-DB.firebaseio.com
    set MINE_BRIDGE_TOKEN=some-long-random-string
    python bridge.py

The phone dashboard URL is then:
    https://your-site.vercel.app/#/YOUR-TOKEN   (token in the hash, not sent to any server)
"""

import base64
import json
import os
import threading
import time
from typing import Any, Dict, Optional

import requests

import config
from agent_loop import AgentLoop

# ---------------------------------------------------------------------------
# Firebase REST helpers (no SDK needed — plain REST keeps this dependency-light)
# ---------------------------------------------------------------------------

class Firebase:
    def __paths__(self):  # pragma: no cover - namespace helper
        raise NotImplementedError

    def __init__(self, db_url: str, token: str) -> None:
        if not db_url:
            raise RuntimeError("MINE_FIREBASE_DB_URL is not set (e.g. https://mine-xxxx.firebaseio.com)")
        if not token:
            raise RuntimeError("MINE_BRIDGE_TOKEN is not set")
        self.base = db_url.rstrip("/")
        self.token = token

    def _url(self, path: str) -> str:
        return f"{self.base}/{path}.json?auth={self.token}"

    def get(self, path: str, timeout: int = 10) -> Any:
        r = requests.get(self._url(path), timeout=timeout)
        r.raise_for_status()
        return r.json()

    def set(self, path: str, value: Any, timeout: int = 10) -> None:
        r = requests.put(self._url(path), json=value, timeout=timeout)
        r.raise_for_status()

    def update(self, path: str, value: Dict[str, Any], timeout: int = 10) -> None:
        r = requests.patch(self._url(path), json=value, timeout=timeout)
        r.raise_for_status()

    def push(self, path: str, value: Any, timeout: int = 10) -> None:
        r = requests.post(self._url(path), json=value, timeout=timeout)
        r.raise_for_status()

    def delete(self, path: str, timeout: int = 10) -> None:
        r = requests.delete(self._url(path), timeout=timeout)
        r.raise_for_status()


# ---------------------------------------------------------------------------
# Remote task runner
# ---------------------------------------------------------------------------

class RemoteRunner:
    """Runs AgentLoop tasks in a worker thread, streaming state to Firebase."""

    def __init__(self, fb: Firebase) -> None:
        self.fb = fb
        self.mode = os.getenv("MINE_BRIDGE_MODE", "web")  # "web" | "desktop"
        self.task_thread: Optional[threading.Thread] = None
        self.stop_requested = threading.Event()
        self.current_task: Optional[str] = None
        self.loop: Optional[AgentLoop] = None

    # -- status helpers --------------------------------------------------------

    def _set_status(self, **fields) -> None:
        payload = {"ts": int(time.time()), **fields}
        try:
            self.fb.update("status/current", payload)
        except requests.RequestException as e:
            print(f"[Mine] (bridge) status update failed: {e}")

    def _push_log(self, entry: Dict[str, Any]) -> None:
        try:
            self.fb.push("status/log", {"ts": int(time.time()), **entry})
            self._trim_log()
        except requests.RequestException:
            pass  # log pushes are best-effort

    MAX_LOG_ENTRIES = 60

    def _trim_log(self) -> None:
        """Keep the log node small so phone fetches stay fast."""
        try:
            log = self.fb.get("status/log") or {}
            if len(log) > self.MAX_LOG_ENTRIES + 20:
                keys = sorted(log, key=lambda k: log[k].get("ts", 0))
                for k in keys[:len(log) - self.MAX_LOG_ENTRIES]:
                    self.fb.delete(f"status/log/{k}")
        except (requests.RequestException, AttributeError):
            pass

    def _upload_image(self, path: str) -> None:
        """Upload latest annotated screenshot as base64 (best-effort)."""
        try:
            if path and os.path.exists(path):
                with open(path, "rb") as f:
                    b64 = "data:image/png;base64," + base64.b64encode(f.read()).decode("ascii")
                # Cap size — Firebase value limit is 10 MB; annotated PNGs are
                # typically 100-500 KB which is fine for personal use.
                self.fb.set("status/image", b64[:5_000_000])
        except (requests.RequestException, OSError) as e:
            print(f"[Mine] (bridge) image upload failed: {e}")

    # -- progress callback wired into AgentLoop.run -----------------------------

    def _progress(self, info: Dict[str, Any]) -> Any:
        """Called by AgentLoop during a task. Returns False to request a stop."""
        if self.stop_requested.is_set():
            return False  # agent raises GracefulInterrupt → clean stop

        kind = info.get("type")
        if kind == "step_start":
            self._set_status(state="running", step=info.get("step"), max_steps=info.get("max_steps"))
        elif kind == "step":
            self._set_status(state="running", step=info.get("step"),
                             last_action=info.get("action"),
                             thought=info.get("thought", ""))
            self._push_log(info)
            # Attach the freshest screenshot after each executed action
            shot = getattr(self.loop, "state", {}).get("annotated_screenshot")
            if shot:
                self._upload_image(shot)
        elif kind == "done":
            self._set_status(state=info.get("status", "idle"),
                             step=info.get("steps"),
                             last_action="done",
                             thought=info.get("message", ""))
            self._push_log({"action": "task_end", "result": info.get("status"),
                            "summary": info.get("message", "")})
        return None

    # -- worker -------------------------------------------------------------------

    def _run_task(self, goal: str) -> None:
        try:
            self.stop_requested.clear()
            if self.loop is None:
                self.loop = AgentLoop(mode=self.mode)
            self.loop.run(goal, progress_cb=self._progress, keep_alive=True)
        except Exception as e:  # never let the worker thread die silently
            print(f"[Mine] (bridge) task crashed: {e}")
            self._set_status(state="error", thought=f"Task crashed: {e}")
            # The session may be corrupted — discard it so the next task
            # gets a freshly launched browser.
            try:
                if self.loop:
                    self.loop.shutdown()
            except Exception:
                pass
            self.loop = None
        finally:
            self.current_task = None
            # self.loop is intentionally kept alive between tasks so the
            # browser (tabs, cookies, logins) persists; it is only reset
            # to None after a crash.
            self._set_status(state="idle")

    # -- main loop ---------------------------------------------------------------

    def serve_forever(self) -> None:
        print(f"[Mine] (bridge) polling {self.fb.base} every {config.BRIDGE_POLL_SECONDS}s "
              f"(mode={self.mode}) — Ctrl+C to stop")
        self._set_status(state="idle", last_action="bridge_started")
        while True:
            try:
                cmd = self.fb.get("commands/task") or {}
                if cmd.get("goal"):
                    if cmd.get("token") != self.fb.token:
                        theirs, ours = str(cmd.get("token")), self.fb.token
                        print(f"[Mine] (bridge) WARNING: task arrived but token mismatch "
                              f"— ignoring. dashboard: len={len(theirs)} '{theirs[:8]}...{theirs[-4:]}' | "
                              f"bridge: len={len(ours)} '{ours[:8]}...{ours[-4:]}'")
                    elif self.task_thread and self.task_thread.is_alive():
                        self._push_log({"action": "rejected",
                                        "summary": "busy: task already running"})
                    else:
                        # consume the command so it doesn't re-run
                        self.fb.delete("commands/task")
                        goal = str(cmd["goal"])[:500]
                        print(f"[Mine] (bridge) task received: {goal}")
                        self.current_task = goal
                        self._set_status(state="starting", task=goal)
                        self._push_log({"action": "task_start", "summary": goal})
                        self.task_thread = threading.Thread(
                            target=self._run_task, args=(goal,), daemon=True)
                        self.task_thread.start()
                # stop request
                if self.fb.get("commands/stop"):
                    self.fb.delete("commands/stop")
                    if self.task_thread and self.task_thread.is_alive():
                        self.stop_requested.set()
                        self._push_log({"action": "stop_requested", "summary": "stop from dashboard"})
                # heartbeat so the phone knows the PC is alive
                self.fb.update("status/current", {"heartbeat": int(time.time())})
            except requests.RequestException as e:
                body = ""
                resp = getattr(e, "response", None)
                if resp is not None:
                    try:
                        body = f" — body: {resp.text[:300]}"
                    except Exception:
                        pass
                print(f"[Mine] (bridge) poll error: {e}{body}")
            time.sleep(config.BRIDGE_POLL_SECONDS)


def main() -> None:
    fb = Firebase(config.FIREBASE_DB_URL, config.BRIDGE_TOKEN)
    RemoteRunner(fb).serve_forever()


if __name__ == "__main__":
    main()
