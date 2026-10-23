#!/usr/bin/env python3
"""Hermetic machine comparison and real-dispatcher regression checks."""
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parent))
import machine

CHECKOUT = Path(__file__).resolve().parent.parent
MODELS = '[defaults]\nbackend = "codex"\n'
CRON = 'MAILTO=\n@reboot echo start\n0 * * * * echo hourly\n'


def signed(ac, checks):
    body = json.dumps({"criterion": f"AC{ac}", "checks": checks}, sort_keys=True)
    print(json.dumps(dict(url=f"cli://machine/AC{ac}", status=0,
                          body_sha256=hashlib.sha256(body.encode()).hexdigest(), body_excerpt=body)))
    print(f"machine AC{ac} ok")


def cli(command, repo, mf, cf=None, extra=(), env=None):
    args = [str(CHECKOUT / "doit"), "machine", command, "--repo", str(repo), "--models", str(mf)]
    if cf is not None:
        args += ["--crontab", str(cf)]
    return subprocess.run(args + list(extra), capture_output=True, text=True, env=env)


def test_machine_contract():
    with tempfile.TemporaryDirectory(prefix="machine-test-") as tmp:
        root = Path(tmp)
        repo = root / "repo"
        mirror = repo / "machine"
        mirror.mkdir(parents=True)
        mf, cf = root / "models", root / "cron"

        def pair(m=MODELS, c=CRON):
            (mirror / "models.toml").write_bytes(m.encode())
            (mirror / "crontab.txt").write_bytes(c.encode())

        def check(m=MODELS, c=CRON):
            return machine.check(live_models=m, live_crontab=c, repo=str(repo))

        pair()
        assert check() == []
        for subject, live in (("models.toml", True), ("models.toml", False),
                              ("crontab.txt", True), ("crontab.txt", False)):
            pair()
            m, c = MODELS, CRON
            line = "# extra direction\n"
            if live:
                if subject == "models.toml": m += line
                else: c += line
            else:
                (mirror / subject).write_text((m if subject == "models.toml" else c) + line)
            d = check(m, c)
            assert len(d) == 1 and d[0].kind == "differs" and d[0].subject == subject
            assert ("live-only:" if live else "repo-only:") in d[0].detail and "extra direction" in d[0].detail
        pair()
        assert len(check(MODELS + "# m\n", CRON + "# c\n")) == 2
        assert check(MODELS.replace("\n", "\r\n"))[0].kind == "differs"
        signed(1, "equal; both subjects; both directions; byte equality")

        for m in (None, "", "   ", b"x"):
            d = check(m)
            assert len(d) == 1 and (d[0].kind, d[0].subject) == ("undetermined", "models.toml")
        for c in (None, "", "  ", b"x"):
            d = check(c=c)
            assert len(d) == 1 and (d[0].kind, d[0].subject) == ("undetermined", "crontab.txt")
        for subject in ("models.toml", "crontab.txt"):
            p = mirror / subject
            p.unlink()
            assert [(d.kind, d.subject) for d in check()] == [("undetermined", subject)]
            pair()
            p.write_text("")
            assert [(d.kind, d.subject) for d in check()] == [("undetermined", subject)]
            pair()
            if os.geteuid() != 0:
                p.chmod(0)
                try: assert [(d.kind, d.subject) for d in check()] == [("undetermined", subject)]
                finally: p.chmod(0o600)
        shutil.rmtree(mirror)
        assert len(check()) == 2 and all(d.kind == "undetermined" for d in check())
        mirror.mkdir()
        pair()
        mf.write_text(MODELS)
        cf.write_text(CRON)
        shim = root / "fail-crontab"
        shim.write_text("#!/bin/sh\nexit 1\n")
        shim.chmod(0o700)
        assert cli("check", repo, mf, extra=("--crontab-cmd", str(shim))).returncode == 2
        assert cli("check", repo, mf, cf, extra=("--crontab-cmd", str(shim))).returncode == 2
        signed(2, "invalid live; absent, empty, unreadable repo; failed command; exclusive flags")

        cases = [("models.toml", 'api_key = "' + 'x"\n'),
                 ("models.toml", '[a]\ntoken = "' + 't"\n'),
                 ("models.toml", 'apiKey = "' + 'x"\n'),
                 ("crontab.txt", "OPENAI_API_KEY=" + "abc\n"),
                 ("crontab.txt", "# " + "sk" + "-abc123\n")]
        for subject, text in cases:
            d = check(text if subject == "models.toml" else MODELS,
                      text if subject == "crontab.txt" else CRON)
            assert len(d) == 1 and d[0].kind == "secret"
            (mf if subject == "models.toml" else cf).write_text(text)
            before = {p: p.read_bytes() for p in mirror.iterdir()}
            assert cli("snapshot", repo, mf, cf).returncode == 2
            assert {p: p.read_bytes() for p in mirror.iterdir()} == before
            mf.write_text(MODELS)
            cf.write_text(CRON)
        benign = 'cache_read = 0.1\nkey = ""\n'
        pair(benign, "# keyed comment\n" + CRON)
        assert check(benign, "# keyed comment\n" + CRON) == []
        pair()
        signed(3, "secret refusal without writes; empty values and keyed comment allowed")

        assert cli("check", repo, mf, cf).returncode == 0
        mf.write_text(MODELS + "# changed\n")
        cf.write_text(CRON + "# changed\n")
        p = cli("check", repo, mf, cf)
        assert p.returncode == 1 and len(p.stdout.splitlines()) == 2
        assert cli("check", repo, root / "absent", cf).returncode == 2
        before = {p: p.read_bytes() for p in mirror.iterdir()}
        assert cli("snapshot", repo, root / "absent", cf).returncode == 2
        assert {p: p.read_bytes() for p in mirror.iterdir()} == before
        signed(4, "real dispatcher statuses 0,1,2 and one line per drift")

        fresh = root / "fresh"
        fresh.mkdir()
        subprocess.run(["git", "init", "-q", str(fresh)], check=True)
        subprocess.run(["git", "-C", str(fresh), "-c", "user.name=fixture", "-c", "user.email=f@f",
                        "commit", "--allow-empty", "-qm", "fixture"], check=True)
        def head():
            return subprocess.check_output(["git", "-C", str(fresh), "rev-parse", "HEAD"])
        before = head()
        mf.write_bytes(MODELS.replace("\n", "\r\n").encode())
        assert cli("snapshot", fresh, mf, cf).returncode == 0
        assert head() == before
        assert subprocess.check_output(["git", "-C", str(fresh), "status", "--porcelain"]).decode().strip() == "?? machine/"
        assert (fresh / "machine/models.toml").read_bytes() == mf.read_bytes()
        assert cli("check", fresh, mf, cf).returncode == 0
        cf.write_text(CRON)
        assert cli("check", fresh, mf, cf).returncode == 1
        signed(5, "disk round trip; verbatim CRLF; git HEAD untouched")

        env = dict(os.environ)
        env.pop("DOIT_CANONICAL_REPO", None)
        p = cli("check", fresh, mf, cf, extra=("--gate",), env=env)
        assert p.returncode == 0 and p.stdout.strip() == "machine: skipped (not the canonical checkout)"
        env["DOIT_CANONICAL_REPO"] = str(fresh)
        assert cli("check", fresh, mf, cf, extra=("--gate",), env=env).returncode == 1
        source = (CHECKOUT / "doit").read_text()
        assert source.index('"$SRC/machine.py" check --gate') > source.index('for f in test_*.py')
        assert 'DOIT_ROOT="$DOIT_TEST_MACHINE_ROOT"' in source
        signed(10, "canonical gate red; worktree skip; real root restored after test loop")

        def valid_cron(text):
            macros = {"@reboot", "@daily", "@hourly", "@weekly", "@monthly", "@yearly", "@annually", "@midnight"}
            import re
            for line in text.splitlines():
                line = line.strip()
                if not line or line.startswith("#") or re.match(r"[A-Za-z_][A-Za-z0-9_]*\s*=", line): continue
                fields = line.split()
                if fields[0].startswith("@"):
                    if fields[0] not in macros or len(fields) < 2: return False
                elif len(fields) < 6: return False
            return True
        assert valid_cron(CRON) and not valid_cron("* * * * echo\n")
        m = machine.read_text(CHECKOUT / "machine/models.toml")
        c = machine.read_text(CHECKOUT / "machine/crontab.txt")
        assert m and c and tomllib.loads(m) and valid_cron(c)
        assert machine.check(live_models=m, live_crontab=c, repo=str(CHECKOUT)) == []
        signed(11, "committed baseline parses and is secret-free")

        row = tomllib.loads((CHECKOUT / "retired.d/machine-check.toml").read_text())["retired"]
        assert row == [dict(name="crons-check-as-machine-answer", kind="code",
                            pattern="builder-box cron manifest — measure", paths=["doit"])]
        retirement = root / "retirement"
        retirement.mkdir()
        shutil.copyfile(CHECKOUT / "retired.d/machine-check.toml", retirement / "machine-check.toml")
        p = subprocess.run([str(CHECKOUT / "doit"), "retired", "check", "--root", str(retirement),
                            "--crontab-file", str(cf)], capture_output=True)
        assert p.returncode == 0 and row[0]["pattern"] not in source
        (root / "empty-crond").mkdir()
        p = subprocess.run([str(CHECKOUT / "doit"), "crons", "check", "--crontab-file", str(cf),
                            "--crond-dir", str(root / "empty-crond")], capture_output=True)
        assert p.returncode in (0, 1) and b"unknown command" not in p.stdout + p.stderr
        signed(13, "retired registry green; old pattern gone; manifest dispatcher retained")


if __name__ == "__main__":
    test_machine_contract()
