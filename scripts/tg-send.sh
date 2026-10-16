#!/usr/bin/env bash
# Versioned sender: every attempted send has a private, append-only audit row.
set -euo pipefail
ROOT=${DOIT_ROOT:-$HOME/.do-it}
IFS= read -r TOK < "$ROOT/telegram.token" || [ -n "${TOK:-}" ] || { echo 'no token' >&2; exit 2; }
IFS= read -r CHAT < "$ROOT/telegram/chat_id" || [ -n "${CHAT:-}" ] || { echo 'no chat_id' >&2; exit 2; }
if [ "$#" -gt 0 ]; then MSG="$*"; else MSG=$(cat); fi
sender=${TG_SENDER:-$(ps -o comm= -p "$PPID" 2>/dev/null || echo unknown)}
# Log before calling the transport, including failed/ambiguous attempts. Do not
# log tokens, chat IDs or subsequent message lines. Failure to log stops send.
python3 - "$ROOT/logs/tg-send.jsonl" "$sender" "${MSG%%$'\n'*}" <<'PY'
import datetime, fcntl, json, os, pathlib, sys
path = pathlib.Path(sys.argv[1])
path.parent.mkdir(parents=True, exist_ok=True)
fd = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
with os.fdopen(fd, 'a') as out:
    fcntl.flock(out, fcntl.LOCK_EX)
    os.fchmod(out.fileno(), 0o600)
    json.dump(dict(time=datetime.datetime.now(datetime.timezone.utc).isoformat(timespec='seconds'),
                   first_line=sys.argv[3], sender=sys.argv[2].strip()), out, ensure_ascii=False)
    out.write('\n')
    out.flush()
    os.fsync(out.fileno())
PY
curl -sS -m 20 "https://api.telegram.org/bot${TOK}/sendMessage" \
  --data-urlencode "chat_id=${CHAT}" --data-urlencode "text=${MSG}" -o /dev/null -w '%{http_code}\n'
