"""Immutable pre-L-spec-1790 functions from 19fc8842a6247eda5fb5e7eb7f711ed6758233c1.

Extracted verbatim from src/nextstep.py and src/packet.py. Only tests execute
these functions against their fixture dependencies; no Git history is needed.
"""

def _rebuild(c):
    evs = c['evs']
    # Settled fold states include shipped (and all shipped-owed-* states),
    # accepted, closed-shipped, closed-unbuilt, dropped, killed and void.
    if _terminal(evs, c['spec']) or c['dependency']:
        return None
    blocked = _latest(evs, {'build-blocked'})
    outcome = _latest(evs, {'build-done', 'build-blocked'})
    # thinker-answer refs the builder's question, not the build-blocked row.
    # A spawn ties that question to this blocked build; legacy rows without a
    # spawn must at least belong to the current block's time interval.
    questions = [e for e in evs if blocked and e.get('type') == 'question'
                 and e.get('actor') == 'builder' and e.get('_src')
                 and (e.get('spawn') == blocked['spawn'] if blocked.get('spawn')
                      else _time(e['ts']) >= _time(blocked['ts']))]
    question = _builder_question(evs)
    if question and question not in questions:
        questions.append(question)
    decisions = [e for e in evs if e.get('type') == 'decision' and e.get('actor') in ('thinker', 'operator')
                 and not e.get('close') and e.get('regrade') != 'yes' and not e.get('wave_waive')
                 # Explicit rework belongs to the latest completed builder attempt,
                 # even when its ref also answers a historical blocked build.
                 and (e.get('rework') != 'yes' or not outcome or _time(e['ts']) > _time(outcome['ts']))
                 and (blocked and _time(e['ts']) > _time(blocked['ts']) and e.get('ref')
                      and e['ref'] == blocked.get('_src')
                      or any(e.get('ref') == q['_src'] and _time(e['ts']) > _time(q['ts'])
                             for q in questions)
                      or e.get('rework') == 'yes')]
    rewrites = [e for e in evs if blocked and e.get('type') == 'spec-written'
                and _time(e['ts']) > _time(blocked['ts'])]
    trigger = max(decisions + rewrites, key=lambda e: _time(e['ts']), default=None)
    dispatched = [e for e in evs if _builder_dispatch(e)]
    if (trigger and (not blocked or _time(trigger['ts']) > _time(blocked['ts']))
            and not _after(dispatched, trigger, DISPATCH_EVENTS)
            and not _after(evs, trigger, {'build-started'})):
        return _result(trigger)


def p_builder(c):
    hint = hint_block(c)          # refused before anything else is assembled
    spec, v = c.spec_file(), write_verify_script(c)
    repo = c.a.repo or str(ROOT / "repos" / c.project())
    # R7: the packet pin shares verify_base_sha's precedence with the wrapper's
    # fallback; runtime merge-base may move forward after a rebase (L-spec-1044).
    base_sha = verify_base_sha(c)
    ch = c.charter_file()
    extract = (section(ch.read_text(), "Constraints") or "").splitlines() if ch else []
    rej, fix = c.standing()
    adrs = [e["adr"] for e in c.all_of("adr-filed") if e.get("adr")]
    done = done_condition(c, c.charter_id())
    L = [f"1. The spec: `{spec}`. Read it from disk once. You are not given its text here.",
         "2. The charter extract — binding constraints and product decisions, verbatim:",
         *(extract or ["   none"]),
         f"3. `base_sha` = {base_sha}. Read it back from the worktree and confirm it before the first edit.",
         # R7/Target 1, AC1/AC3: the machine line `dispatch.pinned_base` reads back
         # off this same file — unconditional, byte-equal to `base_sha` above.
         f"PINNED_BASE_SHA: {base_sha}",
         *(["PINNED_BASE_SHA_EXPLICIT: true"] if c.a.base_sha or base_refresh.refresh_tip(c) else []),
         *(["The base moved to the main tip because main changed under this spec. "
            "If your branch already has a commit, rebase it onto `base_sha` first, "
            "keeping exactly one commit above it."] if not c.a.base_sha and base_refresh.refresh_tip(c) else []),
         f"4. Verify command: `bash {v}`" if v else
         "4. Verify command: NONE — the spec's Verification block is empty. Declare `spec-ambiguity` and stop.",
         f"   Done-condition: {done}.",
         f"5. Sibling units' `Produces:` — {c.a.produces or 'none'}.",
         "6. Parked findings on your footprint: none on file.",
         "7. Live cross-charter conflict list — id · what it changes · state:",
         *[f"   {x}" for x in conflicts(c)],
         f"8. ADRs binding your footprint: {', '.join(adrs) or 'none'}. "
         f"Conventions file: {c.a.conventions or 'none'}.",
         f"   Writes (the merge grant, = the spec's footprint): {', '.join(c.footprint()) or 'none stated'}."]
    if rej or fix:
        L += ["", "This is a rework on the same worktree and branch. Standing and blocking:",
              *[f"   rejected — {x}" for x in rej], *[f"   must-fix — {x}" for x in fix]]
    # Two independent sections: a standing grader rejection and a hint may both be on
    # one packet, and neither is dropped when the other is present.
    if hint:
        L += ["", *hint]
    for d in c.all_of("decision"):
        L.append(f"   decided, and binding: {d.get('why', '')}")
    # L-spec-0273: three known local-box footguns, informational only — the
    # fixes are L-charter-0028's, not this builder's to make.
    L += ["## STOP",
          "- DSN missing in a worktree can silently skip a live_db criterion instead of failing it (L-charter-0028).",
          "- the stale test baseline cloned per builder fills /tmp across many builds (L-charter-0028).",
          "- a gate-clean merge sometimes leaves master red (L-charter-0028)."]
    L += ["", f"Your cwd is your worktree, on branch `{c.a.subject.lower()}`. "
              f"The repository is `{repo}`. One spec, one commit, on that branch."]
    return L
