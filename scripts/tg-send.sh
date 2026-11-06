#!/usr/bin/env bash
# Sole outbound transport. Secrets reach curl only through its config stdin.
set +x
set -euo pipefail
ROOT=${DOIT_ROOT:-$HOME/.do-it}
SRC=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/../src" && pwd)
sender=${TG_SENDER:-}
document=''
mode=text
if [ "${1:-}" = --document ]; then
  mode=document
  shift
  document=${1:-}
  if [ "$#" -gt 0 ]; then shift; fi
  MSG="$*"
  # Even an empty caption must not consume the caller's stdin.
elif [ "$#" -gt 0 ]; then
  MSG="$*"
else
  MSG=$(cat)
fi
if ident=$(python3 "$SRC/tg_say.py" --prepare "$sender" "$MSG" "$document" "$mode"); then :; else
  rc=$?; printf '000\n'; exit "$rc"
fi
code=000
rc=1
# read is a shell builtin: no credential-bearing helper argv or diagnostics.
if { IFS= read -r TOK < "$ROOT/telegram.token" || [ -n "${TOK:-}" ]; } 2>/dev/null &&
   { IFS= read -r CHAT < "$ROOT/telegram/chat_id" || [ -n "${CHAT:-}" ]; } 2>/dev/null &&
   [[ "$TOK" =~ ^[A-Za-z0-9:_-]+$ ]] && [[ "$CHAT" =~ ^-?[0-9]+$ ]]; then
  args=(-sS -m 20 -K - -o /dev/null -w '%{http_code}\n')
  endpoint=sendMessage
  if [ "$mode" = document ]; then
    endpoint=sendDocument
    # Quote form filenames using curl's multipart quoted-string grammar.
    escaped=${document//\\/\\\\}; escaped=${escaped//\"/\\\"}
    args+=(--form-string "chat_id=$CHAT" --form-string "caption=$MSG" -F "document=@\"$escaped\"")
  else
    args+=(--data-urlencode "chat_id=$CHAT" --data-urlencode "text=$MSG")
  fi
  set +e
  code=$(printf 'url = "https://api.telegram.org/bot%s/%s"\n' "$TOK" "$endpoint" | curl "${args[@]}" 2>/dev/null)
  rc=$?
  set -e
fi
[[ "$code" =~ ^[0-9]{3}$ ]] || code=000
if ! python3 "$SRC/tg_say.py" --result "$sender" "$MSG" "$ident" "$code"; then
  printf '%s\n' "$code"; exit 1
fi
printf '%s\n' "$code"
[ "$rc" -eq 0 ] && [ "$code" = 200 ]
