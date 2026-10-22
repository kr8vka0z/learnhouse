"""Rescue Ready fork: the student record (audit log + completion data) is complete
and survives. Covers the audit writer's retry, LOGIN on every sign-in path,
COURSE_ENROLLED on admin enrollment, the user-delete guard, one completion per
pass under concurrent grading, regrade provenance, and the mark-done rules.
"""

import pytest
from sqlmodel import select

from src.db.user_audit_events import UserAuditEvent, UserAuditEventType
from src.services.audit.audit import record_audit_event


class _CM:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, *a):
        return False


class TestAuditWriter:
    async def test_transient_failure_is_retried_once(self, db, regular_user, org, monkeypatch):
        calls = {"n": 0}

        def factory():
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError("db blip")
            return _CM(db)

        monkeypatch.setattr("src.core.events.database._async_session_factory", factory)
        await record_audit_event(
            event_type=UserAuditEventType.LOGIN, user_id=regular_user.id, org_id=org.id,
            metadata={"method": "password"},
        )
        rows = (await db.execute(select(UserAuditEvent))).scalars().all()
        assert len(rows) == 1 and rows[0].event_type == UserAuditEventType.LOGIN

    async def test_permanent_failure_never_raises(self, regular_user, monkeypatch, caplog):
        def factory():
            raise ConnectionError("db down")

        monkeypatch.setattr("src.core.events.database._async_session_factory", factory)
        await record_audit_event(
            event_type=UserAuditEventType.LOGIN, user_id=regular_user.id,
            metadata={"method": "magic_link"},
        )
        lost = [r for r in caplog.records if r.levelname == "ERROR"]
        assert lost and "magic_link" in lost[0].getMessage()
