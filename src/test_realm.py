"""realm.py: the compact row shape, the tail offsets, and a bootstrap over a throwaway root."""
import json, os, pathlib, sys, tempfile
TMP = pathlib.Path(tempfile.mkdtemp())
(TMP / "events").mkdir(); (TMP / "seat").mkdir()
os.environ["DOIT_ROOT"] = str(TMP)
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import realm  # noqa: E402

def w(name, rows):
    (TMP / "events" / name).write_text("".join(json.dumps(r) + "\n" for r in rows))

def test_compact_keeps_pipeline_rows_and_drops_noise():
    e = {"ts": "2026-09-25T05:00:00+00:00", "type": "spawn-done", "subject": "L-spec-0001", "spawn": "L-grader-0007",
         "duration_ms": 61000, "subagent_tokens": 12345, "status": "DONE", "model_used": "claude-sonnet-5"}
    c = realm.compact(e, "grader")
    assert c["r"] == "grader" and c["dur"] == 61000 and c["tok"] == 12345 and c["a"] == "grader"
    assert realm.compact({"ts": "x", "type": "worked"}, "x") is None
    assert realm.compact({"ts": "x", "type": "lesson", "text": "no problem field"}, "builder") is None
    assert realm.compact({"ts": "x", "type": "lesson", "problem": "disk-full", "text": "t"}, "thinker")["pb"] == "disk-full"

def test_bootstrap_and_offsets_over_a_throwaway_root():
    w("L-executor-0001.jsonl", [
        {"ts": "2026-09-25T04:00:00+00:00", "type": "tick", "lane": 3},
        {"ts": "2026-09-25T04:01:00+00:00", "type": "spawn-started", "subject": "L-spec-0001", "spawn": "L-grader-0001", "role": "grader"},
        {"ts": "2026-09-25T04:05:00+00:00", "type": "spawn-failed", "subject": "L-spec-0001", "spawn": "L-grader-0001", "why": "unserved after 300s"}])
    (TMP / "seat" / "L-grader-0001.claimed").touch()
    rows, raw, offsets = realm.read_all()
    assert [r["ty"] for r in rows] == ["tick", "spawn-started", "spawn-failed"]
    assert offsets["L-executor-0001.jsonl"] == (TMP / "events" / "L-executor-0001.jsonl").stat().st_size
    b = realm.bootstrap(0, states=[])
    assert b["n_events"] == 3 and b["stats"]["grader"]["fails"] == {"unserved": 1}
    assert b["events"][1]["cl"]  # claim time joined from the seat file
    # a torn tail is not consumed
    with open(TMP / "events" / "L-executor-0001.jsonl", "a") as fh:
        fh.write('{"ts":"2026-09-25T04:06:00+00:00","type":"tick"')
    _, _, off2 = realm.read_all()
    assert off2["L-executor-0001.jsonl"] == offsets["L-executor-0001.jsonl"]

if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_"):
            fn(); print("ok", name)
