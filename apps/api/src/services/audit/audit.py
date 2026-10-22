"""Durable audit-event write path.

Unlike ``services/analytics/analytics.track``, which fires-and-forgets to Tinybird
and swallows every error, this records a legal-grade row in Postgres and commits it.
It writes in its **own** isolated session (from the app session factory) so the audit
row is a self-contained transaction that neither depends on nor interferes with the
caller's unit of work. Callers emit an audit event AFTER the authoritative action has
been committed (mirroring where ``track()`` is already called), so an isolated commit
here can never record an action that later rolls back.

Failures are logged loudly (this is legal data, so a lost write matters) but never
raised: an audit-log hiccup must not break the student's actual action.
"""
import asyncio
import logging
from typing import Optional

from fastapi import Request

from src.db.user_audit_events import UserAuditEvent

logger = logging.getLogger(__name__)


def extract_request_context(request: Optional[Request]) -> tuple[Optional[str], Optional[str]]:
    """Return ``(ip, user_agent)`` from a request, best-effort. Never raises."""
    if request is None:
        return None, None
    ip: Optional[str] = None
    user_agent: Optional[str] = None
    try:
        # Local import avoids a heavier import chain at module load.
        from src.services.security.rate_limiting import get_client_ip

        ip = get_client_ip(request)
    except Exception:
        ip = request.client.host if request.client else None
    try:
        user_agent = request.headers.get("user-agent")
    except Exception:
        user_agent = None
    return ip, user_agent


async def record_audit_event(
    *,
    event_type: str,
    user_id: int,
    org_id: Optional[int] = None,
    ip: Optional[str] = None,
    user_agent: Optional[str] = None,
    target_uuid: Optional[str] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Append one durable audit row in an isolated, immediately-committed transaction.

    Emit this only for STUDENT learning actions (see ``UserAuditEventType``). Do not
    call from authoring/admin paths; that data is intentionally out of scope.
    """
    # Anonymous / system actors (user_id 0) have nothing to audit.
    if not user_id:
        return

    # Rescue Ready fork: one retry (after a short pause, so a connection blip
    # can clear), then an ERROR line describing the lost event. The audit row is
    # the school's official record, and a failure used to drop it with only the
    # event type logged. The line identifies the event (type, ids, attempt,
    # grade) but carries no answer text, IP or user agent: logs sit outside the
    # database's access controls. Joining the caller's transaction instead was
    # rejected: every caller commits its action before emitting the event (often
    # across several commits), so atomicity would mean reordering each upstream
    # call site. A retry after a commit whose acknowledgement was lost can write
    # the row twice; for an append-only log a duplicate beats a gap.
    for attempt in (1, 2):
        try:
            # Imported lazily so this module is importable without booting the engine
            # (e.g. during unit tests that patch the factory).
            from src.core.events.database import _async_session_factory

            async with _async_session_factory() as session:
                session.add(
                    UserAuditEvent(
                        event_type=event_type,
                        user_id=user_id,
                        org_id=org_id,
                        ip=ip,
                        user_agent=user_agent,
                        target_uuid=target_uuid,
                        audit_metadata=metadata or {},
                    )
                )
                await session.commit()
            return
        except Exception:
            if attempt == 1:
                logger.warning(
                    "Audit event %s for user %s failed to write; retrying once",
                    event_type, user_id, exc_info=True,
                )
                await asyncio.sleep(_RETRY_DELAY_SECONDS)
                continue
            logger.error(
                "Failed to record audit event %s for user %s (org %s); audit data lost: %s",
                event_type,
                user_id,
                org_id,
                _event_for_log(event_type, user_id, org_id, target_uuid, metadata),
                exc_info=True,
            )


_RETRY_DELAY_SECONDS = 0.2

# Scalar metadata copied into the lost-event log line: enough to know which
# attempt, grade or completion was lost. Everything else (answers, per-task
# rows, free text) is listed by key name and size only.
_LOGGED_METADATA_FIELDS = (
    "attempt_number", "grade", "max_grade", "percentage", "display_grade",
    "letter_grade", "passed", "auto_graded", "grade_source", "completed_by",
    "method", "enrolled_by", "course_uuid", "assignment_uuid", "activity_type",
    "removed_by", "api_token_id", "steps_deleted", "completed_steps_deleted",
    "was_complete", "previous_status",
)
_LOG_LINE_CAP = 4000  # far below journald's 48 KiB line limit


def _event_for_log(event_type, user_id, org_id, target_uuid, metadata) -> str:
    """Describe a lost audit event without student content (see caller)."""
    import json
    from datetime import datetime, timezone

    metadata = metadata if isinstance(metadata, dict) else {}
    fields = {k: metadata[k] for k in _LOGGED_METADATA_FIELDS if k in metadata}
    sizes: dict = {}
    for key, value in metadata.items():
        if key in fields:
            continue
        try:
            sizes[str(key)] = len(json.dumps(value, default=str))
        except Exception:
            sizes[str(key)] = None
    try:
        text = json.dumps({
            "event_type": event_type, "user_id": user_id, "org_id": org_id,
            "target_uuid": target_uuid, "fields": fields,
            "other_metadata_sizes": sizes,
            "failed_at": datetime.now(timezone.utc).isoformat(),
        }, default=str)
    except Exception:
        text = repr((event_type, user_id, org_id, target_uuid))
    return text[:_LOG_LINE_CAP]
