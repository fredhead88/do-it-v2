#!/usr/bin/env python3
"""L-spec-0479/plain-backfill: every existing ledger spec whose `plain.line()`
source is `"goal-section"` or `"missing"`, and whose fold state is not
terminal-uninteresting, gets a one-line plain-English sidecar, written once
through seat-billed `research` batches the Executor drives.

Pure helpers only need `fold`/`plain` when actually called (`split_batches`,
`decide_row`) — both import them locally, so this module stays importable
before either dependency is on the path. The CLI (`manifest`/`packet`/
`apply`) imports `fold` at the top of `if __name__ == "__main__":` only.
"""
import json
import pathlib
import re

# ── constants (SD8/SD2, verbatim) ────────────────────────────────────────────

STATE_EXCLUDED = frozenset({"void", "killed", "dropped", "closed-unbuilt"})   # SD8, verbatim
JARGON = ("L1", "L2", "owed", "void", "fold")                                # SD2's five words, verbatim
ID_RE = re.compile(r"\bL-[A-Za-z][A-Za-z-]*-\d+\b")
SPEC_ID_RE = re.compile(r"^L-spec-\d{4}$")                                   # fix-2: full match only

REJECTED_NAME = "plain-backfill-rejected.jsonl"


def state_ok(state):
    return state not in STATE_EXCLUDED


def source_ok(source):
    return source in ("goal-section", "missing")


def is_candidate(spec_id, root):
    """fix-2: `SPEC_ID_RE.fullmatch(spec_id)` and the content file existing —
    both. The regex excludes malformed subjects (`"L-spec-writer-0515"`,
    `"-ac12-..."`); `exists()` excludes shaped-but-contentless ones (a real
    `"L-spec-9001"`, state `"unknown"`)."""
    if not SPEC_ID_RE.fullmatch(spec_id):
        return False
    return (pathlib.Path(root) / "content" / f"{spec_id}.md").exists()


def rejection_counts(root):
    """`{spec: n}` from `root/"content"/"plain-backfill-rejected.jsonl"`, every
    valid JSON line with a string `"spec"` key (any reason, "omitted by
    research" included); `{}` if the file is absent."""
    path = pathlib.Path(root) / "content" / REJECTED_NAME
    if not path.exists():
        return {}
    counts = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(obj, dict) and isinstance(obj.get("spec"), str):
            counts[obj["spec"]] = counts.get(obj["spec"], 0) + 1
    return counts


def split_batches(specs, root):
    """`(eligible, hand_line)`, sorted ascending by id. A spec id from `specs`
    (`fold.fold()`'s dict; only `"state"` read) is a CANDIDATE when
    `is_candidate(id, root)` and `state_ok(specs[id]["state"])` and
    `source_ok(plain.line(id, root)["source"])`; `rejection_counts(root).get(id,
    0) < 2` -> eligible, `>= 2` -> hand_line."""
    import plain  # local: only this function needs it
    counts = rejection_counts(root)
    eligible, hand_line = [], []
    for spec_id in sorted(specs):
        if not is_candidate(spec_id, root):
            continue
        if not state_ok(specs[spec_id]["state"]):
            continue
        if not source_ok(plain.line(spec_id, root)["source"]):
            continue
        if counts.get(spec_id, 0) < 2:
            eligible.append(spec_id)
        else:
            hand_line.append(spec_id)
    return eligible, hand_line


def decide_row(row, specs, root):
    """`("write"|"skip"|"reject", detail)`. `row` is always already-parsed JSON
    (the CLI handles a malformed/non-dict line itself, fix-5)."""
    spec_id = row.get("spec")
    line = row.get("line")
    if not isinstance(spec_id, str) or not isinstance(line, str) or spec_id not in specs:
        return "reject", "unknown spec subject"
    if not is_candidate(spec_id, root):
        return "reject", "no content file for this spec"
    sidecar_path = pathlib.Path(root) / "content" / f"{spec_id}.plain.txt"
    if sidecar_path.exists():
        return "skip", "sidecar exists"
    import plain  # local: only this function needs it
    content_path = pathlib.Path(root) / "content" / f"{spec_id}.md"
    text = content_path.read_text(encoding="utf-8")
    if plain.PLAIN_RE.search(text):
        return "skip", "already has a spec-line"
    words = line.split()
    if not (8 <= len(words) <= 30) or not line.rstrip().endswith("."):
        return "reject", "not 8-30 words ending in a period"
    if ID_RE.search(line):
        return "reject", "names a spec or charter id"
    for word in JARGON:
        if re.search(r"\b" + re.escape(word) + r"\b", line):
            return "reject", f"uses jargon word '{word}'"
    return "write", None


def _append_reject(root, spec_id, line, reason):
    path = pathlib.Path(root) / "content" / REJECTED_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"spec": spec_id, "line": line, "reason": reason}) + "\n")


def apply_rows(rows, specs, root, served_ids):
    """`{"written","skipped","rejected"}`. `rows` holds only parsed JSON
    objects (fix-5; the CLI logs a malformed line itself). NEVER calls
    `fold.append` or touches `events/`."""
    counts = {"written": 0, "skipped": 0, "rejected": 0}
    seen = set()
    for row in rows:
        spec_val = row.get("spec")
        if isinstance(spec_val, str):
            seen.add(spec_val)
        verdict, detail = decide_row(row, specs, root)
        if verdict == "write":
            sidecar_path = pathlib.Path(root) / "content" / f"{spec_val}.plain.txt"
            sidecar_path.parent.mkdir(parents=True, exist_ok=True)
            sidecar_path.write_text(row["line"] + "\n", encoding="utf-8")
            counts["written"] += 1
        elif verdict == "skip":
            counts["skipped"] += 1
        else:
            reject_spec = spec_val if isinstance(spec_val, str) else "?"
            reject_line = row.get("line") if isinstance(row.get("line"), str) else ""
            _append_reject(root, reject_spec, reject_line, detail)
            counts["rejected"] += 1
    # fix-1: an id in `served_ids` with no row at all (research produced no row
    # for it, never a row that existed but was rejected) counts toward SD8's
    # cap like any reject.
    for spec_id in served_ids:
        if spec_id not in seen:
            _append_reject(root, spec_id, "", "omitted by research")
            counts["rejected"] += 1
    return counts


# ── CLI ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    import sys

    import fold  # noqa: E402  local: the CLI only

    def _parse_packet(path):
        """`served_ids` from a packet's `- <id>` lines under `specs:`. `None`
        on a missing/unparseable file — the one usage error."""
        p = pathlib.Path(path)
        try:
            text = p.read_text(encoding="utf-8")
        except OSError:
            return None
        lines = text.splitlines()
        try:
            idx = lines.index("specs:")
        except ValueError:
            return None
        ids = []
        for ln in lines[idx + 1:]:
            if ln.startswith("- "):
                ids.append(ln[2:].strip())
            elif ln.strip() == "":
                continue
            else:
                break
        return ids or None

    def cmd_manifest(args):
        specs, *_ = fold.fold(fold.read_events())
        eligible, hand_line = split_batches(specs, fold.ROOT)
        batches = [eligible[i:i + args.batch] for i in range(0, len(eligible), args.batch)]
        today = fold.NOW.date().isoformat()
        summary = f"{len(batches)} batches, {len(eligible)} eligible, {len(hand_line)} needing a hand line."
        lines = [f"# Plain-English backfill manifest — {today}", "", summary]
        for i, b in enumerate(batches, 1):
            lines.append("")
            lines.append(f"## Batch {i}")
            for spec_id in b:
                lines.append(f"- {spec_id}")
        if hand_line:
            lines.append("")
            lines.append("## Needs a hand line")
            for spec_id in hand_line:
                lines.append(f"- {spec_id}")
        out_path = fold.ROOT / "content" / f"plain-backfill-manifest-{today}.md"
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(summary)
        return 0

    def cmd_packet(args):
        specs, *_ = fold.fold(fold.read_events())
        eligible, hand_line = split_batches(specs, fold.ROOT)
        if not eligible:
            print("0 batches, nothing to serve.")
            return 0
        batch = eligible[:args.batch]
        packets_dir = fold.ROOT / "packets"
        # fix-1: a monotonic count of packet files already on disk, never the
        # manifest's re-derived batch index, so two passes never reuse a name.
        n = 1 + len(list(packets_dir.glob("L-charter-0047-research-*.md")))
        packets_dir.mkdir(parents=True, exist_ok=True)
        out_path = packets_dir / f"L-charter-0047-research-{n}.md"
        lines = ["kind: plain-english-backfill-batch", "specs:"]
        for spec_id in batch:
            lines.append(f"- {spec_id}")
        out_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        print(f"packet {n}: {len(batch)} specs -> packets/L-charter-0047-research-{n}.md")
        return 0

    def cmd_apply(args):
        served_ids = _parse_packet(args.packet)
        if served_ids is None:
            print(f"plain_backfill apply: could not parse packet {args.packet}", file=sys.stderr)
            return 2
        rejected_path = fold.ROOT / "content" / REJECTED_NAME
        existing = 0
        if rejected_path.exists():
            existing = len([l for l in rejected_path.read_text(encoding="utf-8").splitlines() if l.strip()])
        jsonl_path = pathlib.Path(args.jsonl)
        malformed = 0
        rows = []
        if jsonl_path.exists():
            for line in jsonl_path.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    obj = None
                if isinstance(obj, dict):
                    rows.append(obj)
                else:
                    # fix-5: parsing/malformed-line handling lives in the CLI;
                    # `apply_rows` only ever receives `list[dict]`.
                    _append_reject(fold.ROOT, "?", "", "malformed JSON line")
                    malformed += 1
        # fix-1: `<jsonl>` absent is no longer a hard error — every
        # `served_ids` entry becomes an "omitted by research" reject inside
        # `apply_rows`.
        specs, *_ = fold.fold(fold.read_events())
        counts = apply_rows(rows, specs, fold.ROOT, served_ids)
        counts["rejected"] += malformed
        print(f"written={counts['written']} skipped={counts['skipped']} rejected={counts['rejected']}")
        if rejected_path.exists():
            new_lines = [l for l in rejected_path.read_text(encoding="utf-8").splitlines() if l.strip()][existing:]
            for line in new_lines:
                obj = json.loads(line)
                print(f"{obj.get('spec', '?')}: {obj.get('reason', '')}")
        return 0

    parser = argparse.ArgumentParser(prog="plain_backfill.py")
    sub = parser.add_subparsers(dest="cmd", required=True)

    p_manifest = sub.add_parser("manifest")
    p_manifest.add_argument("--batch", type=int, default=15)
    p_manifest.set_defaults(func=cmd_manifest)

    p_packet = sub.add_parser("packet")
    p_packet.add_argument("--batch", type=int, default=15)
    p_packet.set_defaults(func=cmd_packet)

    p_apply = sub.add_parser("apply")
    p_apply.add_argument("jsonl")
    p_apply.add_argument("packet")
    p_apply.set_defaults(func=cmd_apply)

    ns = parser.parse_args()
    sys.exit(ns.func(ns))
