"""Browser review readiness, before packet preparation or attempt accounting.

Provision ROOT/review-mcp.json from mcp/chrome-devtools.json. Missing or broken
configuration is an operator input, not a failed reviewer attempt.
"""
import json
import re

import dispatch
import fold


def required(sid, options):
    if not options or options[0] == 'gates-only':
        return False
    return options[0] == 'needs-browser' or bool(
        re.search(r'\[ui\]', dispatch.spec_path(sid).read_text(), re.I))


def problem():
    path = fold.ROOT / 'review-mcp.json'
    try:
        config = json.loads(path.read_text())
    except FileNotFoundError:
        return 'missing review-mcp.json'
    except (OSError, ValueError):
        return 'invalid review-mcp.json'
    servers = config.get('mcpServers') if isinstance(config, dict) else None
    browser = servers.get('chrome-devtools') if isinstance(servers, dict) else None
    if (not isinstance(browser, dict)
            or not isinstance(browser.get('command'), str) or not browser['command'].strip()
            or not isinstance(browser.get('args', []), list)
            or not all(isinstance(arg, str) for arg in browser.get('args', []))):
        return 'invalid review-mcp.json'
    return None


def config(sid, options):
    """Return exactly the config whose browser readiness was checked."""
    if not required(sid, options):
        return None
    reason = problem()
    if reason:
        raise ValueError('review-readiness: ' + reason)
    return fold.ROOT / 'review-mcp.json'


def hold(step, options, *, dry_run=False):
    if step.action != 'review' or not required(step.item, options):
        return None
    reason = problem()
    if not reason:
        return None
    reason = 'review-readiness: ' + reason
    if not dry_run:
        import tick
        path = tick.tick_path()
        # Reopen the writer's rows: callers may reuse a stale pass snapshot.
        rows = [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []
        if not any(e.get('subject') == step.item and e.get('type') == 'autodispatch-skipped'
                   and e.get('reason') == reason and e.get('since') == step.since for e in rows):
            refused = dispatch.emit(path, {'subject': step.item}, 'autodispatch-skipped', strict=True,
                                    role='reviewer', rule='review', since=step.since, reason=reason)
            if refused:
                raise ValueError('review readiness record refused: ' + str(refused))
    return reason
