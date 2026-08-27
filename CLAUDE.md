# DO-IT — project instructions

The system in this repo, and the substrate other projects pull in.

## Rules that are not negotiable here

- **The ledger is append-only.** Never edit or delete a line in
  `~/.do-it/events/*.jsonl`, for any reason, including "it was obviously wrong".
  Repair with an operator-only `correction` event (§9.2 rule 5, D111).
- **Never write an `actor` field.** The actor is the filename (D90).
- **Undetermined is never clean.** Any guard that could not establish its answer
  returns the failure state.
- **Do not claim a guard works because it is written.** It works when it has
  fired on a real case and the event is in the ledger. Four of this repo's
  defects were invisible to its own passing tests.
- **Decision and text are separate commits:** `decision(Dnn): …` then `fix(…): …`.

## Verify before claiming done

```bash
./doit test        # every check
./doit             # the board renders and HEALTH is honest
```

The design is `design/system-design-v2.md`. The decision log lives outside this
repo — it contains client detail and is not shipped here.
