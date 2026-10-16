# R8: stage runner equivalence before enabling self-hosted CI

This is an un-applied rollout plan for charter L-charter-0051. Keep the two
installed runners idle and keep CI on ubuntu-latest until the evidence below
passes. No service, runner registration, workflow, firewall or image has been
changed by this work.

## 1. Capture the hosted reference

Use one immutable candidate SHA for both runner types. Save the hosted job's
“Set up job” image name/version, runner-images commit, action commit pins,
Python/setup-python version, Node version and dependency-lock hashes with the
artifacts. `ubuntu-latest` currently maps to Ubuntu 24.04; record the actual run
rather than assuming that mapping remains constant. Reference:
[GitHub runner-images](https://github.com/actions/runner-images) and
[Ubuntu 24.04 inventory](https://github.com/actions/runner-images/blob/main/images/ubuntu/Ubuntu2404-Readme.md).

Build a staging image from Ubuntu 24.04 and that inventory, preserving two
interpreters: clean `/usr/bin/python3`, and the exact setup-python 3.12 patch
release in a fresh project venv. Match Node/Git versions to the captured run.
Do not mount `/opt`, the orchestration home, a host venv, or host site-packages.
Do not add project site-packages to system Python. Install the project's same
four CI layers into the project interpreter: pipelines lock, requirements-web,
editable `./api[dev]`, requirements-dev. Rebuild the venv per job, using only a
wheel/download cache keyed by OS, interpreter and lock hashes. Use the same
editable-install cwd and PYTHONPATH as the hosted shard.

The current `python-tests.yml` uses setup-python 3.12 and explicitly `-n 2`.
Keep shard partitioning and plugins identical; historical nproc-based paths
must use the quota wrapper below. Changing existing `-n 2` to more workers is a
separate timing experiment after equivalence passes.

Publish the prepared image to the staging registry only during the later
rollout. Resolve its immutable `name@sha256:...` reference. The launcher rejects
mutable tags. The image must contain bash, Python, Node, Git, required OS libs
and the selected project interpreter; retain its build recipe alongside the
captured hosted inventory. This plan does not assert that such an image has
already been built or validated.

## 2. Put each job in its own loopback namespace

Use `scripts/ci/isolated-job.py`. It defaults to printing JSON argv lists and
never contacts Docker without `--run`. Example plan, substituting a real image
digest when one exists:

```bash
python3 scripts/ci/isolated-job.py --checkout /scratch/candidate \
  --image "$CI_PARITY_IMAGE" -- /ci-tools/run-pytest.sh tests/ api/tests/
```

Only a disposable checkout and the read-only CI helper directory are mounted.
Each execution gets a unique Docker bridge and network namespace, unprivileged
UID, dropped capabilities and no Docker socket. Loopback servers launched by
tests remain usable. Host loopback Postgres and HTTP listeners are outside that
namespace. Outbound downloads/HTTP remain possible through bridge NAT.
Container and bridge cleanup runs after failure and SIGTERM. The runner listener
stays on its original network so it can poll GitHub. Use separate per-job HOME
and database paths; do not forward any host database connection variables.
[Docker bridge reference](https://docs.docker.com/engine/network/drivers/bridge/).

Before installing or starting any job database, run `runner-evidence.py`'s
`probe_namespace()` inside the container: it checks namespace identity, proves
job-local loopback works, and refuses an unexpected listener on 5432. Exercise a
host-only *synthetic* HTTP listener too, using a random port: it must be
unreachable at the same loopback port inside the container while an identical
job-local server must be reachable. No real host database needs to be contacted.

An `IPAddressDeny=localhost` systemd drop-in is an alternative only when the
suite does not start local HTTP/database servers. It would block those too.
Do not put `PrivateNetwork=yes` on the runner listener without an outbound
network design. The per-job namespace avoids both limitations.

## 3. Size workers from quotas

`scripts/ci/cpu-budget.py` reads v2 `cpu.max` or v1 quota/period files, checks all
visible ancestors, floors fractional cores with a minimum of one, and caps by
process affinity. It understands a cgroup subtree mounted as the hierarchy root.
Unreadable/malformed quotas fail the command. A 4-core quota on eight visible
CPUs yields four workers; a tighter parent or affinity wins.
[Kernel cgroup CPU interface](https://docs.kernel.org/admin-guide/cgroup-v2.html).

For every nproc-based pytest command, replace the worker selection with
`workers=$(python3 /ci-tools/cpu-budget.py)` and `-n "$workers"`, or invoke
`run-pytest.sh` with `CI_PROJECT_PYTHON` set to the fresh project interpreter.
The wrapper also exports `CI_WORKERS` and `PYTEST_XDIST_AUTO_NUM_WORKERS`.
Do not prepend the project venv to PATH for tests of bare python3. The container
launcher explicitly carries the host service's effective quota into Docker,
whose daemon otherwise launches outside the runner service's cgroup.

## 4. Compare complete evidence

Capture the test wall clock around each shard with a monotonic timer; do not
substitute JUnit's sum of worker test durations. Preserve every shard's JUnit,
pytest NEW-failure report, complete collection list, skip reasons, stdout and
runner/toolchain manifest. Example after a successful, complete test invocation:

```bash
python3 /ci-tools/runner-evidence.py capture --sha "$CANDIDATE_SHA" \
  --junit /work/shard.xml --wall-seconds "$SHARD_WALL_SECONDS" \
  --private-netns > /work/selfhosted.json
# Hosted capture uses the same arguments without --private-netns.
python3 scripts/ci/runner-evidence.py compare hosted.json selfhosted.json \
  --max-seconds 4200
```

The comparison requires the same SHA, exact toolchain fingerprint, identical
collected test outcomes including skips, no candidate failures, positive finite
wall times, proven candidate namespace isolation and self-hosted wall time below
the gate budget. Empty reports and duplicate testcase IDs fail. Compare each
shard separately and require all expected shard artifacts; an absent shard is
undetermined. Retain and compare NEW sets and skip reasons from the existing
CI baseline/fan-in machinery too. The helper's no-failures rule is deliberately
stricter than subtracting a historical baseline.

First run the ten cited regressions (real-PG connection, fresh process imports,
bare Python, unreachable URL, server capacity and fingerprint), then both full
shards on both runner types. Require a full green window and identical NEW/skip
sets. Only then enable the staging runner label for one window. Roll back the
label if any mismatch, missing evidence, or timeout appears. Keep service
configuration and hosted path available for immediate rollback. No runner
rollout was executed during this burn-down.

## 5. Dispatch timeout and verification

Already implemented at the starting revision: `[window_gate]` in `look.toml`
sets `dispatch_timeout_min = 70`; a positive finite
`PREDEPLOY_GATE_DISPATCH_TIMEOUT_MIN` environment override wins, followed by the
config, followed by a 45-minute fallback. The outer process timeout adds the
existing kill margin. `test_window_gate.py` proves precedence, malformed values,
forwarding and timeout behavior against a fake deploy gate.

Run the hermetic checks locally:

```bash
python3 src/test_runner_parity.py
python3 src/test_window_gate.py
```

They use fixture cgroups, synthetic JUnit, fake Docker subprocesses and local
Git origins. They neither build an image nor start or alter any live runner.
Actual hosted/self-hosted equivalence and the wall-time measure remain rollout
work requiring the paired window evidence above.
