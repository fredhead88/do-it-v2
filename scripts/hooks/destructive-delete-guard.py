#!/usr/bin/env python3
"""PreToolUse guard: deny a recursive delete that can reach a protected root.

This is `do-it-v2`'s own canonical copy of the guard proven at
`/opt/albert-scott/.claude/hooks/destructive-delete-guard.py`, written there
after this command ran on the builder box as the albert user and removed the
entire home directory, including the whole DO-IT v2 ledger (~/.do-it) and the
tool repo (~/do-it-v2):

    rm -rf "$DOIT_ROOT" "$HOME" 2>/dev/null; cd .../l-spec-0122 && wc -l src/test_up.py

That is the ordinary test-harness cleanup idiom. Inside a test it is
harmless, because the harness sets HOME and DOIT_ROOT to sandboxed
directories first (see `src/harness.py`). The line is only catastrophic when
it runs in a shell where those names still point at the real thing. No
permission prompt would have caught it either, because panes run with
--dangerously-skip-permissions by necessity: the system is autonomous, and a
prompt on every command is not an option.

So this is a HOOK, not a permission. PreToolUse hooks are deterministic and
run regardless of permission mode, which means full autonomy is preserved and
exactly one class of command -- a recursive delete that can reach a protected
root -- is refused. Everything else passes silently.

Deliberately NOT clever: it does not try to emulate the shell. It resolves the
few expansions that matter (~, $HOME, ${HOME}, $DOIT_ROOT, and a handful of
other roots) and refuses anything whose target is, or contains, a protected
path. An unresolvable variable inside a recursive delete is refused as
UNDETERMINED rather than guessed at -- undetermined is never clean, and this
is the one place where guessing wrong costs a home directory.

Ported logic verbatim (resolve -> hits_protected -> deny-or-pass); the sets
below differ from the proven `albert-scott` copy only by dropping the two
entries that name that OTHER repository (`/opt`, `/opt/albert-scott`) and the
bare filesystem roots that copy also carries (`/`, `/home`, `/etc`, `/var`,
`/usr`, `/boot`, `/root`, `/srv`, `/tmp`) -- this copy's own PROTECTED is the
narrower, do-it-v2-scoped set the spec's Constraints name explicitly. An
ancestor of any protected root is still caught: deleting `/home` still denies,
because `hits_protected` below flags a path that would take a protected root
down WITH it, not only an exact match.
"""
import json
import os
import re
import shlex
import sys

HOME = os.path.expanduser("~")
DOIT_ROOT = os.environ.get("DOIT_ROOT") or os.path.join(HOME, ".do-it")

# Paths that must never be the target of a recursive delete, and never be an
# ancestor of one. do-it-v2-scoped: the real ledger root, the real tool
# checkout, and this box's Claude config -- the exact things the 2026-09-22
# incident destroyed.
PROTECTED = {
    HOME,
    DOIT_ROOT,
    os.path.join(HOME, ".claude"),
    os.path.join(HOME, "do-it-v2"),
}

# Variables we can resolve ourselves. A recursive delete naming any OTHER
# variable is refused as undetermined -- see module docstring.
KNOWN_VARS = {
    "HOME": HOME,
    "DOIT_ROOT": DOIT_ROOT,
    "PWD": os.environ.get("PWD", ""),
    "CLAUDE_PROJECT_DIR": os.environ.get("CLAUDE_PROJECT_DIR", ""),
}

VAR = re.compile(r"\$\{?([A-Za-z_][A-Za-z0-9_]*)\}?")
# `rm` with both recursive and force, in any flag arrangement: -rf, -fr, -r -f,
# --recursive --force. `rm -r` alone is equally destructive, so -r is enough.
RECURSIVE = re.compile(r"^-(?!-)[a-zA-Z]*[rR]|^--recursive$")


def deny(reason):
    print(json.dumps({
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }))
    sys.exit(0)


def norm(path):
    return os.path.normpath(path).rstrip("/") or "/"


def resolve(token):
    """(resolved_path, unresolved_var_name). Only one of the two is ever set."""
    if token.startswith("~"):
        token = HOME + token[1:]
    unresolved = None

    def sub(m):
        nonlocal unresolved
        name = m.group(1)
        if name in KNOWN_VARS and KNOWN_VARS[name]:
            return KNOWN_VARS[name]
        unresolved = name
        return ""

    return VAR.sub(sub, token), unresolved


def hits_protected(path):
    """The path IS a protected root, or is an ancestor of one (so deleting it
    takes the protected root with it). A path strictly INSIDE a protected root
    is fine -- that is ordinary work."""
    p = norm(path)
    if p in PROTECTED:
        return p
    for prot in PROTECTED:
        if norm(prot).startswith(p + "/"):
            return prot
    return None


def check(command):
    try:
        tokens = shlex.split(command, comments=False)
    except ValueError:
        return  # unparseable quoting; other guards and the shell itself apply

    i = 0
    while i < len(tokens):
        if os.path.basename(tokens[i]) != "rm":
            i += 1
            continue
        i += 1
        recursive, targets = False, []
        while i < len(tokens):
            t = tokens[i]
            if t in (";", "&&", "||", "|", "&"):
                break
            if t.startswith("-") and t != "-":
                if RECURSIVE.match(t):
                    recursive = True
            else:
                targets.append(t)
            i += 1
        if not recursive:
            continue
        for t in targets:
            path, unresolved = resolve(t)
            if unresolved:
                deny(
                    f"Refusing `rm -r` on {t!r}: ${unresolved} is not resolvable from "
                    f"this hook, so whether it reaches a protected root is UNDETERMINED. "
                    f"On 2026-09-22 the idiom `rm -rf \"$DOIT_ROOT\" \"$HOME\"` — safe in a "
                    f"test harness, where those are temp dirs — ran in a real shell and "
                    f"deleted the entire home directory: the DO-IT ledger, the tool repo "
                    f"and every dotfile. Delete an explicit literal path instead, or set "
                    f"HOME to a sandbox for the whole run."
                )
            if not path.strip():
                continue  # `rm -rf ""` removes nothing
            prot = hits_protected(path)
            if prot:
                deny(
                    f"Refusing `rm -r` on {t!r} (resolves to {norm(path)!r}): it would "
                    f"delete or contain the protected root {prot!r}. This is the exact "
                    f"command class that wiped /home/albert on 2026-09-22. If you truly "
                    f"mean it, a human runs it by hand in a terminal — never an agent."
                )


def main():
    try:
        payload = json.load(sys.stdin)
    except Exception:
        sys.exit(0)
    if payload.get("tool_name") != "Bash":
        sys.exit(0)
    command = (payload.get("tool_input") or {}).get("command") or ""
    check(command)
    sys.exit(0)


if __name__ == "__main__":
    main()
