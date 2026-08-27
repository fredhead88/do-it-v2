# A ledger that derives state, and the guards that read it

DO-IT keeps a project's state in append-only event files and **derives** every
fact from them — nothing is ever stamped, edited, or asserted. The board you read
is a projection, disposable and rebuilt on every render.

**Read this part before you rely on it:** what is here is the *substrate* and one
guard. It is genuinely finished and genuinely used — but it is roughly a fifth of
the system described in `design/system-design-v2.md`, and the missing four fifths
are **prose, not code** (see *What is not here* below).

---

## Install

Needs **python3** and **git 2.38 or newer** (`git merge-tree --write-tree`).

```bash
git clone https://github.com/fredhead88/do-it-v2.git ~/Projects/do-it
~/Projects/do-it/install.sh
```

Idempotent. It checks your git version, creates `~/.do-it/`, **runs every check
before putting anything on your PATH**, and links `doit` into `~/.local/bin`. An
install that ships a red suite is how a guard becomes a guard that is not
running.

## Use

```bash
doit                         # render the board
doit states                  # every derived state, one per line
doit append <type> <subject> [k=v ...]
doit gate <branch> [main] [--spec ID]    # merge guard. exit 1 = do not merge
doit backup push | drill
doit test
```

`DOIT_ROOT` moves the ledger · `DOIT_PROJECT` scopes the board to one project ·
`DOIT_LEDGER_FILE` picks which actor file you are writing as.

---

## What is here

| File | What it does |
|---|---|
| `src/fold.py` | reads `~/.do-it/events/*.jsonl`, derives every state, renders the board |
| `src/merge_gate.py` | catches a merge that **removes** a file nobody is watching |
| `src/backup.sh` | one-way `restic` push, and a restore drill that proves it |
| `src/test_*.py` | 58 checks. The merge-gate ones all run against real git repos |

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
   it. The mistake and the repair both stay on the record — strictly more than an
   edit would leave. Corrections are counted on the board, always.

### The merge guard, specifically

A branch is cut. `main` then gains a file. The branch merges `main`, deletes that
file, and merges back. **A `base_sha..branch` diff shows no row at all** — the
path is absent at both ends — so the usual guards see nothing and the file is
gone. Measured on a real repo: **7 of the last 200 merges removed a path and
landed.**

The gate **performs the merge and looks at the result.** `git merge-tree
--write-tree` does the real three-way merge in memory and hands back the tree it
would produce; the gate diffs current main against that tree. Whatever the merge
would actually remove, it removes — no modelling, no inference.

Two earlier mechanisms were tried here and both were wrong, in the same way.
One diffed the branch against its recorded base, and was blind to the scar
above. One diffed against current main and confirmed each candidate against the
merge-base — which works until a branch has been merged into and kept going, at
which point git has **several** valid merge-bases and picks one arbitrarily.
Reproduced: it picked the one lacking the file, the gate said clean, the merge
deleted it. **Both were reasoning about a merge instead of performing one.**

Removals and reverts are filtered to paths outside the branch's `writes:` grant.
**Removals under `migrations/` are named regardless** — a deletion there removes
the evidence along with the artifact, which is what fools every consistency check
downstream.

---

## What is not here

`design/system-design-v2.md` is the full specification. Against it, this repo is
**item ① and one guard**. Still to be written, and it is **~4/5 of the total**:

| Missing | Rough size | Kind |
|---|---|---|
| Ten sub-agent contracts (the "seats") | 1,100–1,500 lines | **prose** |
| Their ten output schemas | ~200 lines | schema |
| Three driver skills — Planner, Executor, Thinker | ~900 lines | **prose** |
| Six audit scripts | 150–250 lines | code |
| The rest of the fold's rules (typed ACs, blind grading, wedge queries) | ~250 lines | code |

**A seat is a markdown file, not a program.** Its `tools:` line *is* the sandbox —
that is the whole isolation mechanism, measured and confirmed. So "write the
sub-agents" means writing prompts, not building an agent framework.

## Status, honestly

The ledger here has run one real charter: this system building itself. One spec
is built and its guard has fired on a real merge; two specs are written and not
picked up. **Four defects have been found by running this code and none of them
was visible in the unit tests** — which is the argument for the whole design, and
also the reason to distrust any claim in this file that was not checked.

The backup is **unproven** until you set a destination — the board says so on
every render, by design.

## Relationship to DO-IT v4.7

`fredhead88/do-it` is the predecessor — a filesystem-inbox spec pipeline, shipped
and used. **This is a ground-up redesign that shares no code with it.** v4.7's
record is one of the corpora the design was tested against: several of the
decisions here exist because something in v4.7 failed in a specific, measured way.
It is a successor, not a fork, and the old repo stands on its own.

## Reading the design

`design/system-design-v2.md` is the whole thing: every decision, why it was made,
and what it replaced. It is long and it argues with itself in places, which is
the point — the reasoning is the artifact, not the conclusions.

## Licence

None yet. Nothing here grants you rights to use it; it is public to be read.
Ask if you want to use it for something.
