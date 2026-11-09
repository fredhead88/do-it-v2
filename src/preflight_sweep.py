"""Fail-fast stack -> static -> guards -> changed-tests, within one wall budget."""
from dataclasses import dataclass, field
import json
import math
import multiprocessing
import os
from pathlib import Path, PurePosixPath
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import xml.etree.ElementTree as ET

import cpu_share
import fold
import grading_env
import merge_gate
import rechain_check as stacker
import window_prewindow

# The one guard inventory. Its comment directive carries the one exclusion.
GUARDS = Path(__file__).resolve().parent.parent/'lane/guard-tests.txt'
GUARD_ENV = 'DOIT_PREFLIGHT_GUARDS'
# Project guard policy. A project absent here has an undetermined policy.
GUARD_PROJECTS = {'albert-scott': 'inventory', 'do-it-v2': 'empty'}
PYTHONPATHS = {'albert-scott': 'api:.', 'do-it-v2': 'src:.'}
PG_TRIGGERS = ('short_transactions_scratch', 'ScratchRequired', 'AS_LOCAL_PG_ADMIN')
PROVISION_RESERVE_S = 0.5


class BaseRed(Exception):
    """The failing node fails identically on the base tree: undetermined."""


@dataclass
class SweepResult:
    ok: bool = False
    stage: str = 'stack'
    failed: str = '-'
    findings: list = field(default_factory=list)
    steps: list = field(default_factory=list)
    cpu_s: float = 0.0
    wall_s: float = 0.0
    undetermined: bool = False
    trace: list = field(default_factory=list)
    selected: list = field(default_factory=list)
    excluded: dict = field(default_factory=dict)
    project: str = '-'
    reason: str = ''
    guards: int = 0
    exclusion: tuple | None = None


def finding(stage, cls, detail, owner='-'):
    return dict(stage=stage, **{'class': cls}, detail=detail, owner=owner)


def changed(repo, base, tip):
    return set(stacker.git(repo,'diff','--no-renames','--name-only','-z',base,tip,'--').stdout.decode().split('\0'))-{''}


def safe_path(path):
    p=PurePosixPath(path)
    return bool(path and not p.is_absolute() and '..' not in p.parts
                and not path.startswith('-') and all(ord(c)>=32 and ord(c)!=127 for c in path))


class Reader:
    def __init__(self, repo, tip):
        self.repo,self.tip=repo,tip

    def source(self, paths, path):
        return stacker.git(self.repo,'show',self.tip+':'+path).stdout.decode()


def static(repo, base, entries, steps):
    """Only a resolver-proved mechanical rewrite can excuse grant overlap."""
    owned,grants={},{}
    for spec,branch,sha in entries:
        fork=stacker.out(repo,'merge-base',base,sha)
        owned[spec]=changed(repo,fork,sha)
        try:
            grant=merge_gate.grant_from_text((fold.ROOT/'content'/(spec+'.md')).read_text())
            grant=[p[2:] if p.startswith('./') else p for p in grant]
            if any(not safe_path(p.lstrip('./')) or p.startswith(('/', '../')) for p in grant):
                raise ValueError('unsafe grant')
        except (OSError,ValueError,merge_gate.Undetermined):
            return [finding('static','writes','missing or invalid Writes grant',spec)],owned
        outside=sorted(p for p in owned[spec] if not merge_gate.granted(p,grant))
        if outside:
            return [finding('static','writes','outside owning Writes grant: '+', '.join(outside),spec)],owned
        grants[spec]=grant
    for index,(spec,_,_) in enumerate(entries):
        proof=steps[index].metadata
        proved=set(proof.get('files',[])) if proof.get('mechanical') is True else set()
        for prior,_,_ in entries[:index]:
            overlap=window_prewindow.overlap([g.rstrip('/') for g in grants[spec]],
                                             [g.rstrip('/') for g in grants[prior]])
            actual=owned[spec]&owned[prior]
            # Broad grants are still overlapping even if the actual edit is
            # mechanical. Every overlapping grant pair must name proved files.
            pairs=[(a,b) for a in grants[spec] for b in grants[prior]
                   if window_prewindow.overlap([a.rstrip('/')],[b.rstrip('/')])]
            exempt=bool(proved) and actual<=proved and all(a in proved and b in proved for a,b in pairs)
            if (actual or overlap) and not exempt:
                return [finding('static','overlap','overlaps '+prior+': '+', '.join(sorted(actual) or list(overlap)),spec)],owned
    tip=steps[-1].tip
    paths=set(stacker.git(repo,'ls-tree','-r','--name-only','-z',tip).stdout.decode().split('\0'))-{''}
    reader=Reader(repo,tip)
    try:
        window_prewindow.revisions(reader,paths)
    except ValueError:
        return [finding('static','alembic-heads','invalid Alembic candidate graph',owner(
            [p for p in changed(repo,base,tip) if p.startswith(window_prewindow.VERSIONS)],owned))],owned
    base_paths=set(stacker.git(repo,'ls-tree','-r','--name-only','-z',base).stdout.decode().split('\0'))-{''}
    for path in sorted(changed(repo,base,tip)&paths):
        if not path.endswith('.py'):
            continue
        source=reader.source(paths,path)
        violation=lambda s: any(t in s for t in PG_TRIGGERS) and 'pg_scratch' not in s
        if violation(source) and not (path in base_paths and violation(Reader(repo,base).source(base_paths,path))):
            return [finding('static','pg-marker','missing pg_scratch marker: '+path,owner([path],owned))],owned
    return [],owned


def owner(paths, owned):
    owners={spec for spec,changed_paths in owned.items() if set(paths)&changed_paths}
    return next(iter(owners)) if len(owners)==1 else '-'


def is_test(path, project=None):
    p=PurePosixPath(path)
    return ((path.startswith(('tests/','api/tests/')) and (p.name.startswith('test_') or p.name.endswith('_test.py')) and path.endswith('.py'))
            or (project=='do-it-v2' and path.startswith('src/') and p.name.startswith('test_') and path.endswith('.py'))
            or (path.startswith('dashboard/src/') and path.endswith(('.test.ts','.test.tsx'))))


def targets(repo, base, tip, project=None):
    paths=changed(repo,base,tip)
    present=set(stacker.git(repo,'ls-tree','-r','--name-only','-z',tip).stdout.decode().split('\0'))
    selected,excluded=[],{}
    for path in sorted(paths):
        if not safe_path(path):
            raise stacker.Undetermined('unsafe changed path')
        if is_test(path,project) and path in present:
            selected.append(path)
        else:
            excluded[path]='deleted' if path not in present else 'not a changed test'
    return selected,excluded


def guard_inventory(path=None):
    """(ordered nodes, (excluded node, reason) or None) from the one inventory file."""
    nodes,exclusions=[],[]
    for raw in Path(path or os.environ.get(GUARD_ENV) or GUARDS).read_text().splitlines():
        line=raw.strip()
        if not line:
            continue
        if line.startswith('#'):
            directive=line[1:].strip()
            if directive.startswith('exclude:'):
                node,sep,why=directive[len('exclude:'):].partition('|')
                if not sep or not node.strip() or not why.strip():
                    raise stacker.Undetermined('invalid guard exclusion directive')
                exclusions.append((node.strip(),why.strip()))
            continue
        nodes.append(line)
    if not nodes or len(nodes)!=len(set(nodes)) or len(exclusions)>1:
        raise stacker.Undetermined('guard inventory missing or duplicated')
    for node in nodes+[e[0] for e in exclusions]:
        target,*parts=node.split('::')
        if not safe_path(target) or not target.endswith('.py') or any(not p or any(ord(c)<33 for c in p) for p in parts):
            raise stacker.Undetermined('invalid guard target')
    if exclusions and exclusions[0][0].split('::')[0] not in {n.split('::')[0] for n in nodes}:
        raise stacker.Undetermined('guard exclusion names no inventory file')
    return nodes,(exclusions[0] if exclusions else None)


def guard_policy(project, path=None):
    """The guard stage for a project: (nodes, exclusion). Unknown policy is undetermined."""
    scope=GUARD_PROJECTS.get(project)
    if scope=='empty':
        return [],None
    if scope=='inventory':
        return guard_inventory(path)
    raise stacker.Undetermined('guard policy unconfigured for project')


def remaining(deadline):
    left=deadline-time.monotonic()
    if left<=0:
        raise TimeoutError('sweep deadline expired')
    return left


def command(argv, *, cwd, env, deadline=None):
    # All commands inherit the worker session. The parent kills the complete
    # process group before removing scratch, also on normal completion.
    timeout=remaining(deadline) if deadline is not None else 180
    try:
        return subprocess.run(cpu_share.niced(argv),cwd=cwd,env=env,stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL,timeout=timeout)
    except subprocess.TimeoutExpired:
        raise TimeoutError('sweep subprocess deadline expired') from None


def python_for(project):
    venv=grading_env.project_venv(project)
    python=str(Path(venv)/'bin/python') if venv else sys.executable
    if not os.access(python,os.X_OK):
        raise stacker.Undetermined('project Python environment unavailable')
    return python


def parse_junit(report):
    """Return [(nodeid, file, failed)] or None when the report cannot prove a run."""
    try:
        cases=list(ET.parse(report).getroot().iter('testcase'))
    except (OSError,ET.ParseError):
        return None
    if not cases or any(c.find('skipped') is not None for c in cases):
        return None
    rows=[]
    for case in cases:
        file=case.get('file') or ''
        module=file[:-3].replace('/','.') if file.endswith('.py') else ''
        classname=case.get('classname','')
        classes=classname[len(module)+1:] if module and classname.startswith(module+'.') else ''
        node='::'.join([file]+([p for p in classes.split('.') if p])+[case.get('name','')])
        bad=case.find('failure') is not None or case.find('error') is not None
        rows.append((node,file,bad,case.find('error') is not None))
    return rows


def collected(rows, nodes):
    def has(target):
        path,*names=target.split('::')
        for node,file,_,_ in rows:
            if file!=path:
                continue
            if not names or node.split('[')[0].endswith('::'.join(names)):
                return True
        return False
    return all(has(t) for t in nodes)


def pytest_stage(repo, nodes, scratch, python, env, deadline=None, deselect=()):
    """Run pytest fail-fast. Returns (passed, failed node ids); collection gaps raise."""
    for node in nodes:
        p=repo/node.split('::')[0]
        if not p.is_file() or p.is_symlink() or not p.resolve().is_relative_to(repo.resolve()):
            raise stacker.Undetermined('test target unavailable: '+node)
    report=scratch/'results.xml'
    report.unlink(missing_ok=True)
    argv=[python,'-m','pytest','-x','-p','no:cacheprovider','-o','addopts=','-o','junit_family=xunit1','--junitxml='+str(report)]
    for node in deselect:
        argv+=['--deselect',node]
    p=command(argv+['--',*nodes],cwd=repo,env=env,deadline=deadline)
    if p.returncode==4:
        raise FileNotFoundError('node not found')
    rows=parse_junit(report)
    if p.returncode not in (0,1) or rows is None:
        raise stacker.Undetermined('pytest collection/environment incomplete')
    failed=[node for node,_,bad,_ in rows if bad]
    if p.returncode==0 and (failed or not collected(rows,nodes)):
        raise stacker.Undetermined('pytest collection/environment incomplete')
    if p.returncode==1 and not failed:
        raise stacker.Undetermined('pytest collection/environment incomplete')
    if any(err for _,_,_,err in rows):
        # Setup/fixture errors are not assertion failures.
        raise stacker.Undetermined('pytest collection/environment incomplete')
    return p.returncode==0,failed


def scratch_env(tree, spec, sha, base_env, scratch, deadline):
    """Fresh private loopback Postgres through the shared repo-scratch-pg profile."""
    import checks
    import provision
    wait=deadline-time.monotonic()-PROVISION_RESERVE_S
    if wait<=0:
        raise TimeoutError('sweep deadline expired')
    # A Postgres unix socket path is limited to about 107 bytes; keep the cluster inside the
    # scratch tree unless that path would be too long (deeply nested TMPDIR), then use /var/tmp.
    parent=scratch if len(str(scratch))+len('/preflight-pg-xxxxxxxx/.s.PGSQL.65535')<=104 else Path('/var/tmp')
    record=checks.CheckRecord('preflight:'+spec,spec,'changed-tests',[],sha,'scratch',False,
                              'repo-scratch-pg','pre-merge','script')
    try:
        with provision.context(tree=tree,env=base_env,pg_parent=str(parent),pg_prefix='preflight-pg-',
                               pg_deadline=time.monotonic()+wait):
            return provision.prepare(record,lane='preflight')
    except TimeoutError:
        raise
    except Exception as exc:
        raise stacker.Undetermined('scratch Postgres provisioning unavailable') from exc


def pytest_group(tree, nodes, ctx, *, marked, deselect=()):
    """One pytest invocation; a marked group gets its own scratch cluster."""
    env,scratch,deadline,python=ctx['env'],ctx['scratch'],ctx['deadline'],ctx['python']
    if not marked:
        return pytest_stage(tree,nodes,scratch,python,env,deadline,deselect)
    provisioned=scratch_env(tree,ctx['spec'],ctx['sha'],env,scratch,deadline)
    try:
        return pytest_stage(tree,nodes,scratch,python,provisioned.env,deadline,deselect)
    finally:
        provisioned.cleanup()


def base_tree(ctx):
    """The base checkout, built once from the same object store as the candidate."""
    if 'base_tree' not in ctx:
        ctx['base_tree']=checkout(ctx['repo'],ctx['scratch']/'base',ctx['base'])
        deps=ctx['candidate']/'dashboard/node_modules'
        if deps.is_dir():
            (ctx['base_tree']/'dashboard').mkdir(exist_ok=True)
            (ctx['base_tree']/'dashboard/node_modules').symlink_to(deps)
    return ctx['base_tree']


def compare_base(node, ctx, runner):
    """Failing-node-only base comparison. True: candidate-introduced red. Raises BaseRed."""
    tree=base_tree(ctx)
    if not (tree/node.split('::')[0]).is_file():
        return True
    try:
        passed=runner(tree)
    except FileNotFoundError:
        return True  # missing base node: candidate-introduced
    if passed:
        return True
    raise BaseRed(node)


def checkout(repo, target, tip):
    target.mkdir(parents=True)
    # A private index and refs; no caller worktree registration or checkout.
    stacker.git(target,'init','-q')
    objects=Path(stacker.out(repo,'rev-parse','--git-path','objects'))
    if not objects.is_absolute():
        objects=repo/objects
    (target/'.git/objects/info/alternates').write_text(str(objects.resolve())+'\n')
    stacker.git(target,'checkout','--detach','-q',tip)
    return target


def dashboard_run(tree, nodes, env, deadline, scratch):
    """Returns (passed, failed files). Only the supplied dependencies are used."""
    cwd=tree/'dashboard'
    if not (cwd/'node_modules/.bin/vitest').is_file():
        raise stacker.Undetermined('dashboard test environment unavailable')
    doc=json.loads((cwd/'package.json').read_text())
    if doc.get('scripts',{}).get('test')!='vitest run':
        raise stacker.Undetermined('dashboard test runner unavailable')
    for node in nodes:
        p=tree/node
        if p.is_symlink() or not p.resolve().is_relative_to(tree.resolve()):
            raise stacker.Undetermined('unsafe dashboard target')
    out=scratch/'vitest.json'
    out.unlink(missing_ok=True)
    p=command(['npm','test','--','--bail=1','--reporter=json','--outputFile='+str(out),
               *[n.removeprefix('dashboard/') for n in nodes]],cwd=cwd,env=env,deadline=deadline)
    if p.returncode not in (0,1):
        raise stacker.Undetermined('dashboard runner incomplete')
    if p.returncode==0:
        return True,[]
    try:
        doc=json.loads(out.read_text())
        failed=sorted('dashboard/'+os.path.relpath(r['name'],cwd) if os.path.isabs(r['name']) else 'dashboard/'+r['name']
                      for r in doc['testResults'] if r.get('status')=='failed')
    except (OSError,ValueError,KeyError,TypeError):
        raise stacker.Undetermined('dashboard runner incomplete') from None
    if not failed:
        raise stacker.Undetermined('dashboard runner incomplete')
    return False,failed


def dashboard(repo, nodes, env, deadline=None, scratch=None):
    """Compatibility wrapper used by fixtures: True when the runner passed."""
    return dashboard_run(repo,nodes,env,deadline,scratch or repo)[0]


def changed_tests(ctx, result, owned):
    candidate,selected=ctx['candidate'],result.selected
    project=result.project
    py=[p for p in selected if p.startswith(('tests/','api/tests/'))]
    plain,marked=[],[]
    for path in py:
        target=candidate/path
        if target.is_symlink() or not target.resolve().is_relative_to(candidate.resolve()):
            raise stacker.Undetermined('unsafe changed test target')
        source=target.read_text(errors='replace')
        import sweep_provision
        (marked if sweep_provision.PG_MARKERS.search(source) or any(t in source for t in PG_TRIGGERS) else plain).append(path)
    def red(kind, node, detail, files):
        result.failed=owner(files,owned)
        result.findings=[finding('changed-tests','changed-test',detail,result.failed)]
        return False
    for group,is_marked in ((plain,False),(marked,True)):
        if not group:
            continue
        result.failed=owner(group,owned)
        good,failed=pytest_group(candidate,group,ctx,marked=is_marked)
        result.failed='-'
        if not good:
            node=failed[0]
            try:
                compare_base(node,ctx,lambda tree: pytest_group(tree,[node],ctx,marked=is_marked)[0])
            except BaseRed:
                result.failed=node
                raise
            return red('pytest',node,'changed pytest assertion failed: '+', '.join(failed),[f.split('::')[0] for f in failed])
    for path in [p for p in selected if p.startswith('src/')]:
        target=candidate/path
        if target.is_symlink() or not target.resolve().is_relative_to(candidate.resolve()):
            raise stacker.Undetermined('unsafe changed test target')
        result.failed=owner([path],owned)
        p=command([ctx['python'],path],cwd=candidate,env=ctx['env'],deadline=ctx['deadline'])
        result.failed='-'
        if p.returncode:
            def script(tree, path=path):
                return command([ctx['python'],path],cwd=tree,env=ctx['env'],deadline=ctx['deadline']).returncode==0
            try:
                compare_base(path,ctx,script)
            except BaseRed:
                result.failed=path
                raise
            return red('script',path,'changed script failed: '+path,[path])
    dash=[p for p in selected if p.startswith('dashboard/')]
    if dash:
        deps=ctx['repo']/'dashboard/node_modules'
        if not deps.is_dir():
            raise stacker.Undetermined('dashboard dependencies unavailable')
        shutil.copytree(deps,candidate/'dashboard/node_modules',symlinks=True)
        result.failed=owner(dash,owned)
        good,failed=dashboard_run(candidate,dash,ctx['env'],ctx['deadline'],ctx['scratch'])
        result.failed='-'
        if not good:
            node=failed[0]
            try:
                compare_base(node,ctx,lambda tree: dashboard_run(tree,[node],ctx['env'],ctx['deadline'],ctx['scratch'])[0])
            except BaseRed:
                result.failed=node
                raise
            return red('dashboard',node,'dashboard assertion failed: '+', '.join(failed),failed)
    return True


def _worker(repo, base, specs, tree, scratch, deadline, channel):
    os.setsid()
    result=SweepResult()
    def stage(name):
        result.stage=name
        result.trace.append(name)
        channel.send(('stage',name))
        remaining(deadline)
    try:
        stage('stack')
        base=stacker.object_id(repo,base)
        result.project=stacker.project_for(repo)
        entries=stacker.resolve_specs(repo,specs)
        result.steps=stacker.stack(repo,base,entries)
        bad=next((s for s in result.steps if s.outcome in ('content-conflict','declined')),None)
        if bad:
            result.failed=bad.spec
            result.findings=[finding('stack',bad.outcome,bad.reason or ', '.join(bad.paths),bad.spec)]
            return
        tip=result.steps[-1].tip
        if tree is not None and stacker.object_id(repo,tree,'tree')!=stacker.object_id(repo,tip,'tree'):
            raise stacker.Undetermined('supplied tree differs from computed stacked tree')
        stage('static')
        result.findings,owned=static(repo,base,entries,result.steps)
        if result.findings:
            result.failed=result.findings[-1]['owner']
            return
        stage('guards')
        nodes,result.exclusion=guard_policy(result.project)
        result.guards=len(nodes)
        python=python_for(result.project)
        candidate=checkout(repo,scratch/'candidate',tip)
        env={key:value for key,value in grading_env._clean_env().items() if key in ('PATH','LANG','LC_ALL')}
        for name in ('home','tmp','root'):
            (scratch/name).mkdir()
        env.update(HOME=str(scratch/'home'),TMPDIR=str(scratch/'tmp'),DOIT_ROOT=str(scratch/'root'),
                   DOIT_TEST_PROCESS='1',PYTHONDONTWRITEBYTECODE='1',
                   PYTHONPATH=PYTHONPATHS.get(result.project,'.'))
        ctx=dict(repo=repo,base=base,candidate=candidate,scratch=scratch,deadline=deadline,env=env,python=python,
                 spec=specs[0],sha=tip)
        if nodes:
            deselect=[result.exclusion[0]] if result.exclusion else []
            good,failed=pytest_stage(candidate,nodes,scratch,python,env,deadline,deselect)
            if not good:
                node=failed[0]
                result.failed=node
                compare_base(node,ctx,lambda t: pytest_stage(t,[node],scratch,python,env,deadline)[0])
                result.failed=failed[0] if len(failed)==1 else '-'
                result.findings=[finding('guards','guard','guard assertion failed: '+', '.join(failed))]
                return
        stage('changed-tests')
        result.selected,result.excluded=targets(repo,base,tip,result.project)
        if not changed_tests(ctx,result,owned):
            return
        remaining(deadline)
        result.failed='-'
        result.ok=True
        result.findings=[finding('changed-tests','inventory','selected '+json.dumps(result.selected)+'; excluded '+json.dumps(result.excluded)
                                 + ('; successful empty stage' if not result.selected else ''))]
    except BaseRed as exc:
        result.ok=False
        result.undetermined=True
        result.reason='base-red'
        result.failed=str(exc)
        cls='guard' if result.stage=='guards' else 'changed-test'
        result.findings=[finding(result.stage,cls,'base-red '+str(exc),'-')]
    except (Exception,KeyboardInterrupt) as exc:
        result.ok=False
        result.undetermined=True
        cls={'guards':'guard','changed-tests':'changed-test'}.get(result.stage,'undetermined')
        why=('deadline expired' if isinstance(exc,TimeoutError) else
             str(exc) if isinstance(exc,stacker.Undetermined) else 'repository or test environment incomplete')
        result.reason=why
        result.findings.append(finding(result.stage,cls,why,result.failed))
    finally:
        channel.send(('result',result))
        channel.close()


def _cpu():
    return sum(r.ru_utime+r.ru_stime for r in (resource.getrusage(resource.RUSAGE_SELF),resource.getrusage(resource.RUSAGE_CHILDREN)))


def record(result, specs, base, batch, project):
    cls=result.findings[-1]['class'] if result.findings and not result.ok else ''
    reason=result.reason or (result.findings[-1]['detail'] if result.undetermined and result.findings else '')
    fields=dict(batch=batch,base=base,ok=result.ok,stage=result.stage,**{'class':cls},failed=result.failed,
                cpu_s=result.cpu_s,wall_s=result.wall_s,undetermined=result.undetermined,reason=reason,project=project)
    for spec in specs:
        stacker.identifier(spec)
        fold.append(['preflight-swept',spec]+[k+':='+json.dumps(v) for k,v in fields.items()],strict=True)


def run(repo, base, specs, *, tree=None, budget_s=180):
    start,cpu_start=time.monotonic(),_cpu()
    result=SweepResult()
    process=None
    scratch=None
    receive=send=None
    try:
        if isinstance(budget_s,bool) or not isinstance(budget_s,(int,float)) or not math.isfinite(budget_s) or not 0<budget_s<=180:
            raise ValueError('invalid budget')
        for spec in specs:
            stacker.identifier(spec)
        if not specs or len(set(specs))!=len(specs):
            raise ValueError('distinct specs required')
        deadline=start+budget_s
        # Reserve a bounded fraction for killing descendants and scratch removal.
        work_deadline=deadline-min(.5,budget_s/10)
        scratch=Path(tempfile.mkdtemp(prefix='preflight-'))
        ctx=multiprocessing.get_context('fork')
        receive,send=ctx.Pipe(duplex=False)
        process=ctx.Process(target=_worker,args=(Path(repo),base,specs,tree,scratch,work_deadline,send))
        process.start()
        send.close()
        while True:
            if not receive.poll(remaining(work_deadline)):
                raise TimeoutError('sweep deadline expired')
            kind,payload=receive.recv()
            if kind=='stage':
                result.stage=payload
                result.trace.append(payload)
            elif kind=='result':
                result=payload
                break
    except (Exception,KeyboardInterrupt):
        result.ok=False
        result.undetermined=True
        cls={'guards':'guard','changed-tests':'changed-test'}.get(result.stage,'undetermined')
        result.reason='sweep interrupted, expired or unavailable'
        result.findings.append(finding(result.stage,cls,result.reason))
    finally:
        if process is not None and process.pid:
            try:
                os.killpg(process.pid,signal.SIGKILL)
            except ProcessLookupError:
                pass
            if process.is_alive():
                process.kill()
            process.join()
            process.close()
        for channel in (receive,send):
            if channel is not None:
                channel.close()
        if scratch is not None:
            try:
                shutil.rmtree(scratch)
            except OSError:
                result.ok=False
                result.undetermined=True
                result.reason='scratch cleanup incomplete'
                result.findings.append(finding(result.stage,'undetermined',result.reason))
    result.cpu_s=max(0,_cpu()-cpu_start)
    result.wall_s=max(0,time.monotonic()-start)
    if isinstance(budget_s,(int,float)) and result.wall_s>budget_s:
        result.ok=False
        result.undetermined=True
        result.reason='deadline expired including cleanup'
        result.findings.append(finding(result.stage,'undetermined',result.reason))
    try:
        record(result,specs,base,uuid.uuid4().hex,result.project if result.project!='-' else Path(repo).name)
    except (Exception,SystemExit):
        result.ok=False
        result.undetermined=True
        result.reason='ledger append rejected or unavailable'
        result.findings.append(finding(result.stage,'undetermined',result.reason))
    return result


def _terminated(signum, frame):
    raise KeyboardInterrupt


def main(argv=None):
    try:
        if threading.current_thread() is threading.main_thread():
            signal.signal(signal.SIGTERM,_terminated)
        a=stacker.arguments(sys.argv[1:] if argv is None else argv,preflight=True)
        result=run(a.repo,a.on,a.specs,tree=a.tree)
        for step in result.steps:
            if step.outcome=='rechained':
                print('PREFLIGHT-RECHAINED',step.spec,stacker.diagnostic(step.metadata['old_parent']),stacker.diagnostic(step.metadata['new_parent']))
        if result.stage in ('guards','changed-tests') and result.project!='-':
            line=['PREFLIGHT-GUARDS','project='+stacker.diagnostic(result.project),'guards='+str(result.guards)]
            if result.exclusion:
                line+=['excluded='+stacker.diagnostic(result.exclusion[0]),'reason='+stacker.diagnostic(result.exclusion[1])]
            print(*line)
        if result.stage=='changed-tests':
            print('PREFLIGHT-TARGETS',len(result.selected),'selected',len(result.excluded),'excluded',
                  stacker.diagnostic(json.dumps(dict(selected=result.selected,excluded=result.excluded),sort_keys=True)))
        if result.undetermined:
            detail=result.findings[-1]['detail'] if result.findings else 'incomplete'
            print('PREFLIGHT-UNDETERMINED',result.stage,stacker.diagnostic(detail))
            return 2
        if not result.ok:
            f=result.findings[-1]
            print('PREFLIGHT-RED',result.stage,f.get('owner','-'),f['class'],stacker.diagnostic(f['detail']))
            return 1
        print('PREFLIGHT-CLEAN',result.stage,'cpu_s='+str(round(result.cpu_s,3)),'wall_s='+str(round(result.wall_s,3)))
        return 0
    except (stacker.Undetermined,OSError,ValueError):
        print('PREFLIGHT-UNDETERMINED stack invalid project or inputs')
        return 2


if __name__=='__main__':
    sys.exit(main())
