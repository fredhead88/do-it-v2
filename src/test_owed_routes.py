#!/usr/bin/env python3
"""Pure route vocabulary and readiness checks for L-spec-1003."""
import ast
from datetime import datetime, timedelta, timezone
import pathlib
import sys

import grading_env
import owed_routes

assert owed_routes.MAX_ATTEMPTS == 2
assert owed_routes.ROUTES == {"ro-dsn": "plain", "not-live": "plain", "droplet": "plain",
                              "elapsed": "plain", "repo": "repo", "browser": "browser",
                              "operator-action": None}
for capability in owed_routes.ROUTES:
    assert owed_routes.route_for(capability) == capability
assert owed_routes.route_for("other") == "repo"
assert owed_routes.route_for("zzz") is None
assert owed_routes.route_for(None) is None
for value, expected in (("git", "repo"), ("view-paths", "repo"), ("node_modules", "repo"),
                        ("unknown", "repo"), ("browser", "browser"), ("db", "ro-dsn"),
                        ("deploy", "not-live"), (None, "repo"), ("new-capability", "repo")):
    assert owed_routes.from_grader(value) == expected
assert all(owed_routes.from_grader(v) in owed_routes.ROUTES for v in grading_env.GRADER_NEVER)
assert owed_routes.from_grader("browser") == "browser"

now = datetime(2026, 10, 15, tzinfo=timezone.utc)
last = now - timedelta(hours=12)
for cap in ("ro-dsn", "not-live"):
    for timestamp in (last, last.isoformat()):
        row = {"capability": cap, "last_unobservable_at": timestamp}
        assert owed_routes.ready(row, now, {"deploy": now.isoformat()})
        for epochs in ({}, {"deploy": None}, {"deploy": last.isoformat()},
                       {"deploy": (last - timedelta(seconds=1)).isoformat()}, {"deploy": "bad"}):
            assert not owed_routes.ready(row, now, epochs), (row, epochs)
    assert not owed_routes.ready({"capability": cap}, now, {"deploy": now.isoformat()})
for previous, epoch, expected in ((None, "new", True), ("old", "new", True),
                                   ("same", "same", False), ("old", None, False)):
    assert owed_routes.ready({"capability": "droplet", "last_route_epoch": previous}, now,
                             {"droplet-wrapper": epoch}) == expected
assert not owed_routes.ready({"capability": "droplet"}, now, {})
for age, expected in ((timedelta(hours=12), True), (timedelta(hours=13), True),
                      (timedelta(hours=12) - timedelta(seconds=1), False)):
    assert owed_routes.ready({"capability": "elapsed", "last_unobservable_at": now - age}, now, {}) == expected
assert not owed_routes.ready({"capability": "elapsed", "last_unobservable_at": None}, now, {})
assert not owed_routes.ready({"capability": "elapsed"}, now, {})
for cap, expected in (("repo", True), ("other", True), ("browser", True),
                      ("operator-action", False), ("zzz", False), (None, False)):
    assert owed_routes.ready({"capability": cap}, now, {}) == expected
print("owed-lifecycle AC4 ok")

module = ast.parse(pathlib.Path(owed_routes.__file__).read_text())
for node in ast.walk(module):
    if isinstance(node, ast.Import):
        assert all(n.name.split(".")[0] in sys.stdlib_module_names for n in node.names)
    if isinstance(node, ast.ImportFrom):
        assert node.level == 0 and node.module.split(".")[0] in sys.stdlib_module_names
print("owed-lifecycle AC5 ok")
