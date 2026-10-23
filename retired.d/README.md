# Retirement registry

Every unit adds its own `retired.d/<slot>.toml`, with one or more `[[retired]]`
rows. `doit retired check` proves absence in the real checkout without changing
files or ledger history. This replaces unverified retirement claims with a check.

Fields:

- `name`: required nonempty mechanism name.
- `kind`: required `code`, `cron`, or `doc`.
- `pattern`: required nonempty string matched against each line.
- `paths`: required list of repo-relative globs; nonempty for code rows.
  Absolute globs and any `..` component are refused. Cron/doc scopes are fixed.
- `repo`: optional absolute checkout path; otherwise the check's default repo
  (the doit checkout, or `--repo`, independently of `--root`).
- `regex`: optional boolean, default false (literal fixed string); true uses
  Python regular expressions. An invalid regex is undetermined.
- `provisional`: optional boolean, default false. A provisional pattern still
  runs normally, but is printed and counted because it needs tightening before
  its absence can prove retirement.
- `armed_when_path`: optional repo-relative marker file, paired with
  `armed_when_contains`.
- `armed_when_contains`: literal nonempty marker string. Until the marker
  exists and contains it, the row is printed and counted as unarmed. An
  unreadable repo or marker is undetermined.

The search universe is git-tracked files (`git ls-files`); untracked files never
change the answer. Code searches `paths`; cron searches non-comment lines of the installed crontab
and `crons.toml`; docs search `docs/**`, `agents/**`, and `README.md` only.
The never-self-match rule excludes `retired.d/**`, `src/retired.py`, and
`src/test_retired.py` from every file search. The append-only ledger/events,
`CHANGELOG.md`, `docs/handoffs/`, and `design/` are history and outside the doc scope.

Exit codes: exit 0 = clean (unarmed rows do not fail), exit 1 = present,
exit 2 = malformed input or undetermined (takes precedence over present).
Each result names the row, kind, repo and location; matched contents are never
printed. A final line counts present, undetermined, unarmed and provisional results.
Missing/unreadable repos and matched files never silently count as clean.

Options: `--root DIR`, `--repo PATH`, `--crontab-file FILE`. Without a crontab
file, the checker reads `crontab -l` without a shell. No installed crontab means
empty text; other failures mean undetermined for cron rows.
Relative registry roots, including the default `retired.d`, are resolved against
the doit checkout, never the current working directory. Missing and empty
registries are errors (exit 2).

The upload archive row initially uses the plan's `uuid` fallback, marked
`provisional = true`. The
upload-archive-once unit must tighten that pattern when its replacement lands.
