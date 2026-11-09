#!/bin/bash
# Auto-carry inbound items (operator 2026-10-05: "Don't we have something to pull that?").
# Every registered INBOUND row with 0 attempts is carried: v4 ledger ids directly, PR urls
# with title = PR title and body = the PR's inbound spec file. Runs as the Executor actor
# (allowed to emit spec-carried). One carry at a time; flock prevents overlap.
exec 9>/tmp/doit-auto-carry.lock; flock -n 9 || exit 0
cd "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")/.." || exit 1
export DOIT_LEDGER_FILE=L-executor-0001.jsonl
R=fredhead88/albert-scott-platform
./doit inbound 2>/dev/null | grep ' 0 attempt' | awk '{print $1}' | while read src; do
  # Thinker 2026-10-25: sources already carried by another path (e.g. a brief) never get a
  # spec-carried naming them, so carry.uncarried() keeps listing them. Skip those listed here.
  if grep -qF -- "$src " /home/albert/.do-it/bin/auto-carry.skip 2>/dev/null; then echo "$(date -u +%FT%TZ) skip $src (auto-carry.skip)"; continue; fi
  echo "$(date -u +%FT%TZ) carry $src"
  if [[ "$src" == https://github.com/* ]]; then
    n=${src##*/}
    title=$(gh pr view "$n" -R $R --json title -q .title)
    ref=$(gh pr view "$n" -R $R --json headRefOid -q .headRefOid)
    path=$(gh pr view "$n" -R $R --json files -q '.files[].path' | grep '\.md$' | head -1)
    body=$(gh api "repos/$R/contents/$path?ref=$ref" -q .content 2>/dev/null | base64 -d 2>/dev/null)
    [ -n "$body" ] || { echo "  skip: no spec body ($path)"; continue; }
    # Thinker 2026-10-24: the body names its project ("project: do-it-v2"); a hardcoded albert-scott
    # filed L-spec-1510 (a do-it-v2 relay fix) under the wrong project. Unknown -> skip, never guess.
    proj=$(printf '%s' "$body" | grep -oiE 'project:?[[:space:]]*`?(do-it-v2|albert-scott)' | head -1 | grep -oiE 'do-it-v2|albert-scott')
    case "$proj" in
      do-it-v2) repo=/home/albert/do-it-v2 ;;
      albert-scott) repo=/opt/albert-scott ;;
      *) if printf '%s' "$body" | grep -qiE 'do-it-v2|doit (tick|relay)|src/(fold|tick|relay|up)\.py'; then echo "  skip: project unclear (mentions do-it-v2), needs a human"; continue; fi
         proj=albert-scott; repo=/opt/albert-scott ;;
    esac
    ./doit carry "$src" --repo "$repo" --project "$proj" --title "$title" --body "$body" 2>&1 | tail -1 | cut -c1-200
  else
    ./doit carry "$src" --repo /opt/albert-scott --project albert-scott 2>&1 | tail -1 | cut -c1-200
  fi
done
