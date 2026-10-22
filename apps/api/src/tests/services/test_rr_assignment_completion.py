"""Rescue Ready fork: an assignment activity is complete only when passed.

Drives the real student/instructor write paths (save task answer, submit,
retry, grade, grant attempt) and checks the TrailStep, the durable audit rows
and the course-completion side effects that hang off it. RBAC, analytics,
webhooks and the audit writer are patched at the assignments module so the
calls can be inspected without a live Postgres/Tinybird.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlmodel import select

from src.db.courses.assignments import (
    Assignment,
    AssignmentTask,
    AssignmentTaskSubmissionUpdate,
    AssignmentTaskTypeEnum,
    AssignmentTaskUpdate,
    AssignmentUserSubmission,
    AssignmentUserSubmissionStatus,
    GradingTypeEnum,
)
from src.db.trail_steps import TrailStep
from src.db.user_audit_events import UserAuditEventType
from src.security.rbac import AccessAction
from src.services.courses.activities.assignments import (
    _rr_cap_answer,
    create_assignment_submission,
    grade_assignment_submission,
    grant_assignment_attempt,
    handle_assignment_task_submission,
    mark_activity_as_done_for_user,
    retry_assignment_submission,
    update_assignment_task,
)

_A = "src.services.courses.activities.assignments."


@pytest.fixture
def env(admin_user):
    """Patch the outside world; expose the mocks the tests inspect.

    ``admin_user`` is the instructor: role checks say "update" only for them.
    ``check_resource_access`` denies UPDATE to anyone else, which is the gate
    grading and granting share.
    """

    async def roles(request, user_id, action, *args, **kwargs):
        return action == "read" or user_id == admin_user.id

    async def resource_access(request, db_session, current_user, course_uuid, action):
        if action == AccessAction.UPDATE and current_user.id != admin_user.id:
            raise HTTPException(status_code=403, detail="forbidden")

    mocks = {}
    with patch(_A + "authorization_verify_based_on_roles", side_effect=roles), \
         patch(_A + "check_resource_access", side_effect=resource_access) as rbac, \
         patch(_A + "verify_activity_reader_access", new_callable=AsyncMock), \
         patch(_A + "check_course_completion_and_create_certificate", new_callable=AsyncMock) as cert, \
         patch(_A + "revoke_user_certificate", new_callable=AsyncMock), \
         patch(_A + "track", new_callable=AsyncMock) as track, \
         patch(_A + "dispatch_webhooks", new_callable=AsyncMock) as dispatch, \
         patch(_A + "record_audit_event", new_callable=AsyncMock) as audit:
        mocks.update(rbac=rbac, cert=cert, track=track, dispatch=dispatch, audit=audit)
        yield mocks


async def _assignment(db, org, course, chapter, activity, *, auto_grading=True,
                      allow_retries=True, max_retries=0, ungraded=False):
    a = Assignment(
        title="Quiz", description="d", due_date="2999-01-01", published=True,
        grading_type=GradingTypeEnum.PERCENTAGE, auto_grading=auto_grading,
        ungraded=ungraded, allow_retries=allow_retries, max_retries=max_retries,
        org_id=org.id, course_id=course.id, chapter_id=chapter.id,
        activity_id=activity.id, assignment_uuid="assignment_rr_completion",
        creation_date=str(datetime.now()), update_date=str(datetime.now()),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    t = AssignmentTask(
        title="2 + 2", description="d", hint="", reference_file=None,
        assignment_type=AssignmentTaskTypeEnum.SHORT_ANSWER,
        contents={"correct_answers": ["4"], "match_mode": "exact"},
        max_grade_value=100, assignment_id=a.id, org_id=org.id,
        course_id=course.id, chapter_id=chapter.id, activity_id=activity.id,
        assignment_task_uuid="assignmenttask_rr_completion",
        creation_date=str(datetime.now()), update_date=str(datetime.now()),
    )
    db.add(t)
    await db.commit()
    return a


async def _attempt(req, db, a, student, answer):
    """Student saves an answer and hands the attempt in."""
    await handle_assignment_task_submission(
        req, "assignmenttask_rr_completion",
        AssignmentTaskSubmissionUpdate(task_submission={"answer": answer}),
        student, db,
    )
    return await create_assignment_submission(req, a.assignment_uuid, student, db)


async def _step(db, activity, student):
    activity_id, student_id = activity.id, student.id
    step = (await db.execute(select(TrailStep).where(
        TrailStep.activity_id == activity_id, TrailStep.user_id == student_id,
    ))).scalars().first()
    if step is not None:
        await db.refresh(step)  # the service may have changed it in this session
    return step


def _audits(env, event_type):
    return [c.kwargs for c in env["audit"].await_args_list
            if c.kwargs.get("event_type") == event_type]


def _events(mock):
    return [c.kwargs.get("event_name") for c in mock.await_args_list]


class TestAutoGraded:
    async def test_failed_submission_is_not_complete(
        self, env, mock_request, db, org, course, chapter, activity, regular_user
    ):
        a = await _assignment(db, org, course, chapter, activity)
        result = await _attempt(mock_request, db, a, regular_user, "5")

        assert result.submission_status == AssignmentUserSubmissionStatus.GRADED
        step = await _step(db, activity, regular_user)
        assert step is not None and step.complete is False
        assert _audits(env, UserAuditEventType.ACTIVITY_COMPLETED) == []
        assert "course_completed" not in _events(env["dispatch"])

    async def test_passed_submission_completes_and_audits(
        self, env, mock_request, db, org, course, chapter, activity, regular_user
    ):
        a = await _assignment(db, org, course, chapter, activity)
        await _attempt(mock_request, db, a, regular_user, "4")

        assert (await _step(db, activity, regular_user)).complete is True
        completed = _audits(env, UserAuditEventType.ACTIVITY_COMPLETED)
        assert len(completed) == 1
        assert completed[0]["target_uuid"] == activity.activity_uuid
        assert completed[0]["user_id"] == regular_user.id
        assert completed[0]["metadata"]["attempt_number"] == 1
        # The assignment is the course's only activity, so the pass finishes it.
        assert "course_completed" in _events(env["dispatch"])
        assert "course_completed" in _events(env["track"])
        assert len(_audits(env, UserAuditEventType.COURSE_COMPLETED)) == 1

    async def test_retry_after_pass_resets_then_new_pass_completes(
        self, env, mock_request, db, org, course, chapter, activity, regular_user
    ):
        a = await _assignment(db, org, course, chapter, activity)
        await _attempt(mock_request, db, a, regular_user, "4")
        assert (await _step(db, activity, regular_user)).complete is True

        await retry_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert (await _step(db, activity, regular_user)).complete is False

        await _attempt(mock_request, db, a, regular_user, "5")  # attempt 2 fails
        assert (await _step(db, activity, regular_user)).complete is False

        await retry_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        await _attempt(mock_request, db, a, regular_user, "4")  # attempt 3 passes
        assert (await _step(db, activity, regular_user)).complete is True
        attempts = [c["metadata"]["attempt_number"]
                    for c in _audits(env, UserAuditEventType.ACTIVITY_COMPLETED)]
        assert attempts == [1, 3]

    async def test_earlier_answers_survive_retry_in_audit(
        self, env, mock_request, db, org, course, chapter, activity, regular_user
    ):
        a = await _assignment(db, org, course, chapter, activity)
        await _attempt(mock_request, db, a, regular_user, "5")
        await retry_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        await _attempt(mock_request, db, a, regular_user, "4")

        submitted = _audits(env, UserAuditEventType.ASSIGNMENT_SUBMITTED)
        by_attempt = {s["metadata"]["attempt_number"]: s["metadata"]["tasks"] for s in submitted}
        assert by_attempt[1][0]["answer"] == {"answer": "5"}
        assert by_attempt[2][0]["answer"] == {"answer": "4"}
        assert by_attempt[1][0]["task_uuid"] == "assignmenttask_rr_completion"

        graded = {g["metadata"]["attempt_number"]: g["metadata"]["tasks"][0]
                  for g in _audits(env, UserAuditEventType.ASSIGNMENT_GRADED)}
        assert graded[1]["passed"] is False and graded[1]["grade"] == 0
        assert graded[2]["passed"] is True and graded[2]["grade"] == 100


class TestManualGrading:
    async def test_submitted_is_not_complete_until_graded_pass(
        self, env, mock_request, db, org, course, chapter, activity,
        regular_user, admin_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, auto_grading=False)
        await _attempt(mock_request, db, a, regular_user, "4")
        assert (await _step(db, activity, regular_user)).complete is False
        assert _audits(env, UserAuditEventType.ACTIVITY_COMPLETED) == []

        await grade_assignment_submission(
            mock_request, regular_user.id, a.assignment_uuid, admin_user, db
        )
        assert (await _step(db, activity, regular_user)).complete is True
        # Grading the same pass again is not a second completion.
        await grade_assignment_submission(
            mock_request, regular_user.id, a.assignment_uuid, admin_user, db
        )
        assert len(_audits(env, UserAuditEventType.ACTIVITY_COMPLETED)) == 1

    async def test_graded_fail_stays_incomplete(
        self, env, mock_request, db, org, course, chapter, activity,
        regular_user, admin_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, auto_grading=False)
        await _attempt(mock_request, db, a, regular_user, "5")
        await grade_assignment_submission(
            mock_request, regular_user.id, a.assignment_uuid, admin_user, db
        )
        assert (await _step(db, activity, regular_user)).complete is False


class TestRegradeAndOverride:
    async def test_regrade_below_threshold_uncompletes(
        self, env, mock_request, db, org, course, chapter, activity,
        regular_user, admin_user,
    ):
        a = await _assignment(db, org, course, chapter, activity)
        await _attempt(mock_request, db, a, regular_user, "4")
        assert (await _step(db, activity, regular_user)).complete is True

        # The instructor moves the answer key, so the stored answer now fails.
        await update_assignment_task(
            mock_request, "assignmenttask_rr_completion",
            AssignmentTaskUpdate(contents={"correct_answers": ["5"], "match_mode": "exact"}),
            admin_user, db,
        )
        assert (await _step(db, activity, regular_user)).complete is False

    async def test_instructor_mark_done_is_refused(
        self, env, mock_request, db, org, course, chapter, activity,
        regular_user, admin_user,
    ):
        # Owner decision: no mark-done override on assignments; a failed
        # attempt stays incomplete until a passing grade.
        a = await _assignment(db, org, course, chapter, activity, auto_grading=False)
        await _attempt(mock_request, db, a, regular_user, "5")
        with pytest.raises(HTTPException) as exc:
            await mark_activity_as_done_for_user(
                mock_request, regular_user.id, a.assignment_uuid, admin_user, db
            )
        assert exc.value.status_code == 400
        assert (await _step(db, activity, regular_user)).complete is False
        assert _audits(env, UserAuditEventType.ACTIVITY_COMPLETED) == []


class TestFormative:
    async def test_ungraded_hand_in_completes(
        self, env, mock_request, db, org, course, chapter, activity, regular_user
    ):
        a = await _assignment(db, org, course, chapter, activity,
                              auto_grading=False, ungraded=True)
        await _attempt(mock_request, db, a, regular_user, "anything")
        assert (await _step(db, activity, regular_user)).complete is True
        assert len(_audits(env, UserAuditEventType.ACTIVITY_COMPLETED)) == 1


class TestGrantAttempt:
    async def _exhausted(self, mock_request, db, org, course, chapter, activity, student):
        """Student fails the only allowed attempt (max_retries=1)."""
        a = await _assignment(db, org, course, chapter, activity, max_retries=1)
        await _attempt(mock_request, db, a, student, "5")
        with pytest.raises(HTTPException) as exc:
            await retry_assignment_submission(mock_request, a.assignment_uuid, student, db)
        assert exc.value.status_code == 403  # stuck without a grant
        return a

    async def test_non_instructor_cannot_grant(
        self, env, mock_request, db, org, course, chapter, activity, regular_user
    ):
        a = await self._exhausted(mock_request, db, org, course, chapter, activity, regular_user)
        with pytest.raises(HTTPException) as exc:
            await grant_assignment_attempt(
                mock_request, a.assignment_uuid, regular_user.id, regular_user, db
            )
        assert exc.value.status_code == 403
        assert _audits(env, UserAuditEventType.ASSIGNMENT_ATTEMPT_GRANTED) == []

    async def test_instructor_grant_reopens_and_audits(
        self, env, mock_request, db, org, course, chapter, activity,
        regular_user, admin_user,
    ):
        a = await self._exhausted(mock_request, db, org, course, chapter, activity, regular_user)
        result = await grant_assignment_attempt(
            mock_request, a.assignment_uuid, regular_user.id, admin_user, db
        )
        assert result["attempt_number"] == 2

        granted = _audits(env, UserAuditEventType.ASSIGNMENT_ATTEMPT_GRANTED)
        assert len(granted) == 1
        assert granted[0]["user_id"] == regular_user.id
        assert granted[0]["metadata"]["granted_by_user_id"] == admin_user.id

        # The student can now pass, and that completes the activity.
        await _attempt(mock_request, db, a, regular_user, "4")
        assert (await _step(db, activity, regular_user)).complete is True

    async def test_grant_refused_while_student_can_still_retry(
        self, env, mock_request, db, org, course, chapter, activity,
        regular_user, admin_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, max_retries=3)
        await _attempt(mock_request, db, a, regular_user, "5")
        with pytest.raises(HTTPException) as exc:
            await grant_assignment_attempt(
                mock_request, a.assignment_uuid, regular_user.id, admin_user, db
            )
        assert exc.value.status_code == 400
        sub = (await db.execute(select(AssignmentUserSubmission))).scalars().first()
        assert sub.attempt_number == 1

    async def test_grant_refused_after_pass(
        self, env, mock_request, db, org, course, chapter, activity,
        regular_user, admin_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, allow_retries=False)
        await _attempt(mock_request, db, a, regular_user, "4")
        with pytest.raises(HTTPException) as exc:
            await grant_assignment_attempt(
                mock_request, a.assignment_uuid, regular_user.id, admin_user, db
            )
        assert exc.value.status_code == 400
        assert (await _step(db, activity, regular_user)).complete is True


def test_oversized_answer_is_replaced_by_marker():
    assert _rr_cap_answer({"answer": "4"}) == {"answer": "4"}
    capped = _rr_cap_answer({"answer": "x" * 20000})
    assert capped["_truncated"] is True
