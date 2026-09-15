#!/usr/bin/env python3
"""Hand-built Planner packets (pilot S6): plan-auditor at stage cut|plan, same shape as the pilot's.
usage: mkpacket.py <charter-id> <stage> <out>"""
import json, pathlib, re, sys, glob
R = pathlib.Path.home() / ".do-it"
cid, stage, out = sys.argv[1], sys.argv[2], pathlib.Path(sys.argv[3])
ch = (R / "content" / f"{cid}.md").read_text()
def section(n):
    m = re.search(rf"^## {n}\. [^\n]*\n(.*?)(?=^## \d+\.|\Z)", ch, re.S | re.M)
    return m.group(1).strip("\n")
parts = [f"stage: {stage}", f"charter: {cid}", "", "## The charter's done-condition", section(4), "",
         "## The charter's requirements", section(2), "", "## The cut", (R / "content" / f"cut-{cid}.md").read_text().rstrip()]
if stage == "plan":
    parts += ["", "## The Plan", (R / "content" / f"plan-{cid}.md").read_text().rstrip(), "", "## The cut-audit's findings (prior round, by design)"]
    files = [f for f in sorted(glob.glob(str(R / "events" / "L-plan-auditor-*.jsonl")))
             if any(json.loads(l).get("subject") == cid and json.loads(l).get("type") == "spawn-done" for l in open(f) if l.strip())]
    for f in files[-1:]:          # the LATEST cut-audit only: a re-cut is audited fresh, its predecessor's findings are superseded
        for line in open(f):
            e = json.loads(line)
            if e.get("type") == "audit-finding" and e.get("subject") == cid and e.get("stage") == "cut":
                parts.append(f"- [{e.get('category')}] {e.get('finding')}" + (f" — confirms_with: {e.get('confirms_with')}" if e.get("confirms_with") else ""))
parts += ["", (R / "content" / f"audit-{stage}-{cid}.md").read_text().rstrip(), ""]
out.write_text("\n".join(parts))
print(out)
