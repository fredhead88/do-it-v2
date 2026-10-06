#!/usr/bin/env python3
"""portal-traffic — a GET-only, host-pinned, throttled request helper for owed
checks that need a traffic threshold (L-spec-0682 R2).

  portal-traffic.py PATH [PATH ...] [--count N]

It exists so a check like "N requests reach the portal" can be satisfied with
real requests made as the read-only verifier account (`verify@albertscott.com`,
named here and nowhere else). Hard rules, each enforced BEFORE any byte is sent:

  * method is GET — anything else raises `Refused`;
  * the URL's host must equal the configured portal host, `DOIT_PORTAL_HOST`
    (required; there is no default) — any other host raises `Refused`;
  * the credential is read from the environment only, `DOIT_VERIFIER_PORTAL_TOKEN`,
    and goes only into the Authorization header; it is never printed, logged,
    or placed in the returned evidence (status, a body hash and an excerpt only).
  * requests are throttled (`MIN_GAP_S` apart) and capped (`MAX_REQUESTS`).

`fetch()` takes an injectable `opener` so a test proves the refusals happen
before any socket send; the default opener is `urllib.request.urlopen`.
"""
import argparse, hashlib, os, sys, time, urllib.parse, urllib.request

MIN_GAP_S = 1.0
MAX_REQUESTS = 200
TOKEN_ENV, HOST_ENV, SCHEME_ENV = "DOIT_VERIFIER_PORTAL_TOKEN", "DOIT_PORTAL_HOST", "DOIT_PORTAL_SCHEME"
_last_sent = [0.0]


class Refused(Exception):
    """A request refused before anything was sent."""


def _host(env):
    host = (env.get(HOST_ENV) or "").strip().lower()
    if not host:
        raise Refused(f"{HOST_ENV} is not set — no portal host configured")
    return host


def fetch(url, *, method="GET", env=None, opener=None, sleep=time.sleep, clock=time.monotonic):
    """One throttled GET. Returns `{url, status, body_sha256, body_excerpt}`."""
    env = os.environ if env is None else env
    if str(method).upper() != "GET":
        raise Refused(f"method {method!r} refused: GET only")
    parts = urllib.parse.urlsplit(url)
    if parts.scheme not in ("http", "https") or (parts.hostname or "").lower() != _host(env).split(":")[0]:
        raise Refused(f"host {parts.hostname!r} is not the configured portal host")
    token = env.get(TOKEN_ENV)
    if not token:
        raise Refused(f"{TOKEN_ENV} is not set — no verifier credential in the environment")
    wait = MIN_GAP_S - (clock() - _last_sent[0])
    if wait > 0 and _last_sent[0]:
        sleep(wait)
    _last_sent[0] = clock()
    req = urllib.request.Request(url, method="GET", headers={"Authorization": f"Bearer {token}"})
    with (opener or urllib.request.urlopen)(req, timeout=30) as r:
        body = r.read()
        status = getattr(r, "status", None) or r.getcode()
    text = body.decode("utf-8", "replace")
    return {"url": url, "status": int(status), "body_sha256": hashlib.sha256(body).hexdigest(),
            "body_excerpt": text.replace(token, "[redacted]")[:200]}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", help="paths on the configured portal host")
    ap.add_argument("--count", type=int, default=1, help="times to request each path (capped)")
    a = ap.parse_args(argv)
    if a.count * len(a.paths) > MAX_REQUESTS:
        print(f"portal-traffic: over the {MAX_REQUESTS}-request cap", file=sys.stderr)
        return 2
    try:
        base = f"{os.environ.get(SCHEME_ENV) or 'https'}://{_host(os.environ)}"
        for _ in range(a.count):
            for p in a.paths:
                r = fetch(base + p)
                print(f"{r['status']} {r['url']} {r['body_sha256'][:12]}")
    except Refused as x:
        print(f"portal-traffic: refused: {x}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
