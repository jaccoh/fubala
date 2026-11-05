import json
from types import SimpleNamespace

from sqlalchemy import create_engine
from sqlmodel import Session, SQLModel

from app import runner as runner_mod
from app.models import Story, StoryStatus
from app.verify_counts import count_mismatch, parse_summaries, trim_output


def test_parse_ignores_log_text_and_reads_real_lines():
    assert parse_summaries("3 failed attempts in 2s of work\nretry 5 passed") is None
    out = "\x1b[31m=== 3 failed, 10 passed, 2 warnings in 1.5s (0:00:01) ===\x1b[0m\n"
    c = parse_summaries(out)
    assert c["lanes"] == [{"failed": 3, "passed": 10, "warnings": 2}]
    assert c["ran"] == 13
    assert parse_summaries("12 passed in 0.31s\n")["ran"] == 12


def test_three_lanes_survive_long_output():
    lanes = ["10 passed in 1.0s", "5 failed, 2 passed in 2.0s", "1 error in 0.5s"]
    filler = "x" * 8000 + "\n"
    out = "head\n" + filler + lanes[0] + "\n" + filler + lanes[1] + "\n" + filler
    out += lanes[2] + "\n" + filler + "tail\n"
    assert len(out) > 20_000
    trimmed = trim_output(out)
    for line in lanes:
        assert line in trimmed
    assert "chars omitted" in trimmed
    assert "tail" in trimmed and trimmed.startswith("head")
    counts = parse_summaries(out)
    assert len(counts["lanes"]) == 3
    assert counts["totals"]["failed"] == 5 and counts["totals"]["errors"] == 1


def test_all_middle_lines_and_short_output():
    assert trim_output("short") == "short"
    a, b, c = "1 passed in 1.0s", "2 passed in 1.0s", "3 passed in 1.0s"
    out = "h" * 1600 + "\n" + "\n".join(f"{x}\n{'y' * 3000}" for x in (a, b, c)) + "\n" + "t" * 3000
    t = trim_output(out)
    assert all(x in t for x in (a, b, c))


def test_count_mismatch():
    assert count_mismatch(0, parse_summaries("3 failed, 10 passed in 1.0s")) == (
        "exit 0 but 3 failed"
    )
    assert count_mismatch(0, parse_summaries("3 failed, 1 error, 2 passed in 1s")) == (
        "exit 0 but 3 failed, 1 errors"
    )
    assert count_mismatch(1, parse_summaries("3 failed in 1.0s")) is None
    assert count_mismatch(0, parse_summaries("5 passed in 1.0s")) is None
    assert count_mismatch(0, None) is None


def _baseline(ran):
    engine = create_engine("sqlite://")
    SQLModel.metadata.create_all(engine)
    cmd = "pytest"
    with Session(engine) as db:
        db.add(
            Story(
                id="old",
                title="old",
                status=StoryStatus.DONE,
                verify_result=json.dumps(
                    {"passed": True, "command": cmd, "counts": {"ran": 1753}}
                ),
            )
        )
        db.add(Story(id="new", title="new", status=StoryStatus.EXECUTING))
        db.commit()
    fake = SimpleNamespace(engine=engine)
    return runner_mod.StoryRunner._verify_baseline_flag(fake, "new", cmd, ran, 2.0, "/nonexistent")


def test_baseline_flags_clear_drop_only():
    flag = _baseline(1500)
    assert flag == {"main_ran": 1753, "flag": "ran 1500 tests, main ran 1753"}
    assert _baseline(1800) is None
