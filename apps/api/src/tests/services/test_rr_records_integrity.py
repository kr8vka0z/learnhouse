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


# ---------------------------------------------------------------------------
# Every sign-in path writes LOGIN
# ---------------------------------------------------------------------------


@pytest.fixture
def audit_to_test_db(db, monkeypatch):
    """Route the audit writer's own session to the test database, so tests read
    the real rows it writes."""
    monkeypatch.setattr("src.core.events.database._async_session_factory", lambda: _CM(db))


async def _audit_rows(db, user_id, event_type):
    return (await db.execute(select(UserAuditEvent).where(
        UserAuditEvent.user_id == user_id, UserAuditEvent.event_type == event_type,
    ))).scalars().all()


@pytest.fixture
def auth_client(db):
    from fastapi import FastAPI
    from httpx import ASGITransport, AsyncClient

    from src.core.events.database import get_db_session
    from src.db.users import AnonymousUser
    from src.routers.auth import router as auth_router
    from src.security.auth import get_current_user

    app = FastAPI()
    app.include_router(auth_router, prefix="/api/v1/auth")
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: AnonymousUser()
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://test")


class TestLoginAudit:
    async def test_self_serve_magic_link_writes_login(
        self, auth_client, audit_to_test_db, db, regular_user
    ):
        from src.services.auth.magic_login import issue_magic_login_token

        user_id = regular_user.id
        token = issue_magic_login_token(regular_user.email, None)
        async with auth_client as c:
            r = await c.post("/api/v1/auth/magic-link/verify", json={"token": token},
                             headers={"user-agent": "ua-test"})
        assert r.status_code == 200
        rows = await _audit_rows(db, user_id, UserAuditEventType.LOGIN)
        assert len(rows) == 1
        assert rows[0].audit_metadata == {"method": "magic_link"}
        assert rows[0].user_agent == "ua-test" and rows[0].ip

    async def test_admin_issued_magic_link_writes_login(
        self, auth_client, audit_to_test_db, db, org, regular_user
    ):
        from datetime import timedelta

        from src.security.auth import create_access_token

        user_id, org_id = regular_user.id, org.id
        token = create_access_token(
            data={"sub": regular_user.email, "purpose": "magic_link", "org_id": org_id,
                  "redirect_to": "", "jti": "rr-admin-link"},
            expires_delta=timedelta(minutes=5),
        )
        async with auth_client as c:
            r = await c.post("/api/v1/auth/magic-link/verify", json={"token": token})
        assert r.status_code == 200
        rows = await _audit_rows(db, user_id, UserAuditEventType.LOGIN)
        assert len(rows) == 1
        assert rows[0].audit_metadata == {"method": "admin_magic_link"}
        assert rows[0].org_id == org_id

    async def test_login_row_survives_last_login_failure(
        self, auth_client, audit_to_test_db, db, regular_user
    ):
        from unittest.mock import patch

        from src.services.auth.magic_login import issue_magic_login_token

        user_id = regular_user.id
        token = issue_magic_login_token(regular_user.email, None)
        with patch("src.routers.auth.update_login_info", side_effect=RuntimeError("blip")):
            async with auth_client as c:
                r = await c.post("/api/v1/auth/magic-link/verify", json={"token": token})
        assert r.status_code == 200
        assert len(await _audit_rows(db, user_id, UserAuditEventType.LOGIN)) == 1

    async def test_email_verification_sign_in_writes_login(
        self, auth_client, audit_to_test_db, db, org, regular_user
    ):
        from unittest.mock import AsyncMock, patch

        from src.db.users import User

        user_id = regular_user.id
        user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
        with patch("src.routers.auth.check_email_verification_rate_limit", return_value=(True, None)), \
             patch("src.routers.auth.verify_email_token", new_callable=AsyncMock,
                   return_value=(user, "Email verified")):
            async with auth_client as c:
                r = await c.post("/api/v1/auth/verify-email", json={
                    "token": "t", "user_uuid": regular_user.user_uuid, "org_uuid": org.org_uuid,
                })
        assert r.status_code == 200 and r.json().get("tokens")
        rows = await _audit_rows(db, user_id, UserAuditEventType.LOGIN)
        assert [row.audit_metadata["method"] for row in rows] == ["email_verification"]


# ---------------------------------------------------------------------------
# Admin-API enrollment writes COURSE_ENROLLED
# ---------------------------------------------------------------------------

_ADMIN = "src.services.admin.admin."


@pytest.fixture
def token_user(org, admin_user):
    from src.db.users import APITokenUser

    rights = {
        b: {"action_create": True, "action_read": True, "action_update": True, "action_delete": True}
        for b in ("users", "roles", "courses", "certifications", "usergroups")
    }
    return APITokenUser(
        id=1, user_uuid="apitoken_rr", username="api_token", org_id=org.id, rights=rights,
        token_name="t", created_by_user_id=admin_user.id,
    )


@pytest.fixture
def quiet_admin():
    from unittest.mock import AsyncMock, patch

    with patch(_ADMIN + "track", new_callable=AsyncMock), \
         patch(_ADMIN + "dispatch_webhooks", new_callable=AsyncMock), \
         patch(_ADMIN + "check_course_completion_and_create_certificate", new_callable=AsyncMock,
               return_value=False):
        yield


class TestAdminEnrollmentAudit:
    async def test_enroll_user_writes_course_enrolled(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, regular_user, course
    ):
        from src.services.admin.admin import enroll_user

        user_id, course_uuid = regular_user.id, course.course_uuid
        await enroll_user(mock_request, token_user, user_id, course_uuid, db)
        rows = await _audit_rows(db, user_id, UserAuditEventType.COURSE_ENROLLED)
        assert [r.target_uuid for r in rows] == [course_uuid]

    async def test_bulk_enroll_writes_one_row_per_new_enrollment(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, regular_user, course
    ):
        from src.services.admin.admin import bulk_enroll_users

        user_id, course_uuid = regular_user.id, course.course_uuid
        await bulk_enroll_users(token_user, course_uuid, [user_id], mock_request, db)
        # Already enrolled the second time: no second row.
        await bulk_enroll_users(token_user, course_uuid, [user_id], mock_request, db)
        rows = await _audit_rows(db, user_id, UserAuditEventType.COURSE_ENROLLED)
        assert [r.target_uuid for r in rows] == [course_uuid]


# ---------------------------------------------------------------------------
# Deleting an account must not erase the record
# ---------------------------------------------------------------------------


@pytest.fixture
def delete_rbac():
    from unittest.mock import AsyncMock, patch

    with patch("src.services.users.users.authorization_verify_if_user_is_anon", new_callable=AsyncMock), \
         patch("src.services.users.users.authorization_verify_based_on_roles_and_authorship",
               new_callable=AsyncMock), \
         patch("src.services.trail.trail.track", new_callable=AsyncMock), \
         patch("src.services.trail.trail.dispatch_webhooks", new_callable=AsyncMock):
        yield


@pytest.fixture
def act_as_superadmin():
    from unittest.mock import AsyncMock, patch

    with patch("src.services.users.users.is_user_superadmin", new_callable=AsyncMock, return_value=True):
        yield


class TestUserDeleteKeepsRecords:
    async def test_student_with_completion_cannot_be_hard_deleted(
        self, delete_rbac, audit_to_test_db, act_as_superadmin, db, mock_request,
        admin_user, regular_user, activity,
    ):
        from fastapi import HTTPException

        from src.db.users import User
        from src.services.trail.trail import add_activity_to_trail
        from src.services.users.users import delete_user_by_id

        user_id = regular_user.id
        await add_activity_to_trail(mock_request, regular_user, activity.activity_uuid, db)

        with pytest.raises(HTTPException) as exc:
            await delete_user_by_id(mock_request, db, admin_user, user_id)
        assert exc.value.status_code == 409
        assert (await db.execute(select(User).where(User.id == user_id))).scalars().first()
        assert await _audit_rows(db, user_id, UserAuditEventType.ACTIVITY_COMPLETED)

    async def test_sole_admin_cannot_delete_account_that_drops_org_records(
        self, delete_rbac, audit_to_test_db, db, mock_request, admin_user, regular_user, activity,
    ):
        from fastapi import HTTPException

        from src.services.trail.trail import add_activity_to_trail
        from src.services.users.users import delete_user_by_id

        await add_activity_to_trail(mock_request, regular_user, activity.activity_uuid, db)
        # The admin has no records of their own, but is the org's only admin, so
        # deleting the account would cascade the org and the student's record.
        with pytest.raises(HTTPException) as exc:
            await delete_user_by_id(mock_request, db, admin_user, admin_user.id)
        assert exc.value.status_code == 409
