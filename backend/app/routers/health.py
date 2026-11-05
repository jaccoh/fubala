from datetime import timedelta

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse

from .. import __version__
from ..models import utcnow
from ..schemas import HealthOut, VersionOut

router = APIRouter(tags=["system"])

# /health and /health/live are probe endpoints (k8s probes, self-update, the
# menu bar): they must answer from memory only — a DB round-trip would hang
# exactly when the DB is locked, which is the situation the probes exist to
# expose. Two endpoints because startup, liveness AND readiness all probe this
# app; a single endpoint that 503s on a merely stale heartbeat would drop the
# pod off the ingress (and eventually restart it mid-flush) whenever a tick
# legitimately runs long — a merge-train flush doing git fetch/merge/push for
# several members takes tens of seconds:
#
# - /health answers "is the process serving?" — ALWAYS 200 while HTTP answers.
#   The scheduler heartbeat rides in the body for the board banner, on a SHORT
#   stall bound: max(SCHEDULER_BANNER_STALL_GRACE_SECONDS, 3x the observed
#   poll interval). A banner-visible stall is a warning, not an outage.
# - /health/live answers "is the scheduler really alive?" — 503 only when the
#   last COMPLETED tick is older than max(SCHEDULER_LIVE_STALL_GRACE_SECONDS,
#   10x the poll interval). That bound can never be tripped by a long tick;
#   only a dead loop (the 26 Oct incident: dead for three hours) crosses it.
SCHEDULER_BANNER_STALL_GRACE_SECONDS = 120.0
SCHEDULER_LIVE_STALL_GRACE_SECONDS = 600.0


def _scheduler_liveness(request: Request):
    """The runner's in-memory heartbeat stamps, or (None, 0) when no runner
    exists (no lifespan ran) — both probes stay ok in that case."""
    runner = getattr(request.app.state, "story_runner", None)
    if runner is None:
        return None, 0.0
    return runner.tick_liveness()


def _stalled(request: Request, *, multiplier: float, floor: float) -> tuple[str | None, bool]:
    """(last completed tick as ISO UTC or None, stalled?) under the given
    bound — stalled means older than max(floor, multiplier x poll interval)."""
    last_tick, interval = _scheduler_liveness(request)
    stalled = last_tick is not None and (utcnow() - last_tick) > timedelta(
        seconds=max(floor, multiplier * interval)
    )
    return (last_tick.isoformat() if last_tick is not None else None), stalled


@router.get("/health", response_model=HealthOut)
def health(request: Request) -> HealthOut:
    """Process-serving probe: 200 as long as the process answers, whatever
    the scheduler is doing (self-update and the menu bar read non-200 as
    "down"; k8s readiness keeps the board on the ingress during a long tick).
    The banner's stall signal is in the body, never in the status code."""
    last_tick, stalled = _stalled(
        request,
        multiplier=3.0,
        floor=SCHEDULER_BANNER_STALL_GRACE_SECONDS,
    )
    return HealthOut(
        status="ok",
        version=__version__,
        scheduler_last_tick=last_tick,
        scheduler_stalled=stalled,
    )


@router.get("/health/live", response_model=HealthOut)
def health_live(request: Request) -> HealthOut | Response:
    """Scheduler-liveness probe (the k8s livenessProbe target): 503 only when
    the last COMPLETED tick is older than max(600 s, 10x the poll interval) —
    a really dead loop, never a legitimately long tick."""
    last_tick, stalled = _stalled(
        request,
        multiplier=10.0,
        floor=SCHEDULER_LIVE_STALL_GRACE_SECONDS,
    )
    body = HealthOut(
        status="unhealthy" if stalled else "ok",
        version=__version__,
        scheduler_last_tick=last_tick,
        scheduler_stalled=stalled,
    )
    if stalled:
        # 503, but with the full HealthOut body: the dead scheduler is the
        # payload (it says since WHEN), not an opaque error
        return JSONResponse(status_code=503, content=body.model_dump())
    return body


@router.get("/version", response_model=VersionOut)
def version() -> VersionOut:
    return VersionOut(version=__version__)
