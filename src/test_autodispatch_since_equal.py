"""`_since` keeps its answer after the incremental sibling replay (Thinker 2026-11-05)."""
import datetime as dt
import pathlib
import random
import unittest
from unittest import mock

import autodispatch
import dispatch
import fold


def replay(events, sid, charter, wave):
    """The former replay: re-run `wave_blocker` on every sibling-event prefix."""
    siblings = {e["subject"] for e in events if e["subject"] != sid and e.get("charter")
                and pathlib.Path(e["charter"]).stem == charter}
    was_blocked, settle_ts, prefix = False, None, []
    for e in sorted(events, key=lambda e: str(e.get("ts", ""))):
        if e["subject"] not in siblings:
            continue
        prefix.append(e)
        now_blocked = dispatch.wave_blocker(prefix, charter, sid, wave) is not None
        if was_blocked and not now_blocked:
            settle_ts = fold.ts(e["ts"])
        was_blocked = now_blocked
    return settle_ts


def ledger(rng):
    events = []
    for n in range(rng.randint(1, 7)):
        sid = f"L-spec-{n}"
        charter = rng.choice(["L-charter-1", "L-charter-1", "L-charter-2"])
        for step in range(rng.randint(1, 6)):
            event = {"subject": sid, "ts": f"2026-11-05T{rng.randint(0, 3):02d}:{rng.randint(0, 59):02d}:00+00:00",
                     "type": rng.choice(["spec-written", "build-started", "shipped", "killed", "decision"])}
            if rng.random() < .8:
                event["charter"] = charter
            if event["type"] == "spec-written":
                event["footprint"] = [str(rng.randint(1, 3))]
            events.append(event)
    return events


def state(evs, _):
    last = max(evs, key=lambda e: e["ts"])["type"]
    return "shipped" if last in ("shipped", "killed") else "written"


class SinceEqual(unittest.TestCase):
    def test_matches_former_replay(self):
        rng = random.Random(1105)
        settled = 0
        with mock.patch.object(dispatch, "spec_wave", lambda charter, fp: int(fp[0])), \
                mock.patch.object(fold, "spec_state", state), \
                mock.patch.object(dispatch, "SETTLED_STATES", {"shipped"}):
            for _ in range(600):
                events = ledger(rng)
                sid, wave = "L-spec-0", rng.randint(1, 3)
                evs = [e for e in events if e["subject"] == sid]
                want = replay(events, sid, "L-charter-1", wave)
                settled += want is not None
                with mock.patch.object(autodispatch, "_newest_marker_ts", lambda _: None):
                    got = autodispatch._since(events, sid, evs, "L-charter-1", wave)
                self.assertEqual(got, want)
        self.assertGreater(settled, 20)


if __name__ == "__main__":
    unittest.main()
