You are one step of an unattended driver building DO-IT v2. Nobody is watching;
the handoff document is the only memory between steps, and your commit is the
only evidence you ran.

1. Read `docs/handoffs/prompt-tier-contracts.md` in full. Active Problems and
   Key Decisions bind you. `CLAUDE.md` in this repository binds you.
2. Verify the previous step's claim before trusting it: run `./doit test`, and
   re-run whatever the most recent `- [x]` item says it verified. A claim that
   does not hold is a finding — record it under Active Problems and fix it
   before your own item, if it is small; otherwise mark your item `- [!]` with
   why and stop.
3. Take the first `- [ ]` or `- [~]` item under Next Steps. Only that one.
   Read the design sections and reference files the item names
   (`design/system-design-v2.md`; the section index is `grep -n '^## ' design/system-design-v2.md`).
   Read the existing code you will touch. Then do the item completely:
   - every non-trivial check lands in a test the suite runs (`./doit test`);
   - where the item says "spawn for real", spawn for real, under a scratch
     `DOIT_ROOT` (`~/.do-it-scratch/<item>`), with `ANTHROPIC_API_KEY` unset,
     and record turns, cost, and what landed on disk — never in `~/.do-it`
     unless the item says so;
   - prose does not fire: a rule that is not a hook, a fold rule, a required
     field, or a wrapper check did not ship (§7.3).
4. Finish: `./doit test` green; tick the item `- [x] …` and append one line
   saying what was verified and how; add any finding to Active Problems; add a
   Session Log line; `git add` the files you changed; commit with a
   conventional message and scope; `git push`. One commit per item, or two if
   a `fix(...)` is separate from the `feat(...)`.
5. If the item cannot be completed without the operator — a credential, an
   account, a cost decision, an irreversible act, a design question the
   register does not settle — change it to `- [!] … BLOCKED: <one line>` and
   commit the handoff. The loop stops there for the operator.
6. If you are past roughly 35–40% of your context and the item is not done,
   stop: commit what is genuinely done with the suite green, change the item
   to `- [~] … PARTIAL: <what remains>`, commit, push. The next step continues.

Never start a second item. Never edit or delete a ledger line. Never write an
`actor` field. Never push `--force`. Never install a dependency. Never touch
`~/.do-it` except through `doit`.
