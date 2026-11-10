#!/usr/bin/env python3
"""test_history — a per-test-file failure history (L-charter-0094 R4, L-spec-2401).

  build(project, *, now=None) -> pathlib.Path

Writes `$DOIT_ROOT/test-evidence/<project>/history.json`:

  {"project", "generated_at",
   "sources": [{"name", "from", "to", "rows"[, "reason"]}],
   "tests":   {"<repo-relative test file>": {"runs", "failures", "last_failure",
                                             "real", "flaky", "unknown"}}}

Sources: six ledger sources read through `fold.read_events`, and CI junit
artifacts read through `ghlimit` (never `wait=True`). Every source's real reach
is stated; a source that could not be read stays in the list with `rows: null`
and a `reason`. This module is a reader: it appends no ledger event and writes
only inside `test-evidence/<project>/`.

  python3 src/test_history.py <project>   prints the written path; exit 1 when
                                          any source is unreadable or partial.
"""
import datetime as dt, io, json, os, pathlib, re, subprocess as _subprocess, sys, zipfile
import xml.etree.ElementTree as ET

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import base_red, fold, ghlimit  # noqa: E402

# Read at call time: the artifact download is the one non-ghlimit.run GitHub
# read (binary), so a test replaces this wholesale. `git` uses `_subprocess`.
subprocess = _subprocess

SOURCES = ("ledger-master-full-run", "ledger-base-red-nightly", "ledger-base-red",
           "ledger-selected-canary", "ledger-verdict", "ledger-rehearsal", "junit-artifacts")
MAX_ARTIFACTS = 200
MAX_BYTES = 8 * 1024 * 1024
MAX_MEMBERS = 100
MAX_PAGES = 10
ARTIFACT_PREFIX = "junit-pytest-"
QUOTA_RC = 75


# ---------------------------------------------------------------- helpers

def _parse(value):
    if isinstance(value, dt.datetime):
        d = value
    else:
        d = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if d.tzinfo is None:
        raise ValueError("timestamp must include timezone")
    return d.astimezone(dt.timezone.utc)


def _stamp(value):
    return _parse(value).strftime("%Y-%m-%dT%H:%M:%SZ")


class _Source:
    """Reach, row count and notes of one source; `rows` None means unreadable."""
    def __init__(self, name):
        self.name, self.rows, self.notes = name, 0, []
        self.lo = self.hi = None          # (datetime, raw)
        self.partial = False

    def seen(self, raw):
        try:
            d = _parse(raw)
        except (ValueError, TypeError):
            return
        if self.lo is None or d < self.lo[0]:
            self.lo = (d, raw)
        if self.hi is None or d > self.hi[0]:
            self.hi = (d, raw)

    def unreadable(self, reason):
        self.rows = None
        self.notes = [reason]

    def entry(self):
        out = {"name": self.name, "from": self.lo[1] if self.lo else None,
               "to": self.hi[1] if self.hi else None, "rows": self.rows}
        if self.notes:
            out["reason"] = "; ".join(self.notes)
        return out


class _Obs:
    __slots__ = ("file", "ok", "ts", "raw", "sha", "run_id", "run_ids", "key")

    def __init__(self, file, ok, raw, sha=None, run_id=None, run_ids=None, key=None):
        self.file, self.ok, self.raw, self.sha = file, ok, raw, sha
        self.ts = _parse(raw)
        self.run_id, self.run_ids, self.key = run_id, run_ids, key


def _tracked(repo):
    """Tracked `test_*.py` paths, or None when `git ls-files` cannot answer."""
    try:
        r = _subprocess.run(["git", "ls-files", "-z"], cwd=str(repo), capture_output=True, timeout=60)
    except (OSError, _subprocess.SubprocessError):
        return None
    if r.returncode:
        return None
    paths = r.stdout.decode("utf-8", "replace").split("\0")
    return {p for p in paths if p.endswith(".py") and p.rsplit("/", 1)[-1].startswith("test_")}


def _ids_of(raw, verdict=False):
    """-> (file-or-node ids that are pytest kind, count of other-kind ids)."""
    if isinstance(raw, dict):
        members = raw.get("pytest") or []
        other = sum(len(v) for k, v in raw.items() if k != "pytest" and isinstance(v, list))
    elif isinstance(raw, list):
        members, other = raw, 0
    else:
        return [], 0
    pytest = []
    for n in members:
        if not isinstance(n, str):
            other += 1
        elif base_red.id_kind(n) == "pytest" or (verdict and n.endswith(".py") and "::" not in n):
            pytest.append(n)
        else:
            other += 1
    return pytest, other


def _run_tail(url):
    m = re.search(r"(\d+)/?$", str(url or ""))
    return m[1] if m else None


# ---------------------------------------------------------------- ledger

def _ledger(project, events, tracked, sources):
    """Failure observations from the six ledger sources."""
    obs = []
    rows_of = {t: [e for e in events if e.get("type") == t and e.get("project") == project]
               for t in ("master-full-run", "base-red-nightly", "base-red", "selected-canary", "verdict")}

    def take(src, row, ids, make_key, run_ids=None, verdict=False):
        """Adds one failure observation per distinct tracked file in `ids`."""
        pytest, other = _ids_of(ids, verdict)
        src.other += other
        seen = set()
        for node in pytest:
            f = node.split("::", 1)[0]
            if f not in tracked:
                src.unresolved += 1
                continue
            if f in seen:
                continue
            seen.add(f)
            obs.append(_Obs(f, False, row["ts"], sha=row.get("sha"),
                            run_ids=frozenset(run_ids) if run_ids else None, key=make_key(f)))

    def start(name):
        s = sources[name]
        s.other = s.unresolved = 0
        return s

    def finish(s, extra=()):
        notes = ["failures only (no pass information)"]
        if s.other:
            notes.append(f"{s.other} non-pytest ids not keyed")
        if s.unresolved:
            notes.append(f"{s.unresolved} pytest ids not resolved to a tracked test file")
        s.notes = notes + list(extra)

    for typ, name in (("master-full-run", "ledger-master-full-run"),
                      ("base-red-nightly", "ledger-base-red-nightly")):
        s = start(name)
        incomplete = 0
        for row in rows_of[typ]:
            s.rows += 1
            s.seen(row.get("ts"))
            if row.get("complete") is False:
                incomplete += 1
            rids = [str(r) for r in (row.get("run_ids") or []) if str(r)]
            take(s, row, row.get("ids"),
                 (lambda f, rids=rids, row=row: (f, frozenset(rids)) if rids
                  else (f, row.get("sha"), row["ts"])), run_ids=rids)
        finish(s, [f"{incomplete} incomplete rows"] if typ == "master-full-run" else [])

    s = start("ledger-base-red")
    for row in rows_of["base-red"]:
        s.rows += 1
        s.seen(row.get("ts"))
        take(s, row, row.get("ids"), lambda f, row=row: (f, row.get("sha"), row["ts"]))
    finish(s)

    s = start("ledger-selected-canary")
    unusable = 0
    for row in rows_of["selected-canary"]:
        s.rows += 1
        s.seen(row.get("ts"))
        ids = row.get("ids")
        if row.get("schema") != 2 and not (isinstance(ids, list) and all(isinstance(i, str) for i in ids)):
            unusable += 1
            continue
        take(s, row, ids, lambda f, row=row: (f, row.get("sha"), row["ts"]))
    finish(s, [f"{unusable} rows read but unusable"] if unusable else [])

    s = start("ledger-verdict")
    for row in rows_of["verdict"]:
        s.rows += 1
        s.seen(row.get("ts"))
        take(s, row, row.get("pre_existing"),
             lambda f, row=row: (f, row.get("subject"), row["ts"]), verdict=True)
    finish(s)

    s = start("ledger-rehearsal")
    rehearsed = {}
    verified = []
    for e in events:
        if e.get("project") != project or e.get("rehearsal") is not True:
            continue
        if e.get("type") == "window-rehearsed":
            rehearsed.setdefault(e.get("subject"), e)
        elif e.get("type") == "window-tree-verified":
            verified.append(e)
    for e in rehearsed.values():
        s.rows += 1
        s.seen(e.get("ts"))
    for row in verified:
        s.rows += 1
        s.seen(row.get("ts"))
        rid = _run_tail((rehearsed.get(row.get("subject")) or {}).get("run_url"))
        pytest, other = _ids_of(row.get("new_ids"))
        s.other += other
        seen = set()
        for node in pytest:
            f = node.split("::", 1)[0]
            if f not in tracked:
                s.unresolved += 1
                continue
            if f in seen:
                continue
            seen.add(f)
            key = (f, rid) if rid else (f, "sha", row.get("sha"), row["ts"])
            obs.append(_Obs(f, False, row["ts"], sha=row.get("sha"), run_id=rid, key=key))
    finish(s)
    return obs


# ---------------------------------------------------------------- junit

def _gh_error(rc, err):
    if rc == QUOTA_RC or "quota low" in (err or ""):
        return "quota low: ghlimit skipped the read"
    return f"gh failed rc={rc}: {(err or '').strip()[:150]}"


def _file_of(case, tracked):
    f = (case.get("file") or "").strip().removeprefix("./")
    if f and f in tracked:
        return f
    parts = [p for p in (case.get("classname") or "").split(".") if p]
    for n in range(len(parts), 0, -1):
        cand = "/".join(parts[:n]) + ".py"
        if cand in tracked:
            return cand
    return None


def _parse_zip(blob, tracked):
    """-> ({file: {"fail", "pass"}}, unresolved testcase count). Raises ValueError."""
    with zipfile.ZipFile(io.BytesIO(blob)) as archive:
        members = archive.infolist()
        if len(members) > MAX_MEMBERS or sum(m.file_size for m in members) > 4 * MAX_BYTES:
            raise ValueError("archive too large")
        result, unresolved = {}, 0
        for m in members:
            name = m.filename
            if name.startswith("/") or ".." in name.split("/") or not name.endswith(".xml"):
                continue
            if m.file_size > MAX_BYTES:
                raise ValueError("member too large")
            data = archive.read(m)
            if b"<!DOCTYPE" in data or b"<!ENTITY" in data:
                raise ValueError("DTD refused")
            try:
                root = ET.fromstring(data)
            except ET.ParseError as e:
                raise ValueError("xml parse error") from e
            for case in root.iter("testcase"):
                f = _file_of(case, tracked)
                if f is None:
                    unresolved += 1
                    continue
                st = result.setdefault(f, {"fail": False, "pass": False})
                if case.find("failure") is not None or case.find("error") is not None:
                    st["fail"] = True
                elif case.find("skipped") is None:
                    st["pass"] = True
        return result, unresolved


def _junit(repo, tracked, src):
    """Failure and pass observations from the CI junit artifacts."""
    if tracked is None:
        src.unreadable("tracked-file list unavailable (git ls-files failed)")
        return []
    try:
        owner = base_red._owner(repo)
    except Exception as e:
        src.unreadable(f"GitHub owner unavailable: {str(e)[:100]}")
        return []
    listed = []
    for page in range(1, MAX_PAGES + 1):
        rc, out, err = ghlimit.run(["gh", "api", f"repos/{owner}/actions/artifacts?per_page=100&page={page}"],
                                   wait=False)
        if rc:
            if not listed:
                src.unreadable(_gh_error(rc, err))
                return []
            src.partial = True
            src.notes.append("listing stopped early: " + _gh_error(rc, err))
            break
        try:
            batch = json.loads(out)["artifacts"]
            if not isinstance(batch, list):
                raise TypeError
        except (ValueError, KeyError, TypeError):
            if not listed:
                src.unreadable("artifact listing was not valid JSON")
                return []
            src.partial = True
            src.notes.append("listing stopped early: invalid JSON page")
            break
        listed.extend(a for a in batch if isinstance(a, dict))
        usable = [a for a in listed if _usable(a)]
        if len(batch) < 100 or len(usable) > MAX_ARTIFACTS:
            break
    else:
        src.partial = True
        src.notes.append(f"listing capped at {MAX_PAGES} pages")
    usable = sorted((a for a in listed if _usable(a)), key=lambda a: _parse(a["created_at"]), reverse=True)
    if len(usable) > MAX_ARTIFACTS:
        src.partial = True
        src.notes.append(f"{len(usable) - MAX_ARTIFACTS} older artifacts beyond the {MAX_ARTIFACTS} bound not read")
        usable = usable[:MAX_ARTIFACTS]
    runs, unresolved, oversize, failed, attempted, first_err = {}, 0, 0, 0, 0, ""
    for art in usable:
        if art.get("size_in_bytes", 0) > MAX_BYTES:
            oversize += 1
            continue
        if not ghlimit.gate(wait=False)["ok"]:
            src.partial = True
            src.notes.append(f"quota low: stopped after {attempted} of {len(usable)} downloads")
            break
        attempted += 1
        try:
            proc = subprocess.run([ghlimit.real_gh(), "api", f"repos/{owner}/actions/artifacts/{int(art['id'])}/zip"],
                                  capture_output=True, timeout=60)
        except Exception as e:
            failed += 1
            first_err = first_err or str(e)[:100]
            continue
        if proc.returncode or len(proc.stdout) > MAX_BYTES:
            failed += 1
            first_err = first_err or (proc.stderr.decode("utf-8", "replace").strip()[:120]
                                      if proc.returncode else "oversized download")
            if proc.returncode and b"quota low" in (proc.stderr or b""):
                first_err = "quota low"
            continue
        try:
            files, un = _parse_zip(proc.stdout, tracked)
        except (ValueError, zipfile.BadZipFile, OSError, KeyError) as e:
            failed += 1
            first_err = first_err or f"parse failure: {e}"[:100]
            continue
        unresolved += un
        src.rows += 1
        src.seen(art["created_at"])
        run = art["workflow_run"]
        for f, st in files.items():
            cur = runs.setdefault((str(run["id"]), f), {"fail": False, "pass": False,
                                                        "raw": art["created_at"], "sha": run.get("head_sha")})
            cur["fail"] |= st["fail"]
            cur["pass"] |= st["pass"]
    if oversize:
        src.partial = True
        src.notes.append(f"{oversize} artifacts over {MAX_BYTES} bytes skipped")
    if failed:
        src.partial = True
        src.notes.append(f"{failed} of {attempted} downloads failed ({first_err})")
    if attempted and failed == attempted and src.rows == 0:
        src.rows = None
    if unresolved:
        src.notes.append(f"{unresolved} testcases not resolved to a tracked test file")
    obs = []
    for (rid, f), st in runs.items():
        if st["fail"] or st["pass"]:
            obs.append(_Obs(f, not st["fail"], st["raw"], sha=st["sha"], run_id=rid, key=(f, rid)))
    return obs


def _usable(a):
    try:
        wr = a["workflow_run"]
        _parse(a["created_at"])
        int(a["id"])
        return (str(a.get("name", "")).startswith(ARTIFACT_PREFIX) and not a.get("expired")
                and wr.get("id") is not None)
    except (KeyError, TypeError, ValueError, AttributeError):
        return False


# ---------------------------------------------------------------- merge + classify

def _merge(junit_obs, ledger_obs):
    """Overlapping sources never double count (R4.4)."""
    failures, passes = {}, []
    by_run = {}                                   # file -> set of run ids with a failure
    for o in junit_obs + [o for o in ledger_obs if o.run_ids is None and o.run_id is not None]:
        if o.ok:
            continue
        if o.key not in failures:
            failures[o.key] = o
            by_run.setdefault(o.file, set()).add(o.run_id)
    for o in ledger_obs:
        if o.run_id is not None:                   # rehearsal with a run id: handled above
            continue
        if o.run_ids and by_run.get(o.file, set()) & o.run_ids:
            continue
        failures.setdefault(o.key, o)
    seen = set()
    for o in junit_obs:
        if o.ok and o.key not in seen and o.run_id not in by_run.get(o.file, ()):
            seen.add(o.key)
            passes.append(o)
    return list(failures.values()), passes


def _classify(fails, passes):
    rows = {}
    for o in passes:
        rows.setdefault(o.file, {"p": [], "f": []})["p"].append(o)
    for o in fails:
        rows.setdefault(o.file, {"p": [], "f": []})["f"].append(o)
    tests = {}
    for f in sorted(rows):
        p, fl = rows[f]["p"], rows[f]["f"]
        real = flaky = unknown = 0
        for x in fl:
            if x.sha and any(q.sha == x.sha for q in p):
                flaky += 1
            elif any(q.ts > x.ts and q.sha != x.sha for q in p):
                real += 1
            else:
                unknown += 1
        last = max(fl, key=lambda x: x.ts).raw if fl else None
        tests[f] = {"runs": len(p) + len(fl), "failures": len(fl), "last_failure": last,
                    "real": real, "flaky": flaky, "unknown": unknown}
    return tests


def _atomic_write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp{os.getpid()}")
    try:
        with open(tmp, "w") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise


LAST_PARTIAL = []          # source names unreadable or partial in the latest build


def build(project, *, now=None):
    repo = base_red._repo(project)        # validates the project name before it forms a path
    sources = {n: _Source(n) for n in SOURCES}
    tracked = _tracked(repo)
    ledger_obs = []
    if tracked is None:
        for n in SOURCES[:6]:
            sources[n].unreadable("tracked-file list unavailable (git ls-files failed)")
    else:
        try:
            events = fold.read_events()
        except Exception as e:
            events = None
            for n in SOURCES[:6]:
                sources[n].unreadable(f"ledger unreadable: {str(e)[:100]}")
        if events is not None:
            try:
                ledger_obs = _ledger(project, events, tracked, sources)
            except Exception as e:
                ledger_obs = []
                for n in SOURCES[:6]:
                    sources[n].unreadable(f"ledger rows unusable: {str(e)[:100]}")
    try:
        junit_obs = _junit(repo, tracked, sources["junit-artifacts"])
    except Exception as e:
        junit_obs = []
        sources["junit-artifacts"].unreadable(f"junit read failed: {str(e)[:100]}")
    fails, passes = _merge(junit_obs, ledger_obs)
    doc = {"project": project,
           "generated_at": _stamp(now if now is not None else dt.datetime.now(dt.timezone.utc)),
           "sources": [sources[n].entry() for n in SOURCES],
           "tests": _classify(fails, passes)}
    path = fold.ROOT / "test-evidence" / project / "history.json"
    _atomic_write(path, json.dumps(doc, indent=1, sort_keys=False) + "\n")
    LAST_PARTIAL[:] = [n for n in SOURCES if sources[n].rows is None or sources[n].partial]
    return path


def main(argv):
    if len(argv) == 1:
        # `./doit test` runs every src/test_*.py with no arguments; this file is
        # a module whose name matches that glob, so a bare run is a no-op.
        print("usage: test_history.py <project>")
        return 0
    if len(argv) != 2:
        sys.stderr.write("usage: test_history.py <project>\n")
        return 2
    try:
        path = build(argv[1])
    except (ValueError, OSError) as e:
        sys.stderr.write(f"test_history: {e}\n")
        return 2
    print(path)
    return 1 if LAST_PARTIAL else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
