# A ledger that derives state, and the roles and guards that read it

DO-IT keeps a project's state in append-only event files and **derives** every
fact from them — nothing is ever stamped, edited, or asserted. The board you read
is a projection, disposable and rebuilt on every render. Around that ledger sit
thirteen role contracts (markdown files with an output schema each), one dispatch
wrapper that runs a contract on any of three backends and checks it after the
fact, and the scripts that own everything deterministic — the merge gate, the
deploy, the reaper, the audit pre-pass, the packets.

**Version:** `VERSION` (`doit version`); history in `CHANGELOG.md`. Current: 0.2.0.

**Status, honestly:** this ran its first real project on 2026-09-14 — the Albert
Scott pilot: three charters closed, a 60-spec backlog deployed, 41 spawns, 7 of
them failed for wrapper or driver reasons and none for a model's. The record of
that day, entry by entry with what was changed for each, is
`docs/handoffs/albert-scott-pilot-2026-09-14.md`; the ranked list of what the
day says the system must become is `docs/handoffs/pilot-retro-change-list.md`.
Every guard in this repo that is claimed to work has fired on a real case and
its event is in a ledger; the ones that have not are named as such.

---

## Install

Needs **python3 ≥ 3.11** (`tomllib`), **git 2.38 or newer** (`git merge-tree
--write-tree`), **node** (the install gate), and `jsonschema` importable.

```bash
git clone https://github.com/fredhead88/do-it-v2.git ~/do-it-v2
~/do-it-v2/install.sh
```

Idempotent. It checks versions, creates `~/.do-it/`, installs the model map from
the mixed template when the root has none, **runs every check before putting
anything on your PATH**, links `doit` into `~/.local/bin`, and links the contracts
into `~/.claude/agents/`.

## Use

```bash
doit                                    # render the board
doit states · doit events <subject>     # derived states; one subject's events
doit append <type> <subject> [k=v ...]  # append one event, then re-fold
doit models show | use <mixed|claude-only>   # the root's model map
doit think <topic> | --land F…          # a Thinker session; land its charters
doit up                                 # the Planner pane; the tick's cron line
doit packet <role> <subject> [...]      # a role's packet, built from the ledger
doit dispatch <role> <subject> [...]    # run one contract, check it, append its events
doit audit cut|plan <charter> ...       # the six mechanical checks under the audits
doit gate <branch> [main] [--spec ID]   # merge guard. exit 1 = do not merge
doit deploy <spec> --sha S ...          # serial; waits; proves the sha is live
doit reap <charter>                     # reap only provably dead worktrees
doit validate <role> <file>             # an Output object against its schema
doit tick                               # fold; spawn the Executor if the lane is actionable
doit backup push | drill · doit test · doit version · doit help
```

`DOIT_ROOT` moves the ledger · `DOIT_PROJECT` scopes the board · `DOIT_LEDGER_FILE`
picks which actor file you write as · `DOIT_K` is the owed criteria a charter may
close over. The full list is under `doit help`.

### The model map

`$DOIT_ROOT/models.toml` decides, once per ledger root, which **backend** runs
each contract and on which **model**:

| backend | what it is |
|---|---|
| `pane` | a standing interactive session the operator opens; never dispatched (Thinker, Planner, and the Executor under a seat-only root) |
| `seat` | an interactive session's sub-agent, seat-billed; the wrapper writes the packet and waits for the pane to serve it |
| `claude-p` | `claude -p --agent <role>`; metered on some plans, banned on some roots |
| `codex` | `codex exec` on the ChatGPT plan, schema enforced by the CLI |

Two templates ship: `models.example.toml` (Claude and Codex mixed, with a
`fallback` per Codex contract for when its weekly limit is gone) and
`models.claude-only.toml`. The loader refuses a map that lies — Fable on anything
but a pane, a pane role dispatched — and every terminal event records
`model_requested` beside `model_used`, so the ledger can never again say one
model ran when another did. Flags and environment variables may only agree with
the file. The tick reads it too, and refuses to spawn on a root whose Executor is
a pane.

---

## What is here

| File | What it does |
|---|---|
| `src/fold.py` | reads `~/.do-it/events/*.jsonl`, derives every state, renders the board; `EMITS` is who may emit what |
| `src/dispatch.py` | runs one contract on `claude-p`, `seat` or `codex`; every after-the-fact check (schema, file on disk, repo unchanged, contamination); appends the events the Output implies |
| `src/models.py` | the model map: load, validate, resolve, `doit models` |
| `src/packet.py` | per-role packets from the ledger, with the Blindness strip that refuses a contaminated packet before it is written |
| `src/audit.py` | the six mechanical checks that run before either plan audit |
| `src/merge_gate.py` | catches a merge that **removes** a file nobody is watching (performs the merge in memory and looks) |
| `src/deploy.py` · `src/tree_cleanup.py` | serial deploy that proves the sha is live; reaper that proves death by patch-id |
| `src/think.py` · `src/up.py` · `src/tick.py` | Thinker landing and the charter-set diff; the Planner pane; the Executor tick |
| `src/validate.py` · `src/paid_call.py` · `scripts/vet-dep.mjs` · `src/backup.sh` | the seat route's StructuredOutput; the paid-call cap; the install gate; the restic push and drill |
| `agents/*.md` + `*.schema.json` | thirteen contracts, ten Output schemas |
| `src/test_*.py`, `src/test_vet_dep.mjs` | ~580 checks; `./doit test` runs them all |

### The five ideas that make it work

1. **Append-only, and state is derived.** No row is updated, so no state can be
   quietly wrong. The board is a cache; delete it and it comes back.
2. **The actor is the filename**, never a field inside the event. A field written
   by an actor about itself is a stamp. `L-grader-0142.jsonl` cannot lie about
   being the grader.
3. **Authorization lives in the fold, not at the write.** An event from an actor
   not permitted to emit it is *recorded, ignored, and counted* — so a rejected
   attempt leaves a trail instead of vanishing. A builder cannot pass its own
   work: its self-issued verdict is written and then ignored.
4. **Undetermined never reads as clean.** Any check that could not establish its
   answer returns the failure state, never the pass state.
5. **Corrections are events, not edits.** A mistake is repaired by an
   operator-only `correction` that names one prior event and overrides fields on
   it. The mistake and the repair both stay on the record.

### The merge guard, specifically

A branch is cut. `main` then gains a file. The branch merges `main`, deletes that
file, and merges back. **A `base_sha..branch` diff shows no row at all** — the
path is absent at both ends — so the usual guards see nothing and the file is
gone. Measured on a real repo: **7 of the last 200 merges removed a path and
landed.**

The gate **performs the merge and looks at the result.** `git merge-tree
--write-tree` does the real three-way merge in memory and hands back the tree it
would produce; the gate diffs current main against that tree. Two earlier
mechanisms reasoned about the merge instead of performing it, and both were
fooled the same way. Removals under `migrations/` are named regardless of the
grant.

---

## What is not here yet

The design (`design/system-design-v2.md`) is ahead of the code in the places the
pilot found. The ranked list is `docs/handoffs/pilot-retro-change-list.md`; the
largest open items are a `deploy.py` that keeps its full log and separates "the
gate refused" from "the target broke", a checker generator that lints the spec's
verification block, owed evidence that can actually be owed, and a per-goal
charter set. None of those is prose-only; each is a code change with a test and
each names the pilot entry it answers.

## Relationship to DO-IT v4.7

`fredhead88/do-it` is the predecessor — a filesystem-inbox spec pipeline, shipped
and used. **This is a ground-up redesign that shares no code with it.** v4.7's
record is one of the corpora the design was tested against: several of the
decisions here exist because something in v4.7 failed in a specific, measured way.

## Reading the design

`design/system-design-v2.md` is the whole thing: every decision, why it was made,
and what it replaced. It is long and it argues with itself in places, which is
the point — the reasoning is the artifact, not the conclusions.

## Licence

None yet. Nothing here grants you rights to use it; it is public to be read.
Ask if you want to use it for something.
