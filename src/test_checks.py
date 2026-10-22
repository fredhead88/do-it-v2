#!/usr/bin/env python3
"""Hermetic regression fixtures for the check registry (L-spec-1105)."""
import builtins
import importlib
import os
import pathlib
import re
import socket
import subprocess
import sys
import tempfile
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))


def forbidden(*args, **kwargs):
    raise AssertionError("external action from checks")


# Import must not open anything under the operator's state directories.
_open = builtins.open
_io_open = __import__("io").open


def guarded_open(original):
    def guard(path, *args, **kwargs):
        if not isinstance(path, int):
            value = os.fspath(path)
            assert ".do-it" not in value and "registry-import-state" not in value, value
        return original(path, *args, **kwargs)
    return guard


with patch.dict(os.environ, {"DOIT_ROOT": "/registry-import-state"}), \
        patch("builtins.open", guarded_open(_open)), \
        patch("io.open", guarded_open(_io_open)), \
        patch.object(socket, "socket", forbidden), \
        patch.object(subprocess, "run", forbidden), \
        patch.object(subprocess, "Popen", forbidden), patch.object(os, "system", forbidden):
    import checks
    import grading_env
    importlib.reload(checks)


def spec(*criteria, verification=""):
    return ("## Acceptance Criteria\n" + "\n".join(criteria)
            + "\n## Verification\n```bash\n" + verification + "\n```\n")


def record(text):
    return checks.records(spec(text), None)[0]


def test_ac1():
    assert checks.NEEDS == ("repo", "node_modules", "scratch-pg", "ro-prod-sql",
                            "droplet-read", "deployed-site", "browser-review-account",
                            "elapsed-time", "operator")
    text = spec("AC1 [backend]: executable.\nreview_path: run `pytest src/test_x.py`",
                "AC2 [observed-data]: production.", "AC3 [ui]: page.",
                "AC4 [backend]: judge the wording.")
    rows = checks.records(text, None)
    assert [r["criterion"] for r in rows] == ["AC1", "AC2", "AC3", "AC4"]
    assert [r["runner"] for r in rows] == ["build", "prod", "prod-browser", "judgment"]
    for row in rows:
        assert set(row) == {"criterion", "proves", "needs", "timing", "validity_h", "runner", "commands"}
        assert set(row["needs"]) <= set(checks.NEEDS)
        assert row["timing"] in ("pre-merge", "post-deploy", "scheduled")
        assert 0 < len(row["proves"]) <= 200
    assert len(record("AC1 [backend]: " + "long " * 100)["proves"]) <= 200
    unknown = checks.record_for_owed({"criterion": "AC1", "capability": "unknown"}, text, None)
    assert unknown["needs"] == ["repo"]


def test_ac2():
    for command in ("pytest src/test_x.py", "python -m pytest", "./test.sh x", "/usr/bin/test -f x"):
        assert record(f"AC1 [backend]: x.\nreview_path: run `{command}`")["commands"] == [command]
    for value in ("read the diff.", "run the Verification command", "pytest", "ro-sql query",
                  "droplet-read status", "portal-e2e page", "`read the diff` then `pytest x`"):
        assert record(f"AC1 [backend]: x.\nreview_path: {value}")["runner"] != "build", value
    assert record("AC1 [backend]: x.\nreview_path: run pytest x / worked if green")["commands"] == ["pytest x"]
    assert record("AC1 [backend]: x.\nreview_path: run pytest x; worked if green")["commands"] == ["pytest x"]
    text = spec("AC1 [backend]: first. review_path: `pytest one`",
                "AC2 [observed-data]: second.\nreview_path: run `pytest two`",
                "AC3 [backend]: third.",
                verification="# AC1\npytest two &&\n# AC1\npytest one &&\n# AC2\npytest two &&\n# AC3\n/usr/bin/test -f x")
    a, b, c = checks.records(text, None)
    assert a["commands"] == ["pytest two", "pytest one"]
    assert b["runner"] == "prod" and b["commands"] == ["pytest two"]
    assert b["needs"] == ["repo", "ro-prod-sql"]
    assert c["commands"] == ["/usr/bin/test -f x"]
    assert checks.records(spec("AC1 [backend]: x.", verification="pytest x # AC1"), None)[0]["commands"] == ["pytest x"]
    for verification in ("cd r &&\n# AC3\n/usr/bin/test -f x",
                         "/usr/bin/test -f x && # AC3\n/usr/bin/true"):
        command = "/usr/bin/true" if "true" in verification else "/usr/bin/test -f x"
        assert checks.records(spec("AC3 [backend]: x.", verification=verification), None)[0]["commands"] == [command]
    first = spec("AC1 [backend]: first.", "AC2 [backend]: second.",
                 verification="# AC2 AC1\npytest two")
    assert [row["commands"] for row in checks.records(first, None)] == [[], ["pytest two"]]
    for prefix in ("", "./scripts/sweep/", "/home/albert/do-it-v2/scripts/sweep/", "/elsewhere/"):
        for wrapper in ("ro-sql", "droplet-read", "portal-e2e", "keepa-fetch", "scrapingbee-fetch"):
            command = prefix + wrapper + " x"
            runner = "prod" if wrapper in ("ro-sql", "droplet-read") else "judgment"
            for text in (spec(f"AC1 [backend]: x.\nreview_path: `{command}`"),
                         spec("AC1 [backend]: x.", verification=f"# AC1\n{command}")):
                row = checks.records(text, None)[0]
                assert row["runner"] == runner and row["commands"] == [], row
    assert record('AC1 [observed-data]: x.\nreview_path: `/home/albert/do-it-v2/scripts/sweep/ro-sql "select 1"`')["needs"] == ["ro-prod-sql"]


def test_ac3():
    with tempfile.TemporaryDirectory() as td:
        root = pathlib.Path(td)
        (root / "look.toml").write_text('[[prod]]\nproject="fixture"\nbase_url="https://prod.fixture.test"\nssh_target="albert@192.0.2.8"\n')
        with patch.object(grading_env, "DOIT_SRC", root):
            cases = (("[observed-data]: no command", "prod", "deployed-site"),
                     ("[backend]: ro-sql query", "prod", "ro-prod-sql"),
                     ("[backend]: droplet-read status", "prod", "droplet-read"),
                     ("[backend]: curl https://prod.fixture.test/health", "prod", "deployed-site"),
                     ("[backend]: curl http://192.0.2.8/health", "prod", "deployed-site"),
                     ("[backend]: curl https://example.com", "judgment", "repo"),
                     ("[backend]: curl https://prod.fixture.test.evil", "judgment", "repo"),
                     ("[ui]: page", "prod-browser", "browser-review-account"),
                     ("[browser]: page", "prod-browser", "browser-review-account"),
                     ("[backend, ui]: page", "prod-browser", "browser-review-account"),
                     ("[backend]: prose", "judgment", "repo"))
            for clause, runner, need in cases:
                row = checks.records(spec("AC1 " + clause), "fixture")[0]
                assert row["runner"] == runner and need in row["needs"], row
            curl = spec("AC1 [backend]: curl https://prod.fixture.test")
            for project in (None, "missing"):
                assert checks.records(curl, project)[0]["runner"] == "judgment"
            (root / "look.toml").write_text('[[prod]]\nproject="fixture"\nbase_url="http://127.0.0.1:8000"\nssh_target="albert@named.fixture.test"\n')
            for host in ("127.0.0.1:8000", "localhost", "[::1]", "example.com"):
                row = checks.records(spec(f"AC1 [backend]: curl http://{host}/health"), "fixture")[0]
                assert row["runner"] == "judgment", row
            row = checks.records(spec("AC1 [backend]: curl http://named.fixture.test/health"), "fixture")[0]
            assert row["runner"] == "prod" and row["needs"] == ["deployed-site"]
            (root / "look.toml").unlink()
            assert checks.records(curl, "fixture")[0]["runner"] == "judgment"
    assert record("AC1 [ui]: ro-sql query")["runner"] == "prod"


def test_ac4():
    assert record("AC1 [backend]: x.\nreview_path: `pytest x`")["needs"] == ["repo"]
    for word in ("vitest", "npm", "npx", "node"):
        row = record(f"AC1 [backend]: x.\nreview_path: `{word} test`")
        assert row["needs"] == ["repo", "node_modules"]
        assert row["timing"] == "pre-merge" and row["validity_h"] is None
    for word in ("private-pg", "pg_scratch", "psql", "alembic"):
        row = record(f"AC1 [backend]: x.\nreview_path: `bash {word}`")
        assert row["needs"] == ["repo", "scratch-pg"]
    assert record("AC1 [backend]: judge.")["commands"] == []
    assert record("AC1 [backend]: judge.")["needs"] == ["repo"]
    assert record("AC1 [observed-data]: ro-sql and droplet-read")["needs"] == ["ro-prod-sql", "droplet-read"]
    assert record("AC1 [ui]: page")["needs"] == ["deployed-site", "browser-review-account"]
    for window, hours in (("2 hours", 2), ("3 h", 3), ("4 days", 96), ("5 d", 120), ("the 14 days", 336)):
        row = record(f"AC1 [observed-data]: within {window}.")
        assert row["timing"] == "scheduled" and row["validity_h"] == hours, row
    for phrase in ("elapsed", "elapsed-time", "elapsed  time", "elapsed\ntime"):
        row = record(f"AC1 [observed-data]: {phrase}.")
        assert row["needs"] == ["deployed-site"] and row["timing"] == "post-deploy", row
    for tag in ("backend", "observed-data", "ui"):
        row = record(f"AC1 [{tag}]: ELAPSED TIME.")
        assert "elapsed-time" in row["needs"] and row["timing"] == "scheduled" and row["validity_h"] == 24
    row = record("AC1 [backend]: within 6 hours, then within 2 days.\nreview_path: `pytest x`")
    assert row["runner"] == "build" and row["needs"] == ["repo", "elapsed-time"]
    assert row["timing"] == "scheduled" and row["validity_h"] == 6
    ac11 = ("AC11 [observed-data]: Owed, elapsed time after ship, within 14 days of the ship instant. "
            "Over every `spec-written` event after this spec's `shipped` event and inside that window, "
            "the latest spec file for each subject, `checks.coverage` summed over all those specs "
            "gives `sum(mapped) / sum(criteria) >= 0.80` (charter M6; the probe's strict reading "
            "of old specs was 57.0%). review_path: log in as albert on the builder box (read only) "
            "/ go to the ledger and `content/L-spec-NNNN.md` for the 14 days after ship "
            "/ do list the spec-written subjects after the ship instant, call `checks.coverage` "
            "on each file with its event's project, and sum / worked if the summed share is at least "
            "0.80 and the per-spec CK-001 lines name the remainder / failed if the share is under 0.80.")
    row = record(ac11)
    assert row["runner"] == "prod" and row["needs"] == ["deployed-site", "elapsed-time"]
    assert row["timing"] == "scheduled" and row["validity_h"] == 336, row
    row = record("AC1 [observed-data]: deployed.")
    assert row["timing"] == "post-deploy" and row["validity_h"] == 24


def test_ac5():
    text = spec("**AC1 [ui]: bold page.**", "AC2 [backend]: no review path.",
                verification="pytest untagged")
    assert checks.records(text, None) == checks.records(text, None)
    assert len(checks.records(text, None)) == len(grading_env._criterion_blocks(text)) == 2
    assert checks.records(text, None)[1]["runner"] == "judgment"
    assert checks.records(text, None)[1]["commands"] == []
    for empty in ("", "# no criteria"):
        assert checks.records(empty, None) == []


def test_ac6():
    text = spec("AC1 [backend]: command within 2 days.\nreview_path: `pytest one`",
                "AC2 [ui]: browser.", "AC3 [backend]: prose.")
    cases = {"ro-dsn": (["ro-prod-sql"], "prod"),
             "droplet": (["droplet-read"], "prod"),
             "browser": (["deployed-site", "browser-review-account"], "prod-browser"),
             "elapsed": (["elapsed-time"], "judgment"),
             "not-live": (["deployed-site"], "prod"),
             "repo": (["repo"], "build"), "other": (["repo"], "build"),
             "operator-action": (["operator"], "judgment"),
             "alien": (["repo"], "judgment")}
    for capability, (needs, runner) in cases.items():
        row = checks.record_for_owed({"criterion": "AC1", "capability": capability}, text, None)
        assert row["needs"] == needs and row["runner"] == runner, row
        assert row["criterion"] == "AC1" and row["commands"] == ["pytest one"]
        assert row["timing"] == ("scheduled" if capability == "elapsed" else "post-deploy")
        assert row["validity_h"] == 48
    for word, need in (("ro-dsn", "ro-prod-sql"), ("droplet", "droplet-read"),
                       ("browser", "browser-review-account"), ("elapsed", "elapsed-time"),
                       ("deploy", "deployed-site")):
        row = checks.record_for_owed({"criterion": "AC1", "line": f"requires {word}"}, text, None)
        assert need in row["needs"]
    row = checks.record_for_owed({"criterion": "AC2", "capability": None, "line": None}, text, None)
    assert row["runner"] == "prod-browser"
    row = checks.record_for_owed({"criterion": "AC1", "capability": "browser", "line": "elapsed"}, text, None)
    assert row["needs"] == ["deployed-site", "browser-review-account", "elapsed-time"]
    assert row["timing"] == "scheduled" and row["validity_h"] == 48
    row = checks.record_for_owed({"criterion": "AC2", "capability": "browser", "line": "elapsed-time"}, text, None)
    assert row["needs"] == ["deployed-site", "browser-review-account", "elapsed-time"]
    assert row["timing"] == "scheduled"
    row = checks.record_for_owed({"criterion": "AC1", "line": "operator-action browser ro-dsn elapsed"}, text, None)
    assert row["runner"] == "prod-browser" and row["timing"] == "scheduled"
    for data in ({"criterion": "AC99", "capability": "browser"},
                 {"criterion": "AC3", "capability": "alien", "line": "browser elapsed"}):
        row = checks.record_for_owed(data, text, None)
        assert row["runner"] == "judgment" and row["needs"] == ["repo"]
    assert checks.record_for_owed({"criterion": "AC3", "capability": "repo"}, text, None)["runner"] == "judgment"


def test_ac7():
    criteria = [f"AC{i} [backend]: prose." if i in (3, 7) else f"AC{i} [ui]: page." for i in range(1, 11)]
    expected = {"mapped": 8, "judgment": ["AC3", "AC7"], "share": 0.8, "verification_only": 0}
    for verification in ("", "cd r &&\n/usr/bin/echo VERIFY_OK", "cd r && true",
                         "# AC1\n/usr/bin/python3 src/test_x.py"):
        assert checks.coverage(spec(*criteria, verification=verification), None) == expected
    assert checks.coverage("\n".join(criteria), None) == expected
    text = spec(*criteria, verification="cd r &&\n/usr/bin/python3 src/test_x.py &&\necho VERIFY_OK")
    assert checks.coverage(text, None) == dict(expected, verification_only=2)
    assert checks.coverage("", None) == {"mapped": 0, "judgment": [], "share": 0.0, "verification_only": 0}
    assert isinstance(checks.coverage("", None)["share"], float)
    assert type(checks.coverage(text, None)["verification_only"]) is int


def template_example():
    text = (pathlib.Path(__file__).resolve().parent.parent / "agents/spec-writer.md").read_text()
    acceptance = text.split("## Acceptance criteria\n", 1)[1].split("\n## ", 1)[0]
    example = acceptance.split("```bash\n", 1)[1].split("```", 1)[0]
    ids = list(dict.fromkeys(re.findall(r"# (AC\d+)", example)))
    return text, acceptance, example, ids


def test_ac9():
    text, acceptance, example, ids = template_example()
    assert "CK-001" in acceptance and "# AC<n>" in acceptance
    assert len(ids) >= 2
    assert all(re.fullmatch(r"# AC\d+", line) for line in example.splitlines() if "# AC" in line)
    assert "next segment" in acceptance and "next statement" in acceptance
    fixture = spec(*(f"{cid} [backend]: fixture." for cid in ids), verification=example)
    assert checks.coverage(fixture, None)["judgment"] == []
    assert [h for h in text.splitlines() if h.startswith("## ")][-1].startswith("## Lessons")


def test_ac12():
    _, _, example, ids = template_example()
    fixture = spec(*(f"{cid} [backend]: fixture." for cid in ids), verification=example)
    statements = [line.removesuffix(" &&") for line in example.splitlines()
                  if line.strip() and not line.startswith("#")]
    assert len(statements) == len(ids)
    texts = grading_env._tool_texts(fixture, "")
    rows = checks.records(fixture, None)
    caps = grading_env.criterion_caps(fixture, "", "fixture")
    for cid, statement, row in zip(ids, statements, rows):
        assert statement in texts[cid]
        assert all(other not in texts[cid] for other in statements if other != statement)
        assert row["criterion"] == cid and row["commands"] == [statement]
        alone = grading_env.criterion_caps("", statement, "fixture")
        assert caps.get(cid, []) == alone.get("verify", [])
    assert caps[ids[0]] != caps[ids[1]], "example commands must distinguish capabilities"
    cmd1, cmd2 = "/usr/bin/python3 first.py", "/usr/bin/node second.js"
    trailing = spec("AC1 [backend]: first.", "AC2 [backend]: second.",
                    verification=f"{cmd1} && # AC1\n{cmd2}")
    texts = grading_env._tool_texts(trailing, "")
    assert cmd2 in texts["AC1"] and cmd1 not in texts["AC1"]
    assert cmd1 in texts["verify"] and cmd2 not in texts["verify"]
    assert [row["commands"] for row in checks.records(trailing, None)] == [[cmd2], []]
    caps = grading_env.criterion_caps(trailing, "", "fixture")
    alone = grading_env.criterion_caps("", cmd2, "fixture")
    assert caps.get("AC1", []) == alone.get("verify", [])
    assert "node_modules" in caps["AC1"] and "AC2" not in caps


def test_ac10():
    with patch.object(socket, "socket", forbidden), patch.object(subprocess, "run", forbidden), \
            patch.object(subprocess, "Popen", forbidden), patch.object(os, "system", forbidden):
        text = spec("AC1 [backend]: x.\nreview_path: `pytest x`", "AC2 [observed-data]: curl https://example.com")
        assert len(checks.records(text, "fixture")) == 2
        assert checks.record_for_owed({"criterion": "AC1"}, text, None)
        assert checks.coverage(text, None)["mapped"] == 2
    root = pathlib.Path(__file__).resolve().parent.parent
    for path in ("src/checks.py", "src/test_checks.py", "src/shape.py", "src/test_shape.py", "agents/spec-writer.md"):
        assert len((root / path).read_text().splitlines()) <= 1500, path


def test_live_db_routing():
    cases = (
        "AC3 [observed-data]: fixed window contains ASIN rows.\n"
        "review_path: run `python -m pytest tests/test_1026.py -m live_db`",
        "AC3 [observed-data]: rows exist.\nreview_path: `pytest tests/test_window.py`",
        "AC3 [backend, observed-data]: rows exist.\nreview_path: `pytest tests/test_window.py`",
        "AC3 [backend]: rows exist.\nreview_path: `pytest tests/test_window.py -m 'live_db and slow'`",
        "AC3 [backend]: rows exist.\nreview_path: `pytest tests/test_window.py -m=live_db`",
        "AC3 [backend]: rows exist.\nreview_path: production: run `pytest tests/test_window.py`",
        "AC3 [backend]: rows exist. review_path: production: run `pytest tests/test_window.py`",
        "AC3 [backend]: rows exist.\nreview_path: `pytest tests/test_production_window.py`",
    )
    for block in cases:
        text = spec(block)
        own = checks.records(text, None)[0]
        assert own["runner"] == "prod" and own["needs"] == ["repo", "ro-prod-sql"], own
        for capability in (None, "repo", "other", "elapsed", "browser", "unknown"):
            row = checks.record_for_owed({"criterion": "AC3", "capability": capability}, text, None)
            assert row["runner"] == "prod" and "ro-prod-sql" in row["needs"], row
            assert "scratch-pg" not in row["needs"]
    ordinary = record("AC1 [backend]: schema.\nreview_path: `pytest tests/test_product.py`")
    assert ordinary["runner"] == "build" and "ro-prod-sql" not in ordinary["needs"]
    tagged = spec("AC3 [backend]: rows exist.", verification="# AC3\npytest tests/test_window.py -m live_db")
    assert checks.records(tagged, None)[0]["needs"] == ["repo", "ro-prod-sql"]
    print("W55 live-data routes override legacy capabilities; backend checks stay private")


if __name__ == "__main__":
    test_live_db_routing()
    for ac in (1, 2, 3, 4, 5, 6, 7, 9, 10, 12):
        globals()[f"test_ac{ac}"]()
        print(f"AC{ac} ok")
