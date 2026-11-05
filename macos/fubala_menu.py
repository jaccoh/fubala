"""Fubala menu-bar launcher (macOS, rumps).

Starts / stops / restarts the Fubala uvicorn server as a child process group,
shows live running/stopped status polled from `/api/v1/health`, opens the
board in the default browser, and quits cleanly (killing only a server it
started itself — an externally started server stays visible but untouchable).

Run from the `macos/` directory:

    uv run python fubala_menu.py
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
import urllib.request
import webbrowser
from pathlib import Path

import rumps

# --- Paths & constants, resolved once at module load ------------------------

REPO_ROOT = Path(__file__).resolve().parents[1]
BACKEND_DIR = REPO_ROOT / "backend"
PORT = 8000
HEALTH_URL = f"http://127.0.0.1:{PORT}/api/v1/health"
REVIEW_URL = f"http://127.0.0.1:{PORT}/api/v1/stories?status_filter=human_review"
BOARD_URL = f"http://localhost:{PORT}"

POLL_INTERVAL_S = 5  # rumps.Timer cadence
HEALTH_TIMEOUT_S = 2  # short: the timer must never stack long blocking calls
STOP_GRACE_S = 5.0
PORT_FREE_TIMEOUT_S = 10.0

# Server log: ~/Library/Logs when we have a home, /tmp otherwise.
_LOG_CANDIDATES = (
    Path.home() / "Library" / "Logs" / "fubala-macos.log",
    Path("/tmp/fubala-macos.log"),
)


def _log_path() -> Path:
    for candidate in _LOG_CANDIDATES:
        try:
            candidate.parent.mkdir(parents=True, exist_ok=True)
            with candidate.open("a"):
                pass
            return candidate
        except OSError:
            continue
    return _LOG_CANDIDATES[-1]


LOG_PATH = _log_path()


def health_ok() -> bool:
    """True when the server answers `/api/v1/health` with 200.

    Mirrors `backend/app/selfupdate.py::_healthy`. Any failure — refused
    connection, timeout, non-200, DNS weirdness — is simply "not healthy";
    the poll must stay quiet while the server is down or stopping.
    """
    try:
        with urllib.request.urlopen(HEALTH_URL, timeout=HEALTH_TIMEOUT_S) as res:
            return res.status == 200
    except Exception:
        return False


def _port_in_use(port: int) -> bool:
    import socket

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


def _wait_port_free(port: int, timeout_s: float = PORT_FREE_TIMEOUT_S) -> bool:
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        if not _port_in_use(port):
            return True
        time.sleep(0.25)
    return False


# --- Server process management ----------------------------------------------


class ServerManager:
    """Owns (or merely observes) the uvicorn server process group.

    `uv run` re-execs uvicorn, so `Popen(..., start_new_session=True)` puts
    `uv` + uvicorn in one process group and Stop can kill the whole group —
    killing only the `uv` parent would orphan a live uvicorn on port 8000.
    """

    def __init__(self, backend_dir: Path = BACKEND_DIR, port: int = PORT) -> None:
        self.backend_dir = backend_dir
        self.port = port
        self.process: subprocess.Popen | None = None
        # True only when *we* spawned the current server. A server started
        # outside this app is visible in the menu but never killed by us.
        self.owns_process = False

    # -- status -----------------------------------------------------------

    def _reap_if_dead(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            self._reap()

    def state(self) -> tuple[bool, bool]:
        """(running, starting), computed with exactly one health probe.

        One probe per refresh keeps the blocking time on the UI thread bounded
        even when the server is wedged (the 2 s timeout is the worst case).
        """
        self._reap_if_dead()
        if health_ok():
            return True, False
        if self.process is not None and self.owns_process:
            # A live child that isn't serving yet: `uv` takes a moment to
            # resolve the env before uvicorn binds the port.
            return False, True
        return False, False

    @property
    def is_starting(self) -> bool:
        running, starting = self.state()
        return starting and not running

    @property
    def running(self) -> bool:
        running, _starting = self.state()
        return running

    def _reap(self) -> None:
        if self.process is not None and self.process.poll() is not None:
            try:
                self.process.wait(timeout=0)
            except Exception:
                pass
            self.process = None
            self.owns_process = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> str:
        """Start the server. Returns a short human-readable outcome."""
        if self.process is not None and self.process.poll() is None:
            # We already have a live child — most likely still booting (`uv`
            # has to resolve the env before uvicorn binds the port). Spawning
            # again would leak the first process, so never do that.
            return "starting"

        if self.running:
            if self.owns_process:
                return "already running"
            return "external"

        if _port_in_use(self.port):
            # Something holds the port but health says no — don't spawn a
            # duplicate onto a busy port.
            return "port-busy"

        try:
            log_file = LOG_PATH.open("ab", buffering=0)
        except OSError:
            log_file = subprocess.DEVNULL
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        header = f"\n===== fubala-macos start {stamp} =====\n".encode()
        try:
            if log_file is not subprocess.DEVNULL:
                log_file.write(header)
        except OSError:
            pass

        self.process = subprocess.Popen(
            ["uv", "run", "uvicorn", "app.main:app", "--port", str(self.port)],
            cwd=str(self.backend_dir),
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        if log_file is not subprocess.DEVNULL:
            # The child holds its own dup of the fd; drop ours so repeated
            # Start/Stop cycles don't leak descriptors.
            try:
                log_file.close()
            except OSError:
                pass
        self.owns_process = True
        return "started"

    def stop(self) -> str:
        """Terminate the owned process group. Idempotent; never kills externals."""
        proc = self.process
        if proc is None or proc.poll() is not None:
            self._reap()
            return "not-running"

        try:
            pgid = os.getpgid(proc.pid)
        except (ProcessLookupError, PermissionError):
            pgid = None

        if pgid is not None:
            try:
                os.killpg(pgid, signal.SIGTERM)
            except (ProcessLookupError, PermissionError):
                pass
        else:
            proc.terminate()

        try:
            proc.wait(timeout=STOP_GRACE_S)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except (ProcessLookupError, PermissionError):
                    pass
            try:
                proc.kill()
            except OSError:
                pass
            try:
                proc.wait(timeout=STOP_GRACE_S)
            except subprocess.TimeoutExpired:
                pass

        self.process = None
        self.owns_process = False
        # Let the OS release the port before anyone restarts onto it.
        _wait_port_free(self.port)
        return "stopped"

    def restart(self) -> str:
        self.stop()
        return self.start()

    def quit(self) -> None:
        """Called on app quit: take down a server we own, leave externals."""
        if self.owns_process:
            self.stop()


# --- The menu-bar app -------------------------------------------------------


class FubalaMenuBarApp(rumps.App):
    def __init__(self, manager: ServerManager | None = None) -> None:
        super().__init__(
            name="Fubala",
            title="◈",
            quit_button=None,  # custom Quit below, so it can stop the server
        )
        self.manager = manager or ServerManager()

        self.status_item = rumps.MenuItem(
            "○ Stopped", callback=None
        )  # informational only
        self.start_item = rumps.MenuItem("Start Server", callback=self.on_start)
        self.stop_item = rumps.MenuItem("Stop Server", callback=self.on_stop)
        self.restart_item = rumps.MenuItem("Restart Server", callback=self.on_restart)
        self.open_item = rumps.MenuItem("Open Board", callback=self.on_open_board)
        self.quit_item = rumps.MenuItem("Quit", callback=self.on_quit)

        self.menu = [
            self.status_item,
            None,
            self.start_item,
            self.stop_item,
            self.restart_item,
            None,
            self.open_item,
            None,
            self.quit_item,
        ]

        self.timer = rumps.Timer(self.on_tick, POLL_INTERVAL_S)
        self._gate_notified: set[str] = set()
        self.timer.start()
        # Paint an accurate state immediately instead of waiting 5s.
        self.refresh()

    # -- helpers ----------------------------------------------------------

    def _notify_quietly(self, title: str, message: str) -> None:
        """Notification must never take the app down with it."""
        try:
            rumps.notification(title, "", message)
        except Exception:
            pass

    def refresh(self) -> None:
        """Refresh the status line and the enabled state of the action items."""
        running, starting = self.manager.state()
        if running and self.manager.owns_process:
            label = "● Running"
        elif running:
            label = "● Running (external)"
        elif starting:
            label = "◌ Starting…"
        else:
            label = "○ Stopped"
        self.status_item.title = label

        self.start_item.enabled = not running and not starting
        self.stop_item.enabled = (running or starting) and self.manager.owns_process
        # Restart is meaningful whenever we own the server (stop+start) or
        # when nothing is running (it degenerates to a plain start).
        self.restart_item.enabled = (not running) or self.manager.owns_process

    # -- timer ------------------------------------------------------------

    def on_tick(self, _sender: object) -> None:
        self.refresh()
        self._notify_review_gates()

    def _notify_review_gates(self) -> None:
        """OS-notification for stories waiting at the human gate.

        Deduped per app lifetime: a story notifies once, no matter how
        long it waits. Board API only — this is the human-facing channel;
        AI clients get their signal via MCP wait_for_story instead."""
        if not health_ok():
            return
        try:
            with urllib.request.urlopen(REVIEW_URL, timeout=2) as res:
                stories = json.loads(res.read().decode("utf-8"))
        except (OSError, ValueError):
            return
        for story in stories or []:
            sid = story.get("id")
            title = (story.get("title") or "Story")[:80]
            if sid and sid not in self._gate_notified:
                self._gate_notified.add(sid)
                self._notify_quietly(
                    "Fubala · review gate", f"{title} awaits your approval"
                )

    # -- actions ----------------------------------------------------------

    def on_start(self, _sender: object) -> None:
        outcome = self.manager.start()
        if outcome == "external":
            self._notify_quietly(
                "Fubala", "Server already running (started outside this app)."
            )
        elif outcome == "starting":
            self._notify_quietly(
                "Fubala", "Server is already starting; give it a few seconds."
            )
        elif outcome == "port-busy":
            self._notify_quietly(
                "Fubala", f"Port {self.manager.port} is busy; not starting a duplicate."
            )
        elif outcome == "already running":
            pass
        self.refresh()

    def on_stop(self, _sender: object) -> None:
        self.manager.stop()
        self.refresh()

    def on_restart(self, _sender: object) -> None:
        if not self.manager.owns_process and self.manager.running:
            # External server: we can't stop it, so a restart would only
            # surface a "no duplicate" no-op. Say so instead of pretending.
            self._notify_quietly(
                "Fubala",
                "Server was started outside this app; leaving it alone.",
            )
            self.refresh()
            return
        self.manager.restart()
        self.refresh()

    def on_open_board(self, _sender: object) -> None:
        webbrowser.open(BOARD_URL)

    def on_quit(self, _sender: object) -> None:
        self.timer.stop()
        self.manager.quit()
        rumps.quit_application()


def main() -> int:
    if sys.platform != "darwin":
        print(
            "fubala_menu.py requires macOS (rumps wraps NSStatusItem).",
            file=sys.stderr,
        )
        return 1
    if not BACKEND_DIR.is_dir():
        print(f"backend directory not found: {BACKEND_DIR}", file=sys.stderr)
        return 1
    app = FubalaMenuBarApp()
    rumps.debug_mode(False)
    app.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
