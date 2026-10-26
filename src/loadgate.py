"""One bounded, fail-open pressure observation per scheduler pass.

No launch is held here. Global off is [admission] mode in look.toml;
DOIT_ADMISSION affects only callers inheriting that environment (cron and
Executor panes differ). Revert enforcement before reverting this module;
the retained state file then becomes inert.
"""
from collections import Counter
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import re
import time
import tomllib

DEFAULTS = dict(mode='observe', mem_floor_gb=6, mem_hard_stop_gb=8,
                mem_swap_hard_stop_gb=12, mem_pressure_some_avg10_max=5,
                mem_pressure_full_avg10_max=1, load1_per_core_delay=3,
                load1_per_core_stop=4, running_peak_factor=.5,
                release_readings=2, max_wait_min=15,
                bypass_roles=['executor', 'tick', 'look', 'reaper', 'cleaner'],
                peak_gb=dict(window_gate=.9, builder=.5, grader=.6, reviewer=.4))
RULES = ('mem-hard-stop', 'mem-swap-hard-stop', 'mem-pressure', 'load-stop', 'mem-headroom', 'load-delay')
PROBE_ROLE = 'probe'
_TOML = Path(__file__).resolve().parent.parent / 'look.toml'
_SHORT = ('mem_avail_gb', 'swap_free_gb', 'mem_some10', 'mem_full10', 'load1_per_core', 'pswpout_s')
_FULL = ('shmem_gb', 'mem_some60', 'mem_full60', 'cpu_some10', 'io_some10', 'io_full10',
         'load1', 'procs_running', 'pswpin_s', 'cron_mem_some10', 'cron_mem_full10')


def _number(value):
    try:
        return type(value) in (int, float) and math.isfinite(value) and value >= 0
    except (OverflowError, ValueError):
        return False


def settings(env=None, toml_path=None):
    """Read only our own table; malformed settings never affect launches."""
    env = os.environ if env is None else env
    result, problems, table = deepcopy(DEFAULTS), [], {}
    try:
        with open(_TOML if toml_path is None else toml_path, 'rb') as f:
            table = tomllib.load(f).get('admission', {})
        if not isinstance(table, dict):
            raise ValueError('table')
    except FileNotFoundError:
        pass
    except Exception:
        problems.append('toml unreadable')
        table = {}
    for key, value in table.items():
        if key not in result or key == 'mode':
            continue
        if key == 'peak_gb':
            if not isinstance(value, dict):
                problems.append('bad peak_gb')
            else:
                for role, peak in value.items():
                    if _number(peak):
                        result[key][role] = peak
                    else:
                        problems.append('bad peak_gb.' + role)
            continue
        valid = (isinstance(value, list) and all(isinstance(r, str) for r in value)
                 if key == 'bypass_roles' else
                 type(value) is int and value >= 1 if key == 'release_readings' else _number(value))
        if valid:
            result[key] = value
        else:
            problems.append('bad ' + key)
    if result['max_wait_min'] > 60:
        result['max_wait_min'] = 60
        problems.append('max_wait_min capped at 60')
    mode = table.get('mode', 'observe')
    if not isinstance(mode, str):
        problems.append('bad mode')
        mode = 'observe'
    override = env.get('DOIT_ADMISSION', '')
    if isinstance(override, str) and override.strip():
        mode = override
    mode = mode.strip().lower()
    if mode not in ('off', 'observe', 'enforce'):
        mode = 'observe'
        problems.append('bad mode')
    if mode == 'enforce':
        mode = 'observe'
        problems.append('enforce only via $DOIT_ROOT/admission.toml; observing')
    result['mode'] = mode
    return result, problems


def _psi(text, prefix):
    result = {}
    for line in text.splitlines():
        tokens = line.split()
        if not tokens or tokens[0] not in ('some', 'full'):
            continue
        fields = dict(token.split('=', 1) for token in tokens[1:])
        for avg in (10, 60, 300):
            if 'avg' + str(avg) in fields:
                value = float(fields['avg' + str(avg)])
                if not _number(value):
                    raise ValueError('invalid PSI')
                result[f'{prefix}_{tokens[0]}{avg}'] = value
    if not result:
        raise ValueError('no PSI values')
    return result


def readings(proc_root='/proc', *, cgroup_root='/sys/fs/cgroup', prev=None, clock=time.time, ncpu=None):
    """Nine single opens; each source owns its failure and its output keys."""
    r = dict.fromkeys(_SHORT + _FULL + ('load5', 'load15', 'ncpu', 'pswpin', 'pswpout', 'cron_mem_gb'))
    for prefix in ('mem', 'cpu', 'io'):
        r.update(dict.fromkeys(f'{prefix}_{line}{avg}' for line in ('some', 'full') for avg in (10, 60, 300)))
    unreadable = set()
    try:
        r['t'] = clock()
    except Exception:
        r['t'] = None
    proc = Path(proc_root)
    cron = Path(cgroup_root) / 'system.slice/cron.service'

    def source(name, paths, parse):
        try:
            texts = [p.read_text() for p in paths]
            values = parse(*texts)
            r.update(values)
        except Exception:
            unreadable.add(name)

    def meminfo(text):
        fields = dict(line.split(':', 1) for line in text.splitlines() if ':' in line)
        if not fields:
            raise ValueError('no memory fields')
        values = {}
        for name, key in (('MemAvailable', 'mem_avail_gb'), ('SwapFree', 'swap_free_gb'), ('Shmem', 'shmem_gb')):
            if name in fields:
                value = float(fields[name].split()[0])
                if not _number(value):
                    raise ValueError('invalid memory')
                values[key] = value / 2**20
        return values

    source('meminfo', [proc / 'meminfo'], meminfo)
    for name, prefix in (('memory', 'mem'), ('cpu', 'cpu'), ('io', 'io')):
        source('pressure/' + name, [proc / 'pressure' / name], lambda text, p=prefix: _psi(text, p))

    def loadavg(text):
        loads = [float(v) for v in text.split()[:3]]
        if len(loads) != 3 or not all(_number(v) for v in loads):
            raise ValueError('loadavg')
        cpus = os.cpu_count() or 1 if ncpu is None else ncpu
        if not _number(cpus) or cpus <= 0:
            raise ValueError('ncpu')
        return dict(zip(('load1', 'load5', 'load15', 'ncpu', 'load1_per_core'), loads + [cpus, loads[0] / cpus]))

    def counters(text):
        fields = dict(line.split() for line in text.splitlines() if len(line.split()) == 2)
        if not fields:
            raise ValueError('counters')
        return {k: int(fields[k]) for k in ('pswpin', 'pswpout') if k in fields}

    def stat(text):
        if not text.strip():
            raise ValueError('stat')
        return {'procs_running': int(line.split()[1]) for line in text.splitlines() if line.startswith('procs_running ')}

    def cgroup(psi, current):
        values = _psi(psi, 'cron_mem')
        value = int(current)
        if value < 0:
            raise ValueError('memory.current')
        return {k: values[k] for k in ('cron_mem_some10', 'cron_mem_full10') if k in values} | {'cron_mem_gb': value / 2**30}

    source('loadavg', [proc / 'loadavg'], loadavg)
    source('vmstat', [proc / 'vmstat'], counters)
    source('stat', [proc / 'stat'], stat)
    source('cgroup', [cron / 'memory.pressure', cron / 'memory.current'], cgroup)
    for key in ('pswpin', 'pswpout'):
        r[key + '_s'] = None
        try:
            dt = r['t'] - prev['t']
            delta = r[key] - prev[key]
            if 0 < dt <= 3600 and delta >= 0:
                r[key + '_s'] = delta / dt
        except (KeyError, TypeError, ArithmeticError):
            pass
    r['unreadable'] = sorted(unreadable)
    return r


def admit(role, readings, running=None, *, admitted_gb=0., state=None, settings=None):
    s, r = DEFAULTS if settings is None else settings, readings or {}
    peaks = s['peak_gb']
    unknown = set()
    def peak(name):
        if name == PROBE_ROLE:
            return 0
        if name not in peaks:
            unknown.add(name)
        return peaks.get(name, 0)
    need = s['running_peak_factor'] * sum(n * peak(name) for name, n in (running or {}).items()) + admitted_gb + peak(role) + s['mem_floor_gb']
    detail = dict(failed=[], skipped=[], need_gb=need, unknown_roles=sorted(unknown))
    if role != PROBE_ROLE and role in s['bypass_roles']:
        return True, 'bypass', detail
    if all(r.get(k) is None for k in ('mem_avail_gb', 'swap_free_gb', 'load1_per_core', 'mem_some10', 'mem_full10')):
        return True, 'unreadable', detail
    mem, swap, load = (r.get(k) for k in ('mem_avail_gb', 'swap_free_gb', 'load1_per_core'))
    tests = ((mem is not None, lambda: mem < s['mem_hard_stop_gb']),
             (mem is not None and swap is not None, lambda: mem + swap < s['mem_swap_hard_stop_gb']),
             (r.get('mem_some10') is not None or r.get('mem_full10') is not None, lambda: (state or {}).get('psi_tripped', False)),
             (load is not None, lambda: load > s['load1_per_core_stop']),
             (mem is not None, lambda: mem < need),
             (load is not None, lambda: load > s['load1_per_core_delay']))
    for name, (available, fails) in zip(RULES, tests):
        if not available:
            detail['skipped'].append(name)
        elif fails():
            detail['failed'].append(name)
    return not detail['failed'], next(iter(detail['failed']), 'ok'), detail


def _fresh():
    return dict(v=1, t=0., pswpin=None, pswpout=None, psi_tripped=False,
                psi_release_streak=0, trip_since=None, holding={})


def _iso(t):
    return datetime.fromtimestamp(math.floor(t), timezone.utc).isoformat(timespec='seconds')


def _timestamp(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d(?:Z|\+00:00)', value):
        raise ValueError('UTC timestamp required')
    return datetime.fromisoformat(value.replace('Z', '+00:00')).timestamp()


def hold_key(item):
    def plain(c):
        return ' ' <= c <= '~' and c not in '\\"'
    if len(item) <= 80 and all(map(plain, item)):
        return item
    return ''.join(c if plain(c) else '_' for c in item[:71]) + '#' + hashlib.sha1(item.encode('utf-8')).hexdigest()[:8]


def load_state(path):
    try:
        with open(path, 'rb') as f:
            data = f.read(16385)
        if len(data) > 16384:
            raise ValueError('oversize')
        s = json.loads(data)
        if not isinstance(s, dict) or not _fresh().keys() <= s.keys():
            raise ValueError('shape')
        if type(s['v']) is not int or s['v'] != 1 or not _number(s['t']):
            raise ValueError('version/time')
        if type(s['psi_tripped']) is not bool or type(s['psi_release_streak']) is not int or s['psi_release_streak'] < 0:
            raise ValueError('hysteresis')
        for k in ('pswpin', 'pswpout'):
            if s[k] is not None and (type(s[k]) is not int or s[k] < 0):
                raise ValueError('counter')
        if s['trip_since'] is not None:
            _timestamp(s['trip_since'])
        if not isinstance(s['holding'], dict) or len(s['holding']) > 64:
            raise ValueError('holding')
        for key, since in s['holding'].items():
            if hold_key(key) != key:
                raise ValueError('key')
            _timestamp(since)
        return {k: s[k] for k in _fresh()}, True
    except Exception:
        return _fresh(), False


def _bounded_holding(holding):
    pairs = ((hold_key(k), v) for k, v in holding.items())
    return dict(sorted(pairs, key=lambda kv: (_timestamp(kv[1]), kv[0]))[:64])


def save_state(path, state):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    saved = {k: state[k] for k in _fresh()}
    saved['holding'] = _bounded_holding(saved['holding'])
    data = json.dumps(saved, allow_nan=False)
    if len(data.encode()) > 16384:
        raise ValueError('state oversize')
    tmp = path.with_name(path.name + f'.{os.getpid()}.tmp')
    try:
        tmp.write_text(data)
        os.replace(tmp, path)
    finally:
        tmp.unlink(missing_ok=True)


def advance(state, readings, settings):
    s = _fresh() | deepcopy(state)
    r = readings or {}
    some, full = settings['mem_pressure_some_avg10_max'], settings['mem_pressure_full_avg10_max']
    trip = any(r.get(k) is not None and r[k] > limit for k, limit in (('mem_some10', some), ('mem_full10', full)))
    slow = (r.get('mem_some60'), r.get('mem_full60'))
    if trip:
        s['psi_tripped'], s['psi_release_streak'] = True, 0
    elif s['psi_tripped'] and all(v is not None for v in slow):
        if slow[0] <= some and slow[1] <= full:
            s['psi_release_streak'] += 1
            if s['psi_release_streak'] >= settings['release_readings']:
                s['psi_tripped'], s['psi_release_streak'] = False, 0
        else:
            s['psi_release_streak'] = 0
    return s


@dataclass(frozen=True)
class Decision:
    would_hold: bool
    reason: str
    hold: bool


class Gate:
    def __init__(self, config, problems, clock, running):
        self.settings, self.problems, self.clock, self.running = config, problems, clock, running
        self.state, self.readings, self.path = _fresh(), None, None
        self.errors, self.unreadable, self.trips = [], [], []
        self.roles, self.would_hold = {}, set()
        self.checked, self.reasons, self.admitted_gb = 0, Counter(), 0.
        self.cost, self.age, self.test_hooks = 0., None, False

    def check(self, item, role):
        start = time.monotonic()
        try:
            if self.readings is not None:
                age = max(0, self.clock() - self.readings['t'])
                self.age = max(self.age or 0, age)
            ok, reason, _ = admit(role, self.readings, self.running, admitted_gb=self.admitted_gb,
                                  state=self.state, settings=self.settings)
            self.checked += 1
            self.roles[item] = role
            if not ok:
                self.would_hold.add(hold_key(item))
                self.reasons[reason] += 1
            return Decision(not ok, reason, False)
        except Exception as exc:
            self.errors.append('check: ' + str(exc))
            return Decision(False, 'error', False)
        finally:
            self.cost += time.monotonic() - start

    def note_launch(self, item, spawned):
        start = time.monotonic()
        try:
            if spawned and item in self.roles:
                self.admitted_gb += self.settings['peak_gb'].get(self.roles[item], 0)
        except Exception as exc:
            self.errors.append('note_launch: ' + str(exc))
        finally:
            self.cost += time.monotonic() - start

    def finish(self):
        start = time.monotonic()
        fields = dict(admission_mode=self.settings['mode'])
        try:
            now = self.clock()
            r = self.readings
            t = r['t'] if r is not None else now
            self.state.update(t=t, pswpin=r.get('pswpin') if r else None, pswpout=r.get('pswpout') if r else None)
            self.state['trip_since'] = (self.state['trip_since'] or _iso(t)) if self.trips else None
            old = self.state['holding']
            self.state['holding'] = _bounded_holding({key: old.get(key, _iso(now)) for key in self.would_hold})
            fields.update(admission_checked=self.checked, admission_would_hold=sum(self.reasons.values()),
                          admission_held=0, admission_reason=dict(self.reasons), admission_trips=self.trips,
                          admission_readings={k: round(r[k], 2) if r and r.get(k) is not None else None
                                              for k in _SHORT + (_FULL if self.trips else ())})
            if r is not None:
                fields['admission_sampled_at'] = _iso(t)
            if self.age is not None:
                fields['admission_reading_age_s'] = int(self.age)
            if self.state['trip_since'] is not None:
                fields['admission_trip_since'] = self.state['trip_since']
                fields['admission_trip_s'] = max(0, int(t - _timestamp(self.state['trip_since'])))
            if self.state['holding']:
                fields['admission_oldest_hold_s'] = max(0, int(now - min(map(_timestamp, self.state['holding'].values()))))
            if self.path is not None:
                save_state(self.path, self.state)
        except Exception as exc:
            self.errors.append('finish: ' + str(exc))
        if self.unreadable:
            fields['admission_unreadable'] = sorted(set(self.unreadable))
        if self.problems:
            fields['admission_settings'] = [p[:60] for p in self.problems[:5]]
        if self.errors:
            fields['admission_error'] = '; '.join(self.errors)[:160]
        if self.test_hooks:
            fields['admission_test_hooks'] = True
        self.cost += time.monotonic() - start
        fields['admission_cost_ms'] = int(self.cost * 1000)
        if len(json.dumps(fields).encode()) > 1024:
            fields.pop('admission_readings', None)
        if len(json.dumps(fields).encode()) > 1024:
            return dict(admission_mode=self.settings['mode'], admission_error='oversize')
        return fields


def begin(*, root=None, env=None, toml_path=None, proc_root=None, cgroup_root=None, clock=None, ncpu=None, running=None):
    start = time.monotonic()
    env = os.environ if env is None else env
    args = dict(proc_root=proc_root, cgroup_root=cgroup_root, toml_path=toml_path, ncpu=ncpu)
    hooks = False
    if env.get('DOIT_ADMISSION_TEST') == '1':
        for key in args:
            value = env.get('DOIT_ADMISSION_TEST_' + {'proc_root': 'PROC', 'cgroup_root': 'CGROUP', 'toml_path': 'TOML', 'ncpu': 'NCPU'}[key])
            if args[key] is None and value:
                args[key], hooks = value, True
    config, problems = settings(env=env, toml_path=args['toml_path'])
    if config['mode'] == 'off':
        return None
    gate = Gate(config, problems, time.time if clock is None else clock, running)
    gate.test_hooks = hooks
    try:
        if root is None:
            import fold
            root = fold.ROOT
        gate.path = Path(root) / 'state/admission.json'
        exists = gate.path.exists()
        gate.state, usable = load_state(gate.path)
        if exists and not usable:
            gate.unreadable.append('state')
        gate.readings = readings(args['proc_root'] or '/proc', cgroup_root=args['cgroup_root'] or '/sys/fs/cgroup',
                                 prev=gate.state, clock=gate.clock,
                                 ncpu=int(args['ncpu']) if args['ncpu'] is not None else None)
        gate.unreadable.extend(gate.readings['unreadable'])
        gate.state = advance(gate.state, gate.readings, config)
        gate.trips = admit(PROBE_ROLE, gate.readings, running, state=gate.state, settings=config)[2]['failed']
    except Exception as exc:
        gate.readings = None
        gate.trips = []
        gate.errors.append('begin: ' + str(exc))
    gate.cost = time.monotonic() - start
    return gate
