#!/usr/bin/env python3
"""Versioned entry point; invoke from the canonical repository after rollout."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'src'))
from thinker_inbox import main
main()
