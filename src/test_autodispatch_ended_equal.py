"""`dead_spawns` scans the events once for ended spawns; the answer for each
spawn must equal the per-spawn scan it replaced."""
import random
import unittest

import autodispatch

TYPES = autodispatch.OWN_TERMINAL_TYPES + ("spawn-started", "spawn-stale", "lesson", None)


def _former(events, spawn):
    return any(e.get("spawn") == spawn and e.get("type") in
               ("verdict", "review", "build-done", "spawn-done", "spawn-failed") for e in events)


class EndedEqual(unittest.TestCase):
    def test_same_answer_as_per_spawn_scan(self):
        rnd = random.Random(1105)
        spawns = [f"L-builder-99{i:02d}" for i in range(12)] + [None]
        for _ in range(300):
            events = [dict(spawn=rnd.choice(spawns), type=rnd.choice(TYPES))
                      for _ in range(rnd.randrange(30))]
            ended = autodispatch._ended_spawns(events)
            for spawn in spawns[:-1]:
                self.assertEqual(spawn in ended, _former(events, spawn))


if __name__ == "__main__":
    unittest.main()
