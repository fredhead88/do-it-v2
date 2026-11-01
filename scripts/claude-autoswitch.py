#!/usr/bin/env python3
"""claude-autoswitch: keep Claude Code on a login that has usage left.

Operator ask 2026-10-30 06:58Z: at 99% usage switch to login 2 automatically,
and switch back when the other one resets.

Every run (cron, every 5 min):
- reads session (5-hour) and weekly usage for both saved accounts from the
  OAuth usage endpoint (tokens are read from disk, never printed);
- refreshes unreadable saved credentials with the CLI OAuth protocol;
- alerts once after two unreadable passes for the active account;
- if active usage is unreadable and a pane shows a quota pause, switches to a
  readable account below its limits;
- if the active account is at or above SWITCH_AT on either limit and the other
  account is below it, runs `claude-switch.sh switch <other>`;
- if the active account is not the primary (1) and the primary is back below
  BACK_AT on both limits (or its last known weekly reset has passed and it
  cannot be read), switches back to 1.
A switch is never started within COOLDOWN_MIN of the previous one (claude-switch
needs ~12 min to settle). Every decision is logged; each switch is sent to Telegram.
"""
import datetime as dt
import json
import os
import re
import tempfile
import pathlib
import subprocess
import urllib.request

HOME = pathlib.Path.home()
DIR = HOME / ".do-it/private/claude-accounts"
STATE = HOME / ".do-it/state/claude-autoswitch.json"
LOG = HOME / ".do-it/logs/claude-autoswitch.log"
SWITCH = HOME / ".do-it/bin/claude-switch.sh"
TG = HOME / ".do-it/telegram/tg-send.sh"
SWITCH_AT, BACK_AT, COOLDOWN_MIN, PRIMARY = 99, 90, 15, "1"
NOW = dt.datetime.now(dt.timezone.utc)


def log(msg):
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(f"{NOW:%Y-%m-%dT%H:%M:%SZ} {msg}\n")


LIVE = HOME / ".claude/.credentials.json"


def save_json(path, value):
    """Replace private state atomically, without loosening credential permissions."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=path.name + ".", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as handle:
            json.dump(value, handle, indent=1)
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def read_usage(credentials):
    tok = credentials["claudeAiOauth"]["accessToken"]
    req = urllib.request.Request("https://api.anthropic.com/api/oauth/usage", headers={
        "Authorization": "Bearer " + tok, "anthropic-beta": "oauth-2025-04-20"})
    with urllib.request.urlopen(req, timeout=15) as response:
        d = json.load(response)
    return {"session": float((d.get("five_hour") or {}).get("utilization") or 0),
            "weekly": float(d["seven_day"]["utilization"]),
            "weekly_reset": d["seven_day"]["resets_at"]}


def refresh(credentials):
    """Use the installed CLI's OAuth refresh protocol; never log credentials."""
    oauth = credentials["claudeAiOauth"]
    body = dict(grant_type="refresh_token", refresh_token=oauth["refreshToken"],
                client_id="9d1c250a-e61b-44d9-88ed-5944d1962f5e")
    if oauth.get("scopes"):
        body["scope"] = " ".join(oauth["scopes"])
    req = urllib.request.Request("https://platform.claude.com/v1/oauth/token",
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as response:
        result = json.load(response)
    updated = dict(oauth, accessToken=result["access_token"],
                   refreshToken=result.get("refresh_token", oauth["refreshToken"]),
                   expiresAt=int(NOW.timestamp() * 1000 + result["expires_in"] * 1000))
    return dict(credentials, claudeAiOauth=updated)


def usage(n, live=False):
    """Read the active live login; refresh inactive saved credentials once."""
    saved = DIR / f"{n}.json"
    if live:
        try:
            credentials = json.loads(LIVE.read_text())
            reading = read_usage(credentials)
            # Only update the same saved account, never a different manual login.
            try:
                prior = json.loads(saved.read_text())
                if any(credentials["claudeAiOauth"].get(k) and
                       credentials["claudeAiOauth"].get(k) == prior["claudeAiOauth"].get(k)
                       for k in ("accessToken", "refreshToken")):
                    save_json(saved, credentials)
            except (OSError, ValueError, KeyError, TypeError):
                pass
            return reading
        except Exception as exc:
            log(f"account {n} live usage unreadable: {type(exc).__name__}")
            # The CLI owns the active refresh token. Rotating only its saved
            # copy would invalidate the live login's next refresh.
            return None
    try:
        credentials = json.loads(saved.read_text())
        try:
            return read_usage(credentials)
        except Exception:
            credentials = refresh(credentials)
            save_json(saved, credentials)
            return read_usage(credentials)
    except Exception as exc:
        log(f"account {n} usage unreadable: {type(exc).__name__} {getattr(exc, 'code', '')}".rstrip())
        return None


def usage_paused():
    """Inspect current pane tails; unreadable tmux is not a quota signal."""
    try:
        panes = subprocess.run(["tmux", "list-panes", "-a", "-F", "#{pane_id}"],
                               capture_output=True, text=True, timeout=10)
        if panes.returncode:
            return False
        for pane in panes.stdout.splitlines():
            tail = subprocess.run(["tmux", "capture-pane", "-p", "-t", pane, "-S", "-12"],
                                  capture_output=True, text=True, timeout=10)
            if tail.returncode == 0 and re.search(
                    r"you(?:'|’)ve hit your limit|usage limit (?:reached|exceeded)|"
                    r"out of extra usage|weekly limit reached", tail.stdout, re.I):
                return True
    except (OSError, subprocess.SubprocessError):
        pass
    return False


def notify(message):
    try:
        subprocess.run([str(TG), message], capture_output=True, timeout=60)
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"Telegram failed: {type(exc).__name__}")


def worst(u):
    return max(u["session"], u["weekly"])


def main():
    state = json.loads(STATE.read_text()) if STATE.exists() else {}
    cur = (DIR / "current").read_text().strip() if (DIR / "current").exists() else "?"
    if cur not in ("1", "2"):
        log(f"current account undetermined ({cur!r}); no action")
        return
    other = "2" if cur == "1" else "1"
    u = {n: usage(n, live=(n == cur)) for n in ("1", "2")}
    # Two unreadable passes on this active account form one blind episode.
    blind = 0 if u[cur] else (state.get("blind_runs", 0) if state.get("blind_account") == cur else 0) + 1
    state["blind_account"] = cur
    state["blind_runs"] = blind
    if blind == 2:
        notify(f"Claude auto-switch blind: cannot read usage for active account {cur} "
               "for two consecutive passes; quota protection is unavailable.")
    for n in ("1", "2"):
        if u[n]:
            state[f"last_{n}"] = u[n]
    last = state.get("last_switch")
    cooling = last and NOW - dt.datetime.fromisoformat(last) < dt.timedelta(minutes=COOLDOWN_MIN)
    target, why = None, ""
    if u[cur] and worst(u[cur]) >= SWITCH_AT:
        if u[other] is None or worst(u[other]) < SWITCH_AT:
            target, why = other, f"account {cur} at {worst(u[cur]):.0f}%"
        else:
            log(f"both accounts at or above {SWITCH_AT}%; staying on {cur}")
    elif u[cur] is None and u[other] is not None and worst(u[other]) < SWITCH_AT and usage_paused():
        target, why = other, f"account {cur} usage unreadable and a pane is paused for quota"
    elif cur != PRIMARY:
        p = u[PRIMARY]
        if p is not None and worst(p) < BACK_AT:
            target, why = PRIMARY, f"account {PRIMARY} is back to {worst(p):.0f}%"
        elif p is None:
            reset = (state.get(f"last_{PRIMARY}") or {}).get("weekly_reset")
            if reset and NOW > dt.datetime.fromisoformat(reset):
                target, why = PRIMARY, f"account {PRIMARY}'s weekly limit reset at {reset[:16]}Z"
    summary = " ".join(
        f"{n}:" + ("?" if not u[n] else "%.0f/%.0f" % (u[n]["session"], u[n]["weekly"]))
        for n in ("1", "2"))
    if target and cooling:
        log(f"active {cur}; {summary}; would switch to {target} ({why}) but cooling down")
    elif target:
        subprocess.Popen(["nohup", str(SWITCH), "switch", target], stdout=subprocess.DEVNULL,
                         stderr=subprocess.DEVNULL, start_new_session=True)
        state["last_switch"] = NOW.isoformat()
        log(f"active {cur}; {summary}; SWITCH to {target}: {why}")
        notify(f"Claude login switched automatically to account {target}: {why}.")
    else:
        log(f"active {cur}; {summary} (session/weekly %); no change")
    save_json(STATE, state)


if __name__ == "__main__":
    main()
