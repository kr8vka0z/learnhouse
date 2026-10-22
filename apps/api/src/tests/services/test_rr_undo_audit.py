"""Rescue Ready fork: admin-API actions that remove part of a student's record
(unenroll, bulk unenroll, uncomplete, progress reset) leave an audit row saying
what was removed, so the record never loses completions without a trace.
"""

from src.db.user_audit_events import UserAuditEventType
from src.tests.services.test_rr_records_integrity import (  # noqa: F401 (fixtures)
    _add_activity,
    _audit_rows,
    _complete_steps,
    audit_to_test_db,
    quiet_admin,
    token_user,
)


async def _enroll_and_complete(db, mock_request, token_user, org, course, chapter, user_id):
    """Enroll through the admin API and complete two documents the real way."""
    from src.services.admin.admin import complete_activity, enroll_user

    docs = [await _add_activity(db, org, course, chapter, n, assignment=False) for n in (60, 61)]
    uuids = [d.activity_uuid for d in docs]
    await enroll_user(mock_request, token_user, user_id, course.course_uuid, db)
    for u in uuids:
        await complete_activity(mock_request, token_user, user_id, u, db)
    assert await _complete_steps(db, user_id) == {60, 61}
    return uuids


class TestAdminUndoAudit:
    async def test_unenroll_records_what_was_removed(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, org, course, chapter,
        regular_user,
    ):
        from src.services.admin.admin import unenroll_user

        user_id, course_uuid = regular_user.id, course.course_uuid
        await _enroll_and_complete(db, mock_request, token_user, org, course, chapter, user_id)

        await unenroll_user(token_user, user_id, course_uuid, db)

        assert await _complete_steps(db, user_id) == set()
        rows = await _audit_rows(db, user_id, UserAuditEventType.COURSE_UNENROLLED)
        assert len(rows) == 1
        meta = rows[0].audit_metadata
        assert rows[0].target_uuid == course_uuid and rows[0].org_id == org.id
        assert (meta["steps_deleted"], meta["completed_steps_deleted"]) == (2, 2)
        assert (meta["removed_by"], meta["api_token_id"]) == ("admin_api", token_user.id)

    async def test_bulk_unenroll_one_row_per_enrolled_student(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, org, course, chapter,
        regular_user, admin_user,
    ):
        from src.services.admin.admin import bulk_unenroll_users

        user_id, other_id = regular_user.id, admin_user.id
        await _enroll_and_complete(db, mock_request, token_user, org, course, chapter, user_id)

        # Duplicate id and a not-enrolled user: still one row, and none for the other.
        await bulk_unenroll_users(token_user, course.course_uuid, [user_id, user_id, other_id], db)

        rows = await _audit_rows(db, user_id, UserAuditEventType.COURSE_UNENROLLED)
        assert [(r.target_uuid, r.audit_metadata["completed_steps_deleted"]) for r in rows] == [
            (course.course_uuid, 2)
        ]
        assert await _audit_rows(db, other_id, UserAuditEventType.COURSE_UNENROLLED) == []

    async def test_uncomplete_records_only_when_a_completion_was_removed(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, org, course, chapter,
        regular_user,
    ):
        from src.services.admin.admin import uncomplete_activity

        user_id = regular_user.id
        uuids = await _enroll_and_complete(db, mock_request, token_user, org, course, chapter, user_id)

        await uncomplete_activity(token_user, user_id, uuids[0], db)
        # Second call: nothing left to remove, so no second row.
        await uncomplete_activity(token_user, user_id, uuids[0], db)

        assert await _complete_steps(db, user_id) == {61}
        rows = await _audit_rows(db, user_id, UserAuditEventType.ACTIVITY_UNCOMPLETED)
        assert [(r.target_uuid, r.audit_metadata["was_complete"]) for r in rows] == [(uuids[0], True)]
        assert rows[0].audit_metadata["course_uuid"] == course.course_uuid

    async def test_reset_records_counts_and_skips_noop(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, org, course, chapter,
        regular_user,
    ):
        from src.services.admin.admin import reset_user_progress

        user_id, course_uuid = regular_user.id, course.course_uuid
        await _enroll_and_complete(db, mock_request, token_user, org, course, chapter, user_id)

        result = await reset_user_progress(token_user, user_id, course_uuid, db)
        await reset_user_progress(token_user, user_id, course_uuid, db)  # no-op

        rows = await _audit_rows(db, user_id, UserAuditEventType.COURSE_PROGRESS_RESET)
        assert len(rows) == 1
        meta = rows[0].audit_metadata
        assert meta["steps_deleted"] == result["steps_deleted"] == 2
        assert meta["completed_steps_deleted"] == 2

        # The completion report still builds with the reset row in the log.
        from src.services.audit.completion_report import build_completion_report

        report = await build_completion_report(db, user_id=user_id, org_id=org.id, course=course)
        assert [r["completed"] for r in report["rows"]] == [False, False]

    async def test_dossier_lists_undo_rows(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, org, course, chapter,
        regular_user,
    ):
        from src.services.admin.admin import unenroll_user
        from src.services.audit.dossier import build_user_dossier

        user_id = regular_user.id
        await _enroll_and_complete(db, mock_request, token_user, org, course, chapter, user_id)
        await unenroll_user(token_user, user_id, course.course_uuid, db)

        dossier = await build_user_dossier(db, user_id, org.id)
        undo = [e for e in dossier["timeline"] if e["event_type"] == UserAuditEventType.COURSE_UNENROLLED]
        assert len(undo) == 1 and undo[0]["event_label"]

    async def test_uncomplete_row_survives_status_sync_failure(
        self, audit_to_test_db, quiet_admin, db, mock_request, token_user, org, course, chapter,
        regular_user,
    ):
        from unittest.mock import AsyncMock, patch

        import pytest

        from src.services.admin.admin import uncomplete_activity

        user_id = regular_user.id
        uuids = await _enroll_and_complete(db, mock_request, token_user, org, course, chapter, user_id)

        # The step delete is committed before the sync runs, so its audit row
        # must already be written when the sync fails.
        with patch("src.services.admin.admin.sync_trailrun_status",
                   new_callable=AsyncMock, side_effect=RuntimeError("sync down")):
            with pytest.raises(RuntimeError):
                await uncomplete_activity(token_user, user_id, uuids[0], db)

        rows = await _audit_rows(db, user_id, UserAuditEventType.ACTIVITY_UNCOMPLETED)
        assert [r.target_uuid for r in rows] == [uuids[0]]
