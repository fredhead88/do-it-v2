"""Resolve effective packet bases and render their captured shell baseline."""
import pathlib
import re
import shlex
import subprocess


def _project_main(project):
    """Resolve an explicitly configured project, never infer it from a path."""
    import look
    if (not isinstance(project, str) or project in (".", "..")
            or not re.fullmatch(r"[A-Za-z0-9._-]+", project)):
        raise ValueError("invalid project")
    try:
        main = look.load_toml()["push"]["main"][project]
    except (KeyError, TypeError) as exc:
        raise ValueError("missing configured default branch") from exc
    if (not isinstance(main, str) or not re.fullmatch(r"[A-Za-z0-9._/-]+", main)
            or main.startswith("-")):
        raise ValueError("invalid configured default branch")
    return main


def _branch_tip(repo, branch):
    try:
        ref = "refs/heads/" + branch
        checked = subprocess.run(["git", "check-ref-format", ref],
                                 capture_output=True, text=True, timeout=10)
        if checked.returncode:
            raise ValueError("invalid base branch")
        result = subprocess.run(["git", "-C", str(repo), "rev-parse", "--verify", ref + "^{commit}"],
                                capture_output=True, text=True, timeout=10)
        tip = result.stdout.strip()
        if result.returncode or not re.fullmatch(r"[0-9a-f]{40}", tip):
            raise ValueError("unresolved base commit")
        return tip
    except (OSError, TypeError, subprocess.SubprocessError) as exc:
        raise ValueError("base commit lookup failed") from exc


def current_tip(repo, project):
    """Return the configured local default branch's full commit, or fail closed."""
    return _branch_tip(repo, _project_main(project))


def _effective_branch(repo, project, spec, subject):
    import merge_queue
    main = _project_main(project)
    try:
        explicit = merge_queue._base_blocks({"path": str(spec)}, subject, pathlib.Path(repo), main)
    except (OSError, subprocess.SubprocessError) as exc:
        raise ValueError("spec base lookup failed") from exc
    return explicit or main


def _effective_base(repo, project, spec, subject):
    """Resolve the configured branch, honoring only the existing Base parser."""
    return _branch_tip(repo, _effective_branch(repo, project, spec, subject))


def builder_pin(c, root):
    if not hasattr(c, "_builder_base"):
        project = c.project()
        repo = c.a.repo or str(root / "repos" / project)
        branch = _effective_branch(repo, project, c.spec_file(), c.a.subject)
        c._builder_base = _branch_tip(repo, branch)
        c._builder_base_branch = branch
    return c._builder_base


def pinned_baseline(c, root, spec, base):
    """Intercept only the four supported option-free merge-base tuples."""
    if not re.fullmatch(r"[0-9a-f]{40}", base):
        raise ValueError("invalid pinned base")
    branch = (c._builder_base_branch if c.a.role == "builder" else
              _effective_branch(c.a.repo or str(root / "repos" / c.project()),
                                c.project(), spec, c.a.subject))
    forms = [shlex.quote(pair) for ref in (branch, "origin/" + branch)
             for pair in ("HEAD " + ref, ref + " HEAD")]
    return (f"BASE_PINNED={base}\nBASE={base}\n"
            'git() { if [[ "$#" == 3 && "$1" == merge-base ]]; then\n'
            'case "$2 $3" in ' + "|".join(forms) + ') '
            'printf \'%s\\n\' "$BASE_PINNED"; return 0;; esac\n'
            'fi; command git "$@"; }\n')
