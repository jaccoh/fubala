"""The startup migration on a database that predates the newest columns.

Image 3077 (24 Oct) crash-looped on the live database with "database is
locked" at PRAGMA table_xinfo("projects"): _migrate ran its ALTERs and
backfill UPDATEs inside one transaction but read the schema through a second
pooled connection. On the live database (77 MB) the backfill over every story
spilled the page cache, the spill took the exclusive lock, and the second
connection could not read. Fresh test databases never reached it --
create_all makes every column, so nothing ran inside the transaction. This
test builds the older shape instead (the gate columns dropped, enough story
rows, a small page cache so the backfill spills) and migrates it.
"""

from sqlalchemy import create_engine, event, inspect, text
from sqlmodel import SQLModel

from app import models  # noqa: F401  (registers the tables on the metadata)
from app.main import _migrate

PROJECT_COLUMNS = ("default_gate_spec", "default_gate_code", "protected_globs",
                   "frozen_globs", "auto_max_diff_lines")
STORY_COLUMNS = ("gate_spec", "gate_code", "human_rejected")
NOW = "2026-10-01 00:00:00"


def test_migrate_adds_missing_columns_to_an_existing_database(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'old.db'}", connect_args={"timeout": 1})

    @event.listens_for(engine, "connect")
    def _small_cache(dbapi_conn, _record):
        # a few pages only: the backfill UPDATE spills like on the 77 MB live db
        dbapi_conn.execute("PRAGMA cache_size=10")

    SQLModel.metadata.create_all(engine)
    with engine.begin() as conn:
        for column in PROJECT_COLUMNS:
            conn.execute(text(f"ALTER TABLE projects DROP COLUMN {column}"))
        for column in STORY_COLUMNS:
            conn.execute(text(f"ALTER TABLE stories DROP COLUMN {column}"))
        conn.execute(text(
            "INSERT INTO projects (id, name, root, extra_paths, enabled, created_at, updated_at) "
            f"VALUES ('p1', 'old project', '/tmp/p1', '[]', 1, '{NOW}', '{NOW}')"
        ))
        body = "x" * 4000
        for i in range(300):
            conn.execute(text(
                "INSERT INTO stories (id, title, description, acceptance, status, depends_on, "
                "created_by, kind, spec_version, spec_rejects, created_at, updated_at) "
                "VALUES (:id, 't', :body, '', 'done', '[]', 'test', 'code', 1, 0, :now, :now)"
            ), {"id": f"s{i:04d}", "body": body, "now": NOW})

    _migrate(engine)

    assert set(PROJECT_COLUMNS) <= {c["name"] for c in inspect(engine).get_columns("projects")}
    assert set(STORY_COLUMNS) <= {c["name"] for c in inspect(engine).get_columns("stories")}
    with engine.connect() as conn:
        project = conn.execute(text(
            "SELECT default_gate_spec, auto_max_diff_lines FROM projects WHERE id='p1'"
        )).one()
        humans = conn.execute(text(
            "SELECT count(*) FROM stories WHERE gate_spec='human' AND gate_code='human'"
        )).scalar_one()
    assert project == ("human", 400)
    assert humans == 300
