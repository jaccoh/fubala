"""The v0.10 executor backfill must run once, when the column is added.

16 Oct: two finished stories of "Fubala itself" were switched to
execution=external (landed by an external agent). On the next boot the
backfill -- which ran on EVERY startup -- flipped the whole project to
executor=external, and the scheduler silently stopped picking up every
inheriting story of that project.
"""

import sqlite3
import subprocess

from fastapi.testclient import TestClient

from app.main import create_app


def _repo(path) -> str:
    path.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("x\n")
    subprocess.run(["git", "add", "-A"], cwd=path, check=True)
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", "i"],
        cwd=path,
        check=True,
    )
    return str(path)


def _project_with_external_story(client, root) -> str:
    project = client.post("/api/v1/projects", json={"name": "p", "root": root}).json()
    assert project["executor"] == "internal"
    story = client.post(
        "/api/v1/stories", json={"title": "s", "project_id": project["id"]}
    ).json()
    res = client.patch(f"/api/v1/stories/{story['id']}", json={"execution": "external"})
    assert res.status_code == 200, res.text
    return project["id"]


def test_restart_keeps_an_internal_project_internal(tmp_path, monkeypatch):
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(tmp_path / "m.db"))
    with TestClient(create_app(gateway=object())) as client:
        pid = _project_with_external_story(client, _repo(tmp_path / "r"))
    with TestClient(create_app(gateway=object())) as client:
        project = next(p for p in client.get("/api/v1/projects").json() if p["id"] == pid)
    assert project["executor"] == "internal", "a reboot must not flip the project executor"


def test_legacy_db_backfills_executor_from_external_stories(tmp_path, monkeypatch):
    """The backfill's real purpose still holds: a DB from before the column
    existed gets executor=external for projects with external stories."""
    db_path = tmp_path / "legacy.db"
    monkeypatch.setenv("FUBALA_DATABASE_PATH", str(db_path))
    with TestClient(create_app(gateway=object())) as client:
        pid = _project_with_external_story(client, _repo(tmp_path / "r"))
    conn = sqlite3.connect(db_path)
    conn.execute("ALTER TABLE projects DROP COLUMN executor")
    conn.commit()
    conn.close()
    with TestClient(create_app(gateway=object())) as client:
        project = next(p for p in client.get("/api/v1/projects").json() if p["id"] == pid)
    assert project["executor"] == "external"
