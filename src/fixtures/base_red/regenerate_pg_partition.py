#!/usr/bin/env python3
"""Capture real partitioned pytest replay using a landed producer, without PG."""
import argparse
import contextlib
import hashlib
import importlib
import io
import json
import os
from pathlib import Path
import re
import subprocess
import sys
import tempfile


SOURCES = {
    "tests/test_partition_a.py": (
        "def test_partition_a():\n"
        "    assert False, 'intentional partition A failure'\n"),
    "tests/test_partition_b.py": (
        "def test_partition_b():\n"
        "    assert False, 'intentional partition B failure'\n"),
}
WEIGHTS = {name: weight for name, weight in zip(SOURCES, (2, 1))}
PRODUCER = "scripts/ci/pg_scratch_parallel.py"


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def inventory(text, groups):
    """Compare only short summaries; paths/banners/durations are incidental."""
    rows = []
    for raw in text.splitlines():
        line = re.sub(r"\x1b\[[0-9;]*m", "", raw).split("\t")[-1]
        line = re.sub(r"^\d{4}-\d\d-\d\dT\S+\s+", "", line.strip())
        match = re.match(r"^(FAILED|ERROR)\s+(\S+\.py::\S+)(?:\s+-\s+.*)?$", line)
        if match:
            kind, node = match.groups()
            for index, group in enumerate(groups, 1):
                for file in group:
                    if node.split("::", 1)[0].endswith(file):
                        node = file + "::" + node.split("::", 1)[1]
                        rows.append(dict(group=index, kind=kind, node=node))
    return rows


def generate(checkout, sha, output):
    def git(*args):
        return subprocess.check_output(["git", "-C", str(checkout), *args])
    resolved = git("rev-parse", "--verify", sha + "^{commit}").decode().strip()
    if resolved != sha:
        raise ValueError("producer SHA must be the full landed commit")
    subprocess.run(["git", "-C", str(checkout), "merge-base", "--is-ancestor", sha, "HEAD"],
                   check=True, capture_output=True)
    source = git("show", sha + ":" + PRODUCER)
    output.mkdir(parents=True, exist_ok=False)
    with tempfile.TemporaryDirectory(prefix="partition-producer-") as scratch:
        scratch = Path(scratch)
        producer_root = scratch / "producer"
        producer_root.mkdir()
        archive = scratch / "source.tar"
        archive.write_bytes(git("archive", sha, "scripts"))
        subprocess.run(["tar", "xf", str(archive), "-C", str(producer_root)], check=True)
        assert (producer_root / PRODUCER).read_bytes() == source
        sys.path.insert(0, str(producer_root))
        producer = importlib.import_module("scripts.ci.pg_scratch_parallel")
        root = scratch / "synthetic"
        for name, content in SOURCES.items():
            file = root / name
            file.parent.mkdir(parents=True, exist_ok=True)
            file.write_text(content)
        durations = root / "scripts/ci/shard_durations.json"
        durations.parent.mkdir(parents=True)
        durations.write_text(json.dumps(dict(version=1, files=WEIGHTS)))
        producer.ROOT = root  # Synthetic test root; execution remains the producer's.
        # Replace lifecycle only, including endpoint conversion (amended preflight 7).
        def cluster(action, directory, env):
            if action == "stop":
                return {}
            return dict(AS_LOCAL_PG_ADMIN="inert-B-admin", PG_PARITY_DB_URL="inert-B-parity",
                        PGHOST="inert-B-host", PGPORT="15433", PGUSER="inert-B-user")
        producer._cluster = cluster
        producer._tcp_environment = lambda env: env
        # A fixed allowlist prevents environment secrets and project pytest config
        # from entering the children. No inherited environment dump is recorded.
        os.environ.clear()
        os.environ.update(PATH="/usr/bin:/bin", HOME=str(scratch / "home"),
                          RUNNER_TEMP=str(scratch), TMPDIR=str(scratch),
                          PYTEST_DISABLE_PLUGIN_AUTOLOAD="1", PYTHONHASHSEED="0",
                          AS_LOCAL_PG_ADMIN="inert-A-admin", PG_PARITY_DB_URL="inert-A-parity")
        pytest_command = producer._pytest_command()
        subprocess.run([*pytest_command, "--version"], check=True, capture_output=True)
        groups = producer.partition(list(SOURCES), producer._weights(list(SOURCES)), 2)
        capture = io.StringIO()
        with contextlib.redirect_stdout(capture), contextlib.redirect_stderr(capture):
            status = producer.run(2, list(SOURCES), ["-v", "--tb=short", "-p", "no:cacheprovider"], scratch)
        raw = capture.getvalue().encode()
        summaries = inventory(raw.decode(), groups)
        expected = [file + "::test_partition_" + suffix
                    for file, suffix in zip(SOURCES, ("a", "b"))]
        if status != 1 or sorted(r["node"] for r in summaries) != sorted(expected):
            raise RuntimeError("real producer did not replay both intentional failures")
        provenance = dict(
            producer_sha=sha, producer_path=PRODUCER, producer_source_sha256=digest(source),
            fixture_sha256=digest(raw), synthetic_sources=SOURCES, weights=WEIGHTS,
            groups=groups, expected_nodes=expected, exit_status=status,
            normalized_inventory=summaries,
            generator_argv=["/usr/bin/python3", "src/fixtures/base_red/regenerate_pg_partition.py",
                            "--producer-checkout", "$PG_PARTITION_PRODUCER_CHECKOUT",
                            "--producer-sha", sha, "--output-dir", "<fresh-scratch-dir>"],
            working_directory="do-it-v2 worktree root",
            interpreter=dict(generator=sys.executable, version=sys.version, pytest_command=pytest_command),
            placeholder_variables=["AS_LOCAL_PG_ADMIN", "PG_PARITY_DB_URL"],
            independent_placeholders=True, patched_boundaries=["_cluster", "_tcp_environment"])
        (output / "pg-parity-partitioned.log").write_bytes(raw)
        (output / "pg-parity-partitioned.provenance.json").write_text(
            json.dumps(provenance, indent=2, sort_keys=True) + "\n")
        print(json.dumps(dict(groups=groups, inventory=summaries, exit_status=status), sort_keys=True))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--producer-checkout", type=Path, required=True)
    parser.add_argument("--producer-sha", required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    options = parser.parse_args()
    generate(options.producer_checkout.resolve(), options.producer_sha, options.output_dir.resolve())
