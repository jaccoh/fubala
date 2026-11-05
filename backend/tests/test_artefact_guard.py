"""Artefact guard: coder commits must never carry build artefacts.

The ems incident: setuptools wrote `build/lib/...` during a non-editable
verify; `git check-ignore`, run afterwards on the MAIN checkout, said
"ignored", yet the files landed in the coder commit (bbac771, 4d74aba).
The rules and the worktree forked at different times, so the worktree's
own ignore state at commit time let `git add -A` stage the tree.

Under test here:
1. the REAL coder/verify path (`ToolBox._run_command`,
   `runner._run_verify_command`) producing artefacts inside a linked
   worktree, then `workspaces.commit` — red before the guard, green now;
2. the guard's component matching (build/, dist/, .venv/, *.egg-info/ at
   any depth; `src/dist_helper.py`, `tools/build.py`, `dist_utils/` survive);
3. the warning `StoryNote` the runner's callback writes;
4. a worktree with nothing but artefacts staged commits nothing at all.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from sqlmodel import create_engine, select

from app import workspaces
from app.agent_tools import ToolBox
from app.db import init_db, session_scope
from app.models import Story, StoryNote
from app.runner import (
    ARTEFACT_NOTE_PATH_CAP,
    StoryRunner,
    _artefact_note_writer,
    _run_verify_command,
    artefact_note_body,
)
from app.workspaces import ARTEFACT_PATTERNS, artefact_matches


def _git(cwd: Path, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    res = subprocess.run(
        ["git", "-C", str(cwd), *args], capture_output=True, text=True
    )
    if check and res.returncode != 0:
        raise AssertionError(f"git {' '.join(args)} failed:\n{res.stderr}")
    return res


def _commit_main(cwd: Path, message: str) -> None:
    _git(cwd, "add", "-A")
    _git(cwd, "-c", "user.name=t", "-c", "user.email=t@t", "commit", "-qm", message)


def make_repo(tmp_path: Path) -> Path:
    """Bare-bones repo with one commit — mirrors test_warm_verify.make_repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    (repo / "README.md").write_text("# repo\n")
    _git(repo, "init", "-q")
    _commit_main(repo, "init")
    return repo


def branch_tree(repo: Path, story_id: str) -> list[str]:
    out = _git(repo, "ls-tree", "-r", "--name-only", f"fubala/{story_id}").stdout
    return [line.strip() for line in out.splitlines() if line.strip()]


# --- the incident, replayed through the real coder/verify path --------------


def test_verify_written_build_tree_stays_out_of_the_commit(tmp_path):
    """Red-first reproduction of the ems shape.

    The worktree forks from a commit with NO ignore rules; main gains a
    `.gitignore` listing `build/` afterwards. `git check-ignore` run on the
    main checkout (as the incident's after-the-fact diagnosis did) reports
    the path ignored, but inside the worktree `git add -A` happily stages
    it. The pre-commit guard must keep it out of the branch tree."""
    repo = make_repo(tmp_path)
    story_id = "red1"
    wt, _base, _new = workspaces.create(str(repo), story_id)
    assert (wt / ".git").is_file()  # linked-worktree shape, `.git` is a file

    # main learns to ignore build artefacts only AFTER the fork
    (repo / ".gitignore").write_text("build/\ndist/\n*.egg-info/\n.venv/\n")
    _commit_main(repo, "ignore rules")
    assert (
        _git(repo, "check-ignore", "-q", "build/lib/x.py", check=False).returncode == 0
    ), "precondition: main's check-ignore says build/ is ignored"

    # the coder's own shell tool writes the setuptools-style tree
    box = ToolBox(
        project_root=str(repo),
        workspace=str(wt),
        allow_commands=("python3",),
        writable=True,
    )
    result = box._run_command(
        {
            "command": (
                "python3 -c \"import pathlib; "
                "pathlib.Path('build/lib').mkdir(parents=True, exist_ok=True); "
                "pathlib.Path('build/lib/x.py').write_text('x = 1\\n')\""
            )
        }
    )
    assert result.startswith("exit=0"), result
    assert (wt / "build" / "lib" / "x.py").is_file()

    # the verify stage adds a dist/ tree the same way
    rc, output = _run_verify_command(
        wt, "mkdir -p dist/assets && echo 'compiled' > dist/assets/app.js", 30, str(repo)
    )
    assert rc == 0, output
    assert (wt / "dist" / "assets" / "app.js").is_file()

    seen: list[list[str]] = []
    workspaces.commit(str(repo), story_id, "story red1: coder output", seen.append)

    tree = branch_tree(repo, story_id)
    assert not any(p.startswith("build/") for p in tree), tree
    assert not any(p.startswith("dist/") for p in tree), tree
    assert seen, "the guard must report what it unstaged"
    assert any(p.startswith("build/") for p in seen[0])
    assert any(p.startswith("dist/") for p in seen[0])
    # worktree files survive on disk — only the commit is kept clean
    assert (wt / "build" / "lib" / "x.py").is_file()
    assert (wt / "dist" / "assets" / "app.js").is_file()


# --- the guard: every artefact shape, and nothing else ----------------------


def test_guard_unstages_every_artefact_shape_and_keeps_similar_names(tmp_path):
    repo = make_repo(tmp_path)
    story_id = "guard1"
    wt, _base, _new = workspaces.create(str(repo), story_id)

    keep = {
        "src/dist_helper.py": "helper\n",  # substring 'dist', own component 'dist_helper.py'
        "tools/build.py": "tool\n",  # 'build' is a FILE component, not a directory
        "dist_utils/x.py": "du\n",
        "docs/dist.md": "doc\n",
        "real.egg-infoish.py": "not an egg-info dir\n",
    }
    drop = {
        "build/lib/pkg/mod.py": "artefact\n",
        "dist/app.js": "artefact\n",
        "pkg.egg-info/PKG-INFO": "artefact\n",
        ".venv/bin/python": "artefact\n",
        "deep/nested/build/lib/x.py": "artefact at depth\n",
        "sub/dist/inner.js": "artefact at depth\n",
    }
    for rel, text in {**keep, **drop}.items():
        target = wt / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text)

    seen: list[list[str]] = []
    workspaces.commit(str(repo), story_id, "story guard1: coder output", seen.append)

    tree = branch_tree(repo, story_id)
    for rel in keep:
        assert rel in tree, f"legitimate file {rel} must stay committed"
    for rel in drop:
        assert rel not in tree, f"artefact {rel} must be unstaged"
        assert rel in seen[0], f"artefact {rel} must be named in the report"
    # belt and braces: nothing in the committed tree matches the patterns
    for path in tree:
        assert not artefact_matches([path]), f"{path} rode along in the commit"
    # the reported set contains ONLY artefacts — no legitimate file was dropped
    assert set(seen[0]) == set(drop)


def test_only_artefacts_staged_means_no_commit(tmp_path):
    """A worktree holding nothing but artefacts must not fabricate a commit
    (nor fail one: `git commit` with an empty index errors out)."""
    repo = make_repo(tmp_path)
    story_id = "skip1"
    wt, _base, _new = workspaces.create(str(repo), story_id)
    before = _git(repo, "rev-parse", f"fubala/{story_id}").stdout.strip()

    (wt / "build" / "lib").mkdir(parents=True)
    (wt / "build" / "lib" / "x.py").write_text("x = 1\n")

    seen: list[list[str]] = []
    assert (
        workspaces.commit(str(repo), story_id, "story skip1: coder output", seen.append)
        == ""
    )
    assert _git(repo, "rev-parse", f"fubala/{story_id}").stdout.strip() == before
    assert seen == [["build/lib/x.py"]]
    # the guard unstages, it never deletes the coder's files
    assert (wt / "build" / "lib" / "x.py").read_text() == "x = 1\n"


def test_artefacts_already_tracked_get_untracked_by_the_guard(tmp_path):
    """An artefact committed by an EARLIER coder round (the bbac771 shape)
    must not stay tracked forever: the guard drops it from the index, and
    the commit then records the deletion."""
    repo = make_repo(tmp_path)
    story_id = "tracked1"
    wt, _base, _new = workspaces.create(str(repo), story_id)
    (wt / "build" / "lib").mkdir(parents=True)
    (wt / "build" / "lib" / "legacy.py").write_text("committed by round 1\n")
    (wt / "feature.py").write_text("print('real work')\n")
    # Round 1 is PRE-GUARD history: it landed the artefact exactly as the
    # bbac771/4d74aba commits did, so it must be written with plain git —
    # routing it through workspaces.commit would let today's guard unstage
    # the very artefact the setup needs to have tracked.
    _git(wt, "add", "-A")
    _git(
        wt,
        "-c",
        "user.name=fubala",
        "-c",
        "user.email=fubala@local",
        "commit",
        "-qm",
        "round 1: artefact landed",
    )
    assert "build/lib/legacy.py" in branch_tree(repo, story_id)

    workspaces.commit(str(repo), story_id, "round 2: guard cleans up")

    tree = branch_tree(repo, story_id)
    assert "build/lib/legacy.py" not in tree, "tracked artefact must be untracked"
    assert "feature.py" in tree


# --- pure helpers -----------------------------------------------------------


def test_pattern_constants_and_matching():
    assert "build" in ARTEFACT_PATTERNS
    assert "dist" in ARTEFACT_PATTERNS
    assert ".venv" in ARTEFACT_PATTERNS
    assert "*.egg-info" in ARTEFACT_PATTERNS
    assert "__pycache__" in ARTEFACT_PATTERNS
    matched = artefact_matches(
        [
            "build/x",
            "a/b/dist/c",
            ".venv/py",
            "p.egg-info/PKG-INFO",
            "src/dist_helper.py",
            "dist_utils/x.py",
            "tools/build.py",
            "build.py",
            "distribution/x",
            "venv/x",
        ]
    )
    assert matched == ["build/x", "a/b/dist/c", ".venv/py", "p.egg-info/PKG-INFO"]


def test_note_body_is_capped():
    paths = [f"build/lib/m{i}.py" for i in range(ARTEFACT_NOTE_PATH_CAP + 10)]
    body = artefact_note_body(paths)
    assert f"build/lib/m{ARTEFACT_NOTE_PATH_CAP - 1}.py" in body
    assert f"build/lib/m{ARTEFACT_NOTE_PATH_CAP}.py" not in body
    assert f"(+10 more)" in body


# --- the runner's warning note ---------------------------------------------


def test_artefact_note_writer_records_a_fubala_note(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'notes.db'}", connect_args={"check_same_thread": False}
    )
    init_db(engine)
    with session_scope(engine) as db:
        db.add(Story(id="n1", title="note story"))

    runner = StoryRunner(engine, gateway=None, registry=None)  # type: ignore[arg-type]
    _artefact_note_writer(runner, "n1")(["build/lib/a.py", "dist/app.js"])

    with session_scope(engine) as db:
        notes = list(db.exec(select(StoryNote).where(StoryNote.story_id == "n1")))
        assert len(notes) == 1
        note = notes[0]
        # read while still bound: session_scope expires instances on commit
        assert note.author == "fubala"
        assert "build/lib/a.py" in note.body and "dist/app.js" in note.body
        assert "pre-commit guard" in note.body

    # append-only: a second round adds a note, never replaces one
    _artefact_note_writer(runner, "n1")([".venv/bin/py"])
    with session_scope(engine) as db:
        assert len(list(db.exec(select(StoryNote).where(StoryNote.story_id == "n1")))) == 2


def test_artefact_note_writer_survives_a_missing_story(tmp_path):
    """A broken DB write must never take the commit down with it."""
    engine = create_engine(
        f"sqlite:///{tmp_path / 'notes.db'}", connect_args={"check_same_thread": False}
    )
    init_db(engine)
    runner = StoryRunner(engine, gateway=None, registry=None)  # type: ignore[arg-type]
    _artefact_note_writer(runner, "no-such-story")(["build/x"])  # FK violation, must not raise
