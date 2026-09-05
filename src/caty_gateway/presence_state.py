"""Flag-gated, client-safe presence state for gateway streaming jobs."""

import json
import os
import time


PRESENCE_MODE2_ENABLED = os.environ.get("CATY_PRESENCE_MODE2", "") == "1"
STALE_AFTER_MS = 30_000

QUEUED = "queued"
MODEL_WAITING = "model-waiting"
STREAMING = "streaming"
TOOL_RUNNING = "tool-running"
RETRYING = "retrying"
WAITING_USER = "waiting-user"
DONE = "done"
FAILED = "failed"

_OBSERVED_PHASES = frozenset((STREAMING, DONE, FAILED))
_logger = None


def set_logger(logger):
    """Install the gateway logger without importing the gateway (avoids a cycle)."""
    global _logger
    _logger = logger


def _now_ms():
    return int(time.time() * 1000)


def _tool_category(tool):
    if not tool:
        return None
    return "external-action"


def _speakability(phase, fields):
    """The sole classifier for fields that a client may potentially voice."""
    if phase == FAILED or fields.get("error") is not None:
        return "never-aloud"
    if phase in (TOOL_RUNNING, RETRYING):
        return "generic"
    return "exact"


def attach(job):
    if not PRESENCE_MODE2_ENABLED:
        return
    with job.cond:
        accepted_ms = _now_ms()
        job.presence_state = {
            "accepted_ms": accepted_ms,
            "phase": QUEUED,
            "phase_started_ms": accepted_ms,
            "events": [{"phase": QUEUED, "at_ms": accepted_ms, "epistemic": "assumed"}],
            "job_id": None,
        }


def set_job_id(job, job_id):
    if not PRESENCE_MODE2_ENABLED:
        return
    with job.cond:
        state = getattr(job, "presence_state", None)
        if state is not None:
            state["job_id"] = job_id


def transition(job, phase, *, tool=None, attempt=None, error=None):
    if not PRESENCE_MODE2_ENABLED:
        return
    # Coverage log is emitted after releasing job.cond so no I/O happens under
    # the lock. Callers must not hold job.cond when calling transition — with a
    # re-entrant outer hold the deferred emit would still run under the lock.
    coverage_line = None
    with job.cond:
        state = getattr(job, "presence_state", None)
        if state is None:
            return
        if state["phase"] in (DONE, FAILED):
            # Terminal phases are sticky: a late transition from a racing
            # cleanup/pipeline path must not reopen a finished job (and a
            # duplicate finish must not emit a second coverage line).
            return
        at_ms = max(_now_ms(), state["phase_started_ms"])
        event = {"phase": phase, "at_ms": at_ms,
                 "epistemic": "observed" if phase in _OBSERVED_PHASES else "assumed"}
        category = _tool_category(tool)
        if category is not None:
            event["tool"] = tool
            event["tool_category"] = category
        if attempt is not None:
            event["attempt"] = attempt
        if error is not None:
            event["error"] = error
        state["phase"] = phase
        state["phase_started_ms"] = at_ms
        state["events"].append(event)
        if phase in (DONE, FAILED):
            coverage_line = _coverage_line(state)
    if coverage_line is not None and _logger is not None:
        _logger(coverage_line)


def finish(job, error):
    if not PRESENCE_MODE2_ENABLED:
        return
    # Truthiness (not `is not None`) to mirror _do_reply's `if error:` gate —
    # a falsy error (e.g. "") is a success response to the client.
    transition(job, FAILED if error else DONE, error=error or None)


def _epistemic(state, now_ms):
    if now_ms - state["phase_started_ms"] > STALE_AFTER_MS:
        return "stale"
    return state["events"][-1]["epistemic"]


def _payload(state):
    phase = state["phase"]
    fields = {
        "tool_category": state["events"][-1].get("tool_category"),
        "error": state["events"][-1].get("error"),
    }
    return {
        "phase": phase,
        "phase_started_ms": state["phase_started_ms"],
        "accepted_ms": state["accepted_ms"],
        "epistemic": _epistemic(state, _now_ms()),
        "speakability": _speakability(phase, fields),
    }


def thinking_body(job):
    if not PRESENCE_MODE2_ENABLED:
        return b'{"ok":true,"status":"thinking"}'
    with job.cond:
        state = getattr(job, "presence_state", None)
        if state is None:
            return b'{"ok":true,"status":"thinking"}'
        payload = {"ok": True, "status": "thinking"}
        payload.update(_payload(state))
        return json.dumps(payload, separators=(",", ":")).encode()


def _coverage_line(state):
    phases = [event["phase"] for event in state["events"]]
    covered = bool(phases) and phases[0] == QUEUED and phases[-1] in (DONE, FAILED)
    return (
        "presence: "
        f"job={state['job_id'] or '-'} phases={phases} events={len(phases)} covered={covered}"
    )
