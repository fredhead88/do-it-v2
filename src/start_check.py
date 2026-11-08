"""Read-only early refusals; dispatch remains the authority for every start."""
from dataclasses import dataclass
import os
from pathlib import Path

# Load shared modules here: dispatch initializes its CLI version on import,
# so importing it lazily inside refusal would launch a child during a read.
import dispatch
import fold
import grader_packet
import project_scope
import spawns


@dataclass(frozen=True)
class Refusal:
    reason: str
    why: str
    clears: str


def _without_ended_attempts(events):
    """dispatch.main's inline ended-attempt filter; keep source-parity tested."""
    ended = {e.get("attempt") for e in events if e.get("type") == "infra-failure"
             or e.get("type") == "escalation-blocking" and e.get("kind") in ("failure-unknown", "infra-exhausted")}
    return [e for e in events if not (e.get("type") in spawns.START_TYPES
                                     and e.get("spawn") in ended)]


def refusal(role, subject, events, *, now, root=None, charter=None, main_sha=None):
    """None means no covered refusal, never permission to bypass dispatch.

    root scopes lock/default-worktree lookup only. Existing dispatch plan and
    grader evidence readers retain their own configured content roots.
    """
    if role not in ('builder', 'grader', 'reviewer'):
        return None
    root = Path(root) if root is not None else Path(os.environ.get('DOIT_ROOT', Path.home() / '.do-it'))
    if spawns.lock_held(subject, root):
        return Refusal('dispatch-in-progress',
                       f'{subject}: another dispatch holds the subject lock — refusing role={role}',
                       'The subject dispatch lock is released and can be observed free.')
    worktree = None
    if role == 'builder':
        project = project_scope.subject_project(events, subject)
        try:
            worktree = spawns.default_cwd(role, subject, project, root)
        except (OSError, ValueError):
            # Project eligibility belongs to dispatch; subject conflicts still apply.
            pass
    duplicate = spawns.conflict(role, subject, worktree,
                                spawns.open_spawns(_without_ended_attempts(events), now))
    if duplicate:
        return Refusal('duplicate-dispatch', duplicate + ' before any spend',
                       'The conflicting spawn ends or is no longer open under the shared spawn reader.')
    if role == 'builder':
        wave_charter = dispatch.resolve_charter(events, subject, charter)
        if wave_charter is not None:
            footprint = next((e.get('footprint') for e in reversed(events)
                              if e.get('subject') == subject and e.get('type') == 'spec-written'
                              and e.get('footprint')), None)
            wave = dispatch.spec_wave(wave_charter, footprint) if footprint else None
            blocker = dispatch.wave_blocker(events, wave_charter, subject, wave)
            if blocker:
                return Refusal('wave-order',
                    f'{subject} (wave {wave}, charter {wave_charter}) runs ahead of '
                    f'unsettled earlier-wave sibling {blocker} — refusing role=builder before any spend',
                    'Earlier-wave siblings settle, or a recorded wave waiver/rework exemption applies.')
        if fold.spec_shape_pending([e for e in events if e.get('subject') == subject]):
            return Refusal('spec-shape',
                           f'{subject} fails spec-shape validation — refusing role=builder before any spend',
                           'A subsequent spec-written event clears the shape failure.')
    elif role == 'grader':
        if grader_packet.evidence_state(events, subject, main_sha=main_sha).state == 'pending':
            return Refusal('bundle-pending', 'evidence bundle pending',
                           'Fresh unrestricted evidence records the latest build and current main identity, '
                           'or a runner failure reaches dispatch failure handling.')
    return None
