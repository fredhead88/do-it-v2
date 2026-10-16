#!/usr/bin/env python3
"""Print a per-job network-namespace plan; --run executes it on a staging runner."""
import argparse
import importlib.util
import json
import os
from pathlib import Path
import re
import subprocess
import uuid


def plan(checkout, image, command, cpus, uid, gid, network, host_netns):
    if not re.fullmatch(r'[^\s]+@sha256:[0-9a-f]{64}', image):
        raise ValueError('image must be pinned by sha256 digest')
    if not command or cpus < 1:
        raise ValueError('a command and positive CPU budget are required')
    tooling = Path(__file__).resolve().parent
    return [
        ['docker', 'network', 'create', '--driver', 'bridge', '--opt',
         'com.docker.network.bridge.enable_icc=false', network],
        ['docker', 'run', '--rm', '--name', network, '--network', network, '--cpus', str(cpus),
         '--cap-drop', 'ALL', '--security-opt', 'no-new-privileges', '--user', f'{uid}:{gid}',
         '--env', 'HOME=/tmp/ci-home', '--env', 'PYTHONNOUSERSITE=1',
         '--env', f'CI_HOST_NETNS={host_netns}', '--env', f'CI_WORKERS={cpus}',
         '--mount', f'type=bind,src={Path(checkout).resolve()},dst=/work',
         '--mount', f'type=bind,src={tooling},dst=/ci-tools,readonly', '--workdir', '/work',
         image, 'bash', '-c', 'mkdir -p "$HOME"; exec "$@"', 'ci-job', *command],
        ['docker', 'rm', '--force', network],
        ['docker', 'network', 'rm', network],
    ]


def execute(commands, run=subprocess.run):
    run(commands[0], check=True)
    try:
        return run(commands[1], check=False).returncode
    finally:
        run(commands[2], check=False)
        run(commands[3], check=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', action='store_true')
    parser.add_argument('--checkout', type=Path, required=True)
    parser.add_argument('--image', required=True)
    parser.add_argument('command', nargs=argparse.REMAINDER)
    args = parser.parse_args()
    spec = importlib.util.spec_from_file_location('cpu_budget', Path(__file__).with_name('cpu-budget.py'))
    cpu = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cpu)
    command = args.command[1:] if args.command[:1] == ['--'] else args.command
    commands = plan(args.checkout, args.image, command, cpu.workers(), os.getuid(), os.getgid(),
                    'ci-job-' + uuid.uuid4().hex, os.readlink('/proc/self/ns/net'))
    if not args.run:
        print(json.dumps(commands, indent=2))
        return 0
    import signal
    def stop(signum, frame):
        raise SystemExit(128 + signum)
    signal.signal(signal.SIGTERM, stop)
    return execute(commands)


if __name__ == '__main__':
    raise SystemExit(main())
