"""Runner-local pytest evidence and base selection; loaded only in check children."""
import json
import os
from pathlib import Path
import tempfile

import pytest

_requests = []
_absent = []
_results = {}
_locations = {}
_directories = []
_collectors = {}
_examined = set()
_collection_error = False
_withheld = []


@pytest.hookimpl(tryfirst=True)
def pytest_configure(config):
    """Keep this plugin out of pytest processes the checked tests spawn.

    The runner requests the plugin through PYTEST_PLUGINS and a runner-only
    PYTHONPATH entry. A test that starts its own pytest (often in another
    checkout, with its own PYTHONPATH) would inherit PYTEST_PLUGINS and fail to
    import the plugin; such nested runs are not runner evidence anyway. The
    value is restored at unconfigure so a later in-process session still loads it.
    """
    value = os.environ.get('PYTEST_PLUGINS')
    _withheld.append(value)
    option = getattr(config, 'option', None)
    if getattr(option, 'numprocesses', None) or getattr(option, 'dist', 'no') not in (None, 'no'):
        return  # xdist workers are spawned from this environment and need the plugin
    kept = [n for n in (value or '').split(',') if n and n != __name__]
    if kept:
        os.environ['PYTEST_PLUGINS'] = ','.join(kept)
    else:
        os.environ.pop('PYTEST_PLUGINS', None)


@pytest.hookimpl(trylast=True)
def pytest_unconfigure(config):
    if not _withheld:
        return
    value = _withheld.pop()
    if value is None:
        os.environ.pop('PYTEST_PLUGINS', None)
    else:
        os.environ['PYTEST_PLUGINS'] = value


def _path(path):
    return os.path.relpath(Path(path).resolve(), os.environ['DOIT_RUNNER_TREE'])


def _matches(item, request):
    path, selectors = request
    if Path(item.path).resolve() != path:
        return False
    parts = item.nodeid.split('::')[1:]
    return all(i < len(parts) and (part == parts[i] or parts[i].startswith(part + '['))
               for i, part in enumerate(selectors))


@pytest.hookimpl(tryfirst=True)
def pytest_cmdline_main(config):
    global _collection_error
    _requests.clear()
    _absent.clear()
    _results.clear()
    _locations.clear()
    _directories.clear()
    _collectors.clear()
    _examined.clear()
    _collection_error = False
    if os.environ.get('DOIT_RUNNER_BASE') != '1':
        return
    args = []
    for arg in config.args:
        filename, *selectors = arg.split('::')
        path = (Path(config.invocation_params.dir) / filename).resolve()
        if path.suffix != '.py':
            args.append(arg)
            if path.is_dir():
                _directories.append(path)
            continue
        _requests.append((path, selectors))
        if not path.exists():
            _absent.append({'path': _path(path), 'selectors': selectors})
        else:
            # Collect the file, then select only nodes which exist at base.
            if filename not in args:
                args.append(filename)
    if not args:
        empty = Path(os.environ['DOIT_RUNNER_REPORT']) / 'empty'
        empty.mkdir(exist_ok=True)
        args = [str(empty)]
    config.args = args


def pytest_collectstart(collector):
    _collectors[collector.nodeid] = _path(collector.path)


def pytest_collectreport(report):
    global _collection_error
    if report.failed:
        _collection_error = True
    elif report.passed and report.nodeid.endswith('.py') and report.nodeid in _collectors:
        _examined.add(_collectors[report.nodeid])


@pytest.hookimpl(tryfirst=True)
def pytest_collection_modifyitems(config, items):
    if not _requests:
        return
    if not _collection_error:
        for path, selectors in _requests:
            if path.exists() and selectors and not any(_matches(item, (path, selectors)) for item in items):
                _absent.append({'path': _path(path), 'selectors': selectors})
    requested_paths = {path for path, _ in _requests}
    kept, removed = [], []
    for item in items:
        selected = (any(Path(item.path).resolve().is_relative_to(path) for path in _directories) or
                    Path(item.path).resolve() not in requested_paths or
                    any(_matches(item, request) for request in _requests))
        (kept if selected else removed).append(item)
    items[:] = kept
    if removed:
        config.hook.pytest_deselected(items=removed)


def pytest_itemcollected(item):
    _locations[item.nodeid] = _path(item.path)


def pytest_internalerror(excrepr, excinfo):
    global _collection_error
    _collection_error = True


def pytest_runtest_logreport(report):
    # xdist's controller receives reports but collection lives in its workers.
    # The workers write their own complete node evidence to the shared report dir.
    if report.nodeid not in _locations:
        return
    entry = _results.setdefault(report.nodeid, {'path': _locations[report.nodeid], 'phases': {}})
    entry['phases'][report.when] = 'skipped' if hasattr(report, 'wasxfail') else report.outcome


def pytest_sessionfinish(session, exitstatus):
    if (os.environ.get('DOIT_RUNNER_BASE') == '1' and exitstatus == 5
            and _absent and not _collection_error):
        session.exitstatus = 0
    data = {'step': int(os.environ['DOIT_RUNNER_STEP']), 'absent': _absent,
            'results': _results, 'collection_error': _collection_error,
            'collected': _locations, 'examined': sorted(_examined)}
    with tempfile.NamedTemporaryFile(mode='w', dir=os.environ['DOIT_RUNNER_REPORT'],
                                     prefix='pytest-', suffix='.json', delete=False) as output:
        json.dump(data, output)
