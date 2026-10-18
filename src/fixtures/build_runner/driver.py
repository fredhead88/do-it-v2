"""Test subprocess entry point: replace external services, retain runner logic."""
import os
import pathlib
import sys

FIX = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(FIX.parents[1]))
import build_runner

build_runner.PRIVATE_PG = FIX / "private-pg.sh"
build_runner.grading_env._PG_BIN = FIX
build_runner.WALL_CAP = float(os.environ.pop("FIXTURE_CAP", "30"))
if os.environ.get("FIXTURE_CLASSIFY"):
    build_runner.base_red.classify = lambda *args: {
        "complete": os.environ["FIXTURE_CLASSIFY"] == "complete",
        "pre_existing": ["ci-extra::known"], "new": []}
sys.exit(build_runner.main())
