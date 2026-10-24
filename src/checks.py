"""Pure classification of criterion and owed checks; never executes a check."""
import ast
from dataclasses import asdict, dataclass, fields
from datetime import datetime
import re
import shlex
import urllib.parse

import grading_env
import verification

NEEDS = ("repo", "node_modules", "scratch-pg", "ro-prod-sql", "droplet-read",
         "deployed-site", "browser-review-account", "elapsed-time", "operator")
_BUILD_HEADS = frozenset(("pytest", "python", "python3", "npx", "npm", "node",
                          "vitest", "bash", "sh", "grep", "rg", "awk", "sed", "git",
                          "test", "wc", "ls", "cat", "diff", "head", "tail", "jq",
                          "alembic", "make", "cd"))
_OWED_NEEDS = {"ro-dsn": ("ro-prod-sql",), "droplet": ("droplet-read",),
               "browser": ("deployed-site", "browser-review-account"),
               "elapsed": ("elapsed-time",), "not-live": ("deployed-site",),
               "repo": ("repo",), "other": ("repo",), "operator-action": ("operator",)}
_WRAPPERS = frozenset(("ro-sql", "droplet-read", "portal-e2e", "keepa-fetch", "scrapingbee-fetch"))
_UNSAFE_WRAPPERS = _WRAPPERS - {"ro-sql", "droplet-read"}
_WINDOW = re.compile(r"\bwithin\s+(?:the\s+)?(\d+)\s+(hours|h|days|d)\b", re.I)


@dataclass(frozen=True)
class CheckRecord:
    check_id: str
    spec: str
    criterion: str
    argv: list[str]
    sha: str
    data_source: str
    customer_data: bool
    profile: str
    timing: str
    answered_by: str

    def __post_init__(self):
        for field in fields(self):
            if field.name not in ("argv", "customer_data") and not isinstance(getattr(self, field.name), str):
                raise ValueError(f"{field.name} must be a string")
        if self.data_source not in ("scratch", "production"):
            raise ValueError("invalid data_source")
        if self.answered_by not in ("script", "model"):
            raise ValueError("invalid answered_by")
        if type(self.customer_data) is not bool:
            raise ValueError("customer_data must be a bool")
        if not isinstance(self.argv, list) or not all(isinstance(arg, str) for arg in self.argv):
            raise ValueError("argv must be a list of str")
        if self.timing not in ("pre-merge", "post-deploy"):
            instant = self.timing.removeprefix("due:")
            try:
                if not self.timing.startswith("due:") or not re.match(r"\d{4}-\d\d-\d\dT", instant):
                    raise ValueError("invalid timing")
                if datetime.fromisoformat(instant.replace("Z", "+00:00")).utcoffset() is None:
                    raise ValueError("due timing requires an ISO-8601 instant")
            except ValueError as error:
                raise ValueError("invalid timing") from error

    def to_event(self):
        return asdict(self)

    @classmethod
    def from_event(cls, body):
        if not isinstance(body, dict) or set(body) != {field.name for field in fields(cls)}:
            raise ValueError("CheckRecord requires exactly its ten fields")
        return cls(**body)


def declared(spec_text, spec, sha):
    """Parse explicit declarations only; never infer a check's data source."""
    keys = ("data_source", "customer_data", "profile", "timing", "answered_by")
    tagged = _tagged_commands(spec_text)
    result = []
    for criterion, block in grading_env._criterion_blocks(spec_text).items():
        lines = block.splitlines()
        declarations = [i for i, line in enumerate(lines) if re.match(r"\s*check:", line)]
        if not declarations:
            continue
        try:
            if declarations != [1] or not re.match(r"^(?:  )?check: ", lines[1]):
                raise ValueError("check: must sit directly below the AC line")
            pairs = [token.split("=", 1) for token in shlex.split(lines[1].strip()[7:])]
            if any(len(pair) != 2 for pair in pairs) or [pair[0] for pair in pairs] != list(keys):
                raise ValueError("check: requires each declaration key once, in order")
            body = dict(pairs)
            if body["customer_data"] not in ("true", "false"):
                raise ValueError("customer_data must be true or false")
            body["customer_data"] = body["customer_data"] == "true"
            statements = tagged.get(criterion, [])
            argv = (["/bin/sh", "-c", " && ".join(statements)] if len(statements) > 1
                    else shlex.split(statements[0]) if statements else [])
            result.append(CheckRecord(check_id=f"{spec}:{criterion}", spec=spec, criterion=criterion,
                                      argv=argv, sha=sha, **body))
        except ValueError as error:
            raise ValueError(f"{criterion}: {error}") from error
    return result


def record_for(events, spec, criterion):
    """Latest declaration by position, with malformed latest meaning unknown."""
    envelope = {"ts", "type", "subject", "v", "spawn", "ref", "project", "why", "actor", "_src"}
    latest = next((event for event in reversed(events)
                   if event.get("type") == "check-declared" and event.get("criterion") == criterion
                   and (event.get("spec") == spec or event.get("subject") == spec)), None)
    if latest is None or latest.get("subject") != spec:
        return None
    try:
        record = CheckRecord.from_event({key: value for key, value in latest.items() if key not in envelope})
    except ValueError:
        return None
    return record if record.spec == spec else None


def _names(text, words):
    return any(re.search(r"(?<![\w-])" + re.escape(w) + r"(?![\w-])", text, re.I)
               for w in words)


def _needs_order(needs):
    return [word for word in NEEDS if word in needs]


def _elapsed(text):
    return bool(re.search(r"\belapsed time\b", text, re.I) or _WINDOW.search(text))


def _validity(text, timing):
    if timing == "pre-merge":
        return None
    window = _WINDOW.search(text)
    if not window:
        return 24
    return int(window[1]) * (24 if window[2].lower().startswith("d") else 1)


def _verification_statements(spec_text):
    # Keep the same segment boundaries and first-id attribution as _tool_texts.
    for segment in (verification.block(spec_text) or "").split("&&"):
        tag = re.search(r"\bAC(\d+)\b", segment)
        lines = [line.split("#", 1)[0] for line in segment.splitlines()]
        statement = "\n".join(lines).strip()
        if statement:
            yield f"AC{tag[1]}" if tag else None, statement


def _tagged_commands(spec_text):
    commands = {}
    for criterion, statement in _verification_statements(spec_text):
        if criterion is None:
            continue
        commands.setdefault(criterion, []).append(statement)
    return commands


def _head(command):
    return command.split()[0].rsplit("/", 1)[-1]


def _review_command(block):
    # The existing extractor handles wrapped fields; normalize an inline
    # review_path to the same field form before handing it over.
    fields = grading_env._review_paths(re.sub(r"(?<=\S)[ \t]+(?=review_paths?:)", "\n", block))
    value = re.sub(r"^\s*review_paths?:\s*", "", fields).strip()
    span = re.search(r"`([^`]+)`", value)
    candidate = span[1] if span else re.split(r" / |; ", value, maxsplit=1)[0]
    candidate = re.sub(r"^run\s+", "", candidate.strip()).strip()
    words = candidate.split()
    if len(words) < 2:
        return None
    head = words[0]
    if _head(candidate) in _WRAPPERS:
        return None
    if head in _BUILD_HEADS or head.startswith(("./", "/")):
        return candidate
    return None


def _prod_curl(text, project):
    if not project or not _names(text, ("curl",)):
        return False
    config = grading_env._look_toml(grading_env.DOIT_SRC)
    for row in config.get("prod", []):
        if row.get("project") != project:
            continue
        base = row.get("base_url", "")
        ssh = row.get("ssh_target", "").rsplit("@", 1)[-1]
        hosts = (urllib.parse.urlsplit(base).hostname,
                 urllib.parse.urlsplit("//" + ssh).hostname)
        for host in hosts:
            if (host and host.lower() not in ("127.0.0.1", "localhost", "::1")
                    and re.search(r"(?<![\w.-])" + re.escape(host) + r"(?![\w.-])", text, re.I)):
                return True
    return False


def _proves(criterion, block):
    anchor = block.splitlines()[0]
    value = re.sub(r"^\s*\**AC\d+\s*\[[^]]*\]\s*[:—-]?\s*", "", anchor)
    value = re.split(r"\breview_paths?:", value, maxsplit=1)[0]
    return " ".join(value.strip("* ").split())[:200] or criterion


def production_data(block, commands, sources=()):
    """An observed-data check with a production DSN dependency, not a marker.

    Source strings are supplied by the runner from the requested git tree.
    Comments and prose mentioning production/live_db are not DSN resolution.
    Backend tests may set SUPABASE_DB_URL to their own scratch database.
    """
    anchor = block.splitlines()[0] if block else ""
    tag = re.search(r"AC\d+\s*\[([^]]*)\]", anchor, re.I)
    if not tag or "observed-data" not in re.split(r"[\s,]+", tag[1].lower()):
        return False
    variable = r"(?:SUPABASE_DB_URL(?:_RO|_DIRECT|_MIGRATIONS)?|[A-Z_]*PROD[A-Z_]*(?:DSN|DB_URL))"

    def dependency(value):
        if re.fullmatch(variable, value):
            return True
        if value.startswith(("postgres://", "postgresql://")):
            try:
                host = urllib.parse.urlsplit(value).hostname
            except ValueError:
                return False
            return bool(host and host not in ("localhost", "127.0.0.1", "::1"))
        return False

    for command in commands:
        if re.search(r"\b" + variable + r"\b", command):
            return True
        if any(dependency(value) for value in re.findall(r'''postgres(?:ql)?://[^\s'";]+''', command)):
            return True
    for source in sources:
        try:
            nodes = ast.walk(ast.parse(source))
            for node in nodes:
                value = (node.value if isinstance(node, ast.Constant) else
                         node.id if isinstance(node, ast.Name) else None)
                if isinstance(value, str) and dependency(value):
                    return True
        except (SyntaxError, ValueError):
            continue
    return False


def records(spec_text, project):
    """One SD1 record per existing grading_env criterion anchor, in order."""
    tagged = _tagged_commands(spec_text)
    result = []
    for criterion, block in grading_env._criterion_blocks(spec_text).items():
        statements = tagged.get(criterion, [])
        commands = [statement for statement in statements if _head(statement) not in _WRAPPERS]
        routing_text = "\n".join((block, *statements))
        review = _review_command(block)
        if review:
            commands.append(review)
        commands = list(dict.fromkeys(commands))
        tag = re.search(r"AC\d+\s*\[([^]]*)\]", block.splitlines()[0])
        tags = set(re.split(r"[\s,]+", tag[1].lower())) if tag else set()
        needs = set()
        if commands:
            runner = "build"
            needs.add("repo")
            command_text = "\n".join(commands)
            if _names(command_text, ("vitest", "npm", "npx", "node")):
                needs.add("node_modules")
            if _names(command_text, ("private-pg", "pg_scratch", "psql", "alembic", "live_db")):
                needs.add("scratch-pg")
        elif _names(routing_text, _UNSAFE_WRAPPERS):
            runner = "judgment"
            needs.add("repo")
        elif ("observed-data" in tags or _names(routing_text, ("ro-sql", "droplet-read"))
              or _prod_curl(routing_text, project)):
            runner = "prod"
            if _names(routing_text, ("ro-sql",)):
                needs.add("ro-prod-sql")
            if _names(routing_text, ("droplet-read",)):
                needs.add("droplet-read")
            if _names(routing_text, ("curl",)) or not needs:
                needs.add("deployed-site")
        elif tags & {"browser", "ui"}:
            runner = "prod-browser"
            needs.update(("deployed-site", "browser-review-account"))
        else:
            runner = "judgment"
            needs.add("repo")
        if _elapsed(block):
            needs.add("elapsed-time")
        timing = ("scheduled" if "elapsed-time" in needs else
                  "pre-merge" if runner in ("build", "judgment") else "post-deploy")
        result.append({"criterion": criterion, "proves": _proves(criterion, block),
                       "needs": _needs_order(needs), "timing": timing,
                       "validity_h": _validity(block, timing), "runner": runner,
                       "commands": commands})
    return result


def record_for_owed(row, spec_text, project):
    """Translate legacy owed capabilities without leaking unknown vocabulary."""
    criterion = row.get("criterion")
    own = next((record for record in records(spec_text, project)
                if record["criterion"] == criterion), None)
    capability = row.get("capability")
    line = row.get("line") or ""
    unknown = own is None or bool(capability and capability not in _OWED_NEEDS)
    needs = set()
    if unknown:
        needs.add("repo")
    elif capability:
        needs.update(_OWED_NEEDS[capability])
    else:
        needs.update(own["needs"])
    if not unknown and capability and (_names(line, ("elapsed", "elapsed-time")) or capability == "elapsed"):
        needs.add("elapsed-time")
    commands = own["commands"] if own else []
    if unknown or "operator" in needs:
        runner = "judgment"
    elif "browser-review-account" in needs:
        runner = "prod-browser"
    elif needs & {"ro-prod-sql", "droplet-read", "deployed-site"}:
        runner = "prod"
    elif "repo" in needs and commands:
        runner = "build"
    else:
        runner = "judgment"
    timing = "scheduled" if "elapsed-time" in needs else "post-deploy"
    block = grading_env._criterion_blocks(spec_text).get(criterion, "")
    return {"criterion": criterion, "proves": own["proves"] if own else str(criterion),
            "needs": _needs_order(needs), "timing": timing,
            "validity_h": _validity(block + "\n" + line, timing), "runner": runner,
            "commands": commands}


def coverage(spec_text, project):
    """Mapped share excludes the spec-level Verification block itself."""
    rows = records(spec_text, project)
    judgment = [row["criterion"] for row in rows if row["runner"] == "judgment"]
    mapped = len(rows) - len(judgment)
    untagged = any(criterion is None and _head(command) not in ("cd", "echo", "true")
                   for criterion, command in _verification_statements(spec_text))
    return {"mapped": mapped, "judgment": judgment,
            "share": mapped / len(rows) if rows else 0.0,
            "verification_only": len(judgment) if untagged else 0}
