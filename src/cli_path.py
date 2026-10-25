"""Resolve the claude launcher without importing the application launch stack."""
import os
import pathlib
import shutil


def _pin_error(pin):
    if pathlib.Path(pin).name != "claude":
        return "not named claude"
    if not pathlib.Path(pin).is_file() or not os.access(pin, os.X_OK):
        return "not executable"
    return None


def claude_bin() -> str | None:
    """A pin is authoritative; otherwise prefer PATH, then the home launcher."""
    try:
        if "DOIT_CLAUDE_BIN" in os.environ:
            pin = os.environ["DOIT_CLAUDE_BIN"]
            return pin if _pin_error(pin) is None else None
        found = shutil.which("claude")
        if found:
            return found
        launcher = pathlib.Path.home() / ".local/bin/claude"
        if launcher.is_file() and os.access(launcher, os.X_OK):
            return str(launcher)
    except Exception:
        # Resolution is optional for environment builders, even with a bad HOME.
        pass
    return None


def claude_bin_or_raise() -> str:
    """The relay uses a visible failure rather than ignoring a rejected pin."""
    found = claude_bin()
    if found is not None:
        return found
    if "DOIT_CLAUDE_BIN" in os.environ:
        pin = os.environ["DOIT_CLAUDE_BIN"]
        try:
            reason = _pin_error(pin) or "not executable"
        except Exception:
            reason = "not executable"
        raise FileNotFoundError(f"DOIT_CLAUDE_BIN={pin!r}: {reason}")
    raise FileNotFoundError("claude not found: DOIT_CLAUDE_BIN unset; PATH lookup; "
                            f"{os.path.expanduser('~/.local/bin/claude')}")
