"""Pure classification of criterion and owed checks; never executes a check."""
import re
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
        for word, entries in (("ro-dsn", ("ro-prod-sql",)), ("droplet", ("droplet-read",)),
                              ("browser", ("deployed-site", "browser-review-account")),
                              ("elapsed", ("elapsed-time",)), ("deploy", ("deployed-site",))):
            if _names(line, (word,)):
                needs.update(entries)
        if not needs:
            needs.update(own["needs"])
    if not unknown and (_names(line, ("elapsed", "elapsed-time")) or capability == "elapsed"):
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
