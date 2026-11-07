"""Refuse repository ledger mutations from tests, independent of fixture HOME."""
import os
from pathlib import Path
import pwd
import sys

REFUSAL = 'refused: test process cannot write live ledger'


def _test_name(filename):
    name = Path(filename).name
    return (name.startswith(('test_', 'dispatch_checks_')) and name.endswith('.py'))


def mark_test_process():
    """Recognize direct runners and active test frames; children inherit identity."""
    marked = os.environ.get('DOIT_TEST_PROCESS') == '1'
    args = sys.argv[:1]  # Project/subject arguments are payload, never identity.
    marked |= any(_test_name(arg) or Path(arg).name in
                  ('pytest', 'py.test', 'unittest') for arg in args)
    marked |= any(name in sys.modules for name in ('pytest', '_pytest', 'unittest'))
    frame = sys._getframe(1)
    try:
        while frame and not marked:
            marked = _test_name(frame.f_code.co_filename)
            frame = frame.f_back
    finally:
        del frame
    if marked:
        os.environ['DOIT_TEST_PROCESS'] = '1'
    return bool(marked)


def _account_root():
    return Path(pwd.getpwuid(os.getuid()).pw_dir) / '.do-it'


def protected_roots():
    """Only the account boundary is injectable; captured caller roots are additive."""
    return [_account_root(), *(Path(value) for key in
            ('DOIT_TEST_LIVE_ROOT', 'DOIT_TEST_MACHINE_ROOT')
            if (value := os.environ.get(key)))]


def _resolved(path):
    resolved = path.resolve()
    # Python versions differ on non-strict resolution of a symlink loop.
    # Check the nearest existing ancestor so ELOOP/EACCES cannot look safe.
    ancestor = resolved
    while True:
        try:
            ancestor.stat()
            return resolved
        except FileNotFoundError:
            ancestor = ancestor.parent


def check(destination):
    if not mark_test_process():
        return
    try:
        path = Path(destination)
        lexical = Path(os.path.abspath(path))
        resolved = _resolved(path)
        for root in protected_roots():
            events = root / 'events'
            boundaries = (Path(os.path.abspath(events)), _resolved(events))
            if any(p == boundary or boundary in p.parents
                   for p in (lexical, resolved) for boundary in boundaries):
                raise SystemExit(f'{REFUSAL}: {destination}')
    except (OSError, RuntimeError, ValueError) as exc:
        raise SystemExit(f'{REFUSAL}: unresolved destination ({type(exc).__name__})') from None


mark_test_process()
