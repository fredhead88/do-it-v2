"""Strict, locale-compatible JSONL iteration with splitlines numbering."""
import io
import json
import pathlib
import re

_BREAK = re.compile(r"[\n\v\f\x1c-\x1e\x85\u2028\u2029]")


def iter_jsonl(path):
    # Universal newline translation handles CR/CRLF, including chunk boundaries.
    # Other splitlines separators are recognized without retaining physical lines.
    with pathlib.Path(path).open() as stream:
        pending = io.StringIO()
        lineno = 0
        while True:
            chunk = stream.read(65536)
            if not chunk:
                break
            start = 0
            for match in _BREAK.finditer(chunk):
                pending.write(chunk[start:match.start()])
                line = pending.getvalue()
                pending.seek(0)
                pending.truncate(0)
                lineno += 1
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    pass
                else:
                    if isinstance(entry, dict):
                        yield lineno, entry
                start = match.end()
            pending.write(chunk[start:])
        line = pending.getvalue()
        if line:
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                pass
            else:
                if isinstance(entry, dict):
                    yield lineno + 1, entry
