#!/usr/bin/env python3
"""Run dispatch regressions; ordered fixture chunks share this namespace.

Run: python3 src/test_dispatch.py. Chunks keep each file below 1,500 lines
while preserving the suite's existing fixture setup and execution order.
"""
from pathlib import Path

for _chunk in range(1, 4):
    _path = Path(__file__).with_name(f"dispatch_checks_{_chunk}.py")
    exec(compile(_path.read_text(), str(_path), "exec"), globals())
