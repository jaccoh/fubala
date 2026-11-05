# Fubala for macOS (menu-bar launcher)

A tiny [rumps](https://rumps.readthedocs.io/) menu-bar app that starts, stops,
and restarts the Fubala uvicorn server, shows live status polled from
`/api/v1/health` every 5 seconds, and opens the board in your browser.

It never imports or modifies anything in `backend/` — it only spawns
`uv run uvicorn app.main:app --port 8000` as a child process group and talks
to the HTTP API.

## Prerequisites

- macOS (rumps wraps `NSStatusItem`; this does not run on Linux/Windows).
- [`uv`](https://docs.astral.sh/uv/) on your `PATH` — the launcher shells out
  to `uv` to run the backend, and to sync this project's own venv.
  (A bundled-venv fallback so `uv` isn't needed at runtime is future work.)
- The Fubala repo checked out, with `macos/` sitting next to `backend/`.

## Run it

```bash
cd macos
uv sync
uv run python fubala_menu.py
```

A `◈` icon appears in the menu bar. The server log (stdout + stderr of the
child) is appended to `~/Library/Logs/fubala-macos.log`, falling back to
`/tmp/fubala-macos.log` if your home `Library/Logs` isn't writable.

## Menu

| Item | What it does |
| --- | --- |
| `● Running` / `○ Stopped` | Status line (not clickable). Shows `● Running (external)` when a server you started outside the app is serving, and `◌ Starting…` for the few seconds between spawning the child and uvicorn binding the port. |
| `Start Server` | Spawns the server in a new session (its own process group). If the port is already serving health — e.g. you ran `uvicorn` by hand — it does **not** spawn a duplicate and surfaces that instead. |
| `Stop Server` | `SIGTERM` to the whole process group, `SIGKILL` after ~5 s, then waits for the port to be released. Only ever kills a server this app started. |
| `Restart Server` | Stop, wait for the port to be free, start. Ends up `● Running` on a fresh process. |
| `Open Board` | Opens http://localhost:8000 in the default browser. |
| `Quit` | Stops a server this app started (predictable lifecycle, no orphaned uvicorn) and exits. An externally started server is left running. |

## Why the process-group kill

`uv run` re-execs uvicorn as its own process, so terminating only the `uv`
parent leaves a live uvicorn holding port 8000. The child is therefore started
with `start_new_session=True` and Stop signals the entire group.

## Packaging as a `.app` (future work, deliberately deferred)

Not implemented yet. The intended route is
[py2app](https://py2app.readthedocs.io/):

```bash
uv add --dev py2app
uv run python setup.py py2app   # once a minimal setup.py exists
```

That would bundle rumps into a double-clickable `dist/Fubala.app`, but it also
raises the bundled-venv/`uv`-on-PATH question above, so it is out of scope
here.
