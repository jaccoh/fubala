"""Smoke-check for macos/fubala_menu.py on non-macOS (stubbed rumps).

Run: python macos/_smoke_check.py
This is a scratch verification file, not part of the shipped app.
"""

from __future__ import annotations

import subprocess
import sys
import types
import py_compile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

py_compile.compile(str(HERE / "fubala_menu.py"), doraise=True)
print("syntax OK")

# rumps is macOS-only: stub the pieces the module touches at import/def time.
stub = types.ModuleType("rumps")


class _MenuItem:
    def __init__(self, title="", callback=None):
        self.title = title
        self.callback = callback
        self.enabled = True


class _Timer:
    def __init__(self, callback, interval):
        self.callback = callback
        self.interval = interval

    def start(self):
        pass

    def stop(self):
        pass


stub.MenuItem = _MenuItem
stub.Timer = _Timer


class _App:
    """Stand-in for rumps.App: accepts its kwargs, does nothing."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        pass

    def run(self) -> None:
        pass


stub.App = _App
stub.debug_mode = lambda *a, **k: None
stub.quit_application = lambda *a, **k: None
stub.notification = lambda *a, **k: None
sys.modules["rumps"] = stub

import fubala_menu as m  # noqa: E402

print("import OK")
print("PORT", m.PORT)
print("HEALTH_URL", m.HEALTH_URL)
print("BOARD_URL", m.BOARD_URL)
print("LOG_PATH", m.LOG_PATH)
print("backend dir:", m.BACKEND_DIR, "exists:", m.BACKEND_DIR.is_dir())
print("repo root:", m.REPO_ROOT)

# Health poll must tolerate nothing listening, silently.
print("health_ok (nothing serving):", m.health_ok())
assert m.health_ok() is False
print("port_in_use:", m._port_in_use(m.PORT))

# ServerManager against a throwaway fake "server" that answers nothing.
mgr = m.ServerManager(backend_dir=m.BACKEND_DIR, port=m.PORT)
print("running (no server):", mgr.running)
assert mgr.running is False
assert mgr.owns_process is False
print("start() on a free port should refuse 'uv' missing or start:", end=" ")
if sys.platform == "darwin":
    out = mgr.start()
    print(out)
else:
    print("skipped spawn (non-macOS; uv/rumps unavailable here)")

# stop() must be safe when there is no child at all.
print("stop() with no child:", mgr.stop())
mgr.process = None
mgr.owns_process = False
print("restart() with no child:", mgr.restart() if sys.platform == "darwin" else "skipped")

# A dead Popen object must not be reported as running / must not crash stop().
fake = subprocess.Popen([sys.executable, "-c", "pass"])
fake.wait()
mgr.process = fake
mgr.owns_process = True
print("running with dead child:", mgr.running)
print("stop() with dead child:", mgr.stop())
assert mgr.process is None and mgr.owns_process is False

# A live-but-not-listening child must block a second start (no double spawn).
live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
try:
    mgr.process = live
    mgr.owns_process = True
    out = mgr.start()
    print("start() while child booting:", out)
    assert out == "starting", out
    assert mgr.process is live, "start() must not replace the live child"
finally:
    live.kill()
    live.wait()
    mgr.process = None
    mgr.owns_process = False

# Menu construction wiring.
app = m.FubalaMenuBarApp(manager=m.ServerManager())
print("status title:", app.status_item.title)
print("start enabled:", app.start_item.enabled, "| stop:", app.stop_item.enabled,
      "| restart:", app.restart_item.enabled)
app.on_tick(None)
app.on_stop(None)
app.on_open_board(None)  # webbrowser.open returns False without a handler; fine
print("post-refresh title:", app.status_item.title)
print("ALL CHECKS PASSED")
