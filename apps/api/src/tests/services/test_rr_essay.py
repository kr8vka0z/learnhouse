"""Rescue Ready fork: essay questions (CUSTOM task with contents.kind == "essay").

Drives the real write paths (student save + hand-in, instructor manual grade)
and checks: an essay is never auto-graded, word limits are enforced at hand-in,
the full essay text reaches the audit snapshot, the model answer is withheld
from students, and a CUSTOM task without the essay marker behaves as upstream.
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
    AssignmentUserSubmission,
    AssignmentUserSubmissionStatus,
    GradingTypeEnum,
)
from src.db.user_audit_events import UserAuditEventType
from src.security.rbac import AccessAction
from src.services.courses.activities.assignments import (
    create_assignment_submission,
    grade_assignment_submission,
    handle_assignment_task_submission,
    read_assignment_task,
)
from src.services.courses.activities.essay import ESSAY_SNAPSHOT_MAX_CHARS

_A = "src.services.courses.activities.assignments."
ESSAY_UUID = "assignmenttask_rr_essay"
SHORT_UUID = "assignmenttask_rr_essay_short"


@pytest.fixture
def env(admin_user):
    async def roles(request, user_id, action, *args, **kwargs):
        return action == "read" or user_id == admin_user.id

    async def resource_access(request, db_session, current_user, course_uuid, action):
        if action == AccessAction.UPDATE and current_user.id != admin_user.id:
            raise HTTPException(status_code=403, detail="forbidden")

    with patch(_A + "authorization_verify_based_on_roles", side_effect=roles), \
         patch(_A + "check_resource_access", side_effect=resource_access), \
         patch(_A + "verify_activity_reader_access", new_callable=AsyncMock), \
         patch(_A + "check_course_completion_and_create_certificate", new_callable=AsyncMock), \
         patch(_A + "revoke_user_certificate", new_callable=AsyncMock), \
         patch(_A + "track", new_callable=AsyncMock), \
         patch(_A + "dispatch_webhooks", new_callable=AsyncMock), \
         patch(_A + "record_audit_event", new_callable=AsyncMock) as audit:
        yield {"audit": audit}


async def _assignment(db, org, course, chapter, activity, contents, *, with_short=False):
    a = Assignment(
        title="Essay", description="d", due_date="2999-01-01", published=True,
        grading_type=GradingTypeEnum.PERCENTAGE, auto_grading=True,
        allow_retries=True, max_retries=0,
        org_id=org.id, course_id=course.id, chapter_id=chapter.id,
        activity_id=activity.id, assignment_uuid="assignment_rr_essay",
        creation_date=str(datetime.now()), update_date=str(datetime.now()),
    )
    db.add(a)
    await db.commit()
    await db.refresh(a)
    common = dict(
        description="d", hint="", reference_file=None, max_grade_value=100,
        assignment_id=a.id, org_id=org.id, course_id=course.id,
        chapter_id=chapter.id, activity_id=activity.id,
        creation_date=str(datetime.now()), update_date=str(datetime.now()),
    )
    db.add(AssignmentTask(
        title="Describe it", assignment_type=AssignmentTaskTypeEnum.CUSTOM,
        contents=contents, assignment_task_uuid=ESSAY_UUID, order=0, **common,
    ))
    if with_short:
        db.add(AssignmentTask(
            title="2 + 2", assignment_type=AssignmentTaskTypeEnum.SHORT_ANSWER,
            contents={"correct_answers": ["4"], "match_mode": "exact"},
            assignment_task_uuid=SHORT_UUID, order=1, **common,
        ))
    await db.commit()
    return a


async def _save(req, db, student, uuid, payload):
    return await handle_assignment_task_submission(
        req, uuid, AssignmentTaskSubmissionUpdate(task_submission=payload), student, db,
    )


def _submitted_audits(env):
    return [c.kwargs for c in env["audit"].await_args_list
            if c.kwargs.get("event_type") == UserAuditEventType.ASSIGNMENT_SUBMITTED]


ESSAY = {"kind": "essay", "prompt": "Explain hand hygiene.", "explanation": "MODEL ANSWER"}


class TestEssayGrading:
    async def test_not_auto_graded_and_graded_only_manually(
        self, env, mock_request, db, org, course, chapter, activity, regular_user, admin_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, ESSAY, with_short=True)
        saved = await _save(mock_request, db, regular_user, ESSAY_UUID, {"answer": "Wash hands often."})
        await _save(mock_request, db, regular_user, SHORT_UUID, {"answer": "4"})

        # auto_grading is on, but an essay in the assignment keeps it in the queue.
        result = await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert result.submission_status == AssignmentUserSubmissionStatus.SUBMITTED

        # A student cannot grade their own essay.
        with pytest.raises(HTTPException):
            await handle_assignment_task_submission(
                mock_request, ESSAY_UUID,
                AssignmentTaskSubmissionUpdate(
                    assignment_task_submission_uuid=saved.assignment_task_submission_uuid,
                    task_submission={"answer": "Wash hands often."}, grade=100,
                ),
                regular_user, db,
            )

        # Instructor grades the essay task, then finalizes.
        await handle_assignment_task_submission(
            mock_request, ESSAY_UUID,
            AssignmentTaskSubmissionUpdate(
                assignment_task_submission_uuid=saved.assignment_task_submission_uuid,
                task_submission={"answer": "Wash hands often."},
                grade=80, task_submission_grade_feedback="Good", manually_graded=True,
            ),
            admin_user, db,
        )
        await grade_assignment_submission(
            mock_request, regular_user.id, a.assignment_uuid, admin_user, db
        )
        row = (await db.execute(select(AssignmentUserSubmission).where(
            AssignmentUserSubmission.assignment_id == a.id,
            AssignmentUserSubmission.user_id == regular_user.id,
        ))).scalars().first()
        await db.refresh(row)
        assert row.submission_status == AssignmentUserSubmissionStatus.GRADED
        assert row.grade == 180  # 80 manual + 100 server-verified short answer (raw points)


class TestWordLimits:
    LIMITED = {**ESSAY, "min_words": 3, "max_words": 5}

    @pytest.mark.parametrize("answer", ["too short", "one two three four five six"])
    async def test_hand_in_outside_limits_is_refused(
        self, answer, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, self.LIMITED)
        await _save(mock_request, db, regular_user, ESSAY_UUID, {"answer": answer})
        with pytest.raises(HTTPException) as exc:
            await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert exc.value.status_code == 400
        # Refused before anything was written: no submission row, no audit row.
        assert (await db.execute(select(AssignmentUserSubmission).where(
            AssignmentUserSubmission.assignment_id == a.id,
        ))).scalars().first() is None
        assert _submitted_audits(env) == []

    async def test_missing_answer_counts_as_zero_words(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, self.LIMITED)
        with pytest.raises(HTTPException) as exc:
            await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert exc.value.status_code == 400

    async def test_hand_in_within_limits_is_accepted(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, self.LIMITED)
        await _save(mock_request, db, regular_user, ESSAY_UUID, {"answer": "  one\ntwo   three four "})
        result = await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert result.submission_status == AssignmentUserSubmissionStatus.SUBMITTED

    async def test_hand_in_over_size_ceiling_is_refused(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        # One huge "word" passes max_words=1 but would be cut from the audit
        # snapshot, so the size ceiling must refuse it.
        a = await _assignment(db, org, course, chapter, activity, {**ESSAY, "max_words": 1})
        await _save(mock_request, db, regular_user, ESSAY_UUID,
                    {"answer": "x" * (ESSAY_SNAPSHOT_MAX_CHARS + 1)})
        with pytest.raises(HTTPException) as exc:
            await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert exc.value.status_code == 400
        assert _submitted_audits(env) == []

    async def test_answer_under_ceiling_survives_snapshot_uncut(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        # Non-ASCII text grows when JSON-escaped; the ceiling must measure the
        # same way the snapshot cap does, or an accepted essay is still cut.
        a = await _assignment(db, org, course, chapter, activity, ESSAY)
        essay = "é" * (ESSAY_SNAPSHOT_MAX_CHARS // 6 - 10)
        await _save(mock_request, db, regular_user, ESSAY_UUID, {"answer": essay})
        await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert _submitted_audits(env)[0]["metadata"]["tasks"][0]["answer"] == {"answer": essay}

    async def test_non_ascii_over_escaped_ceiling_is_refused(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, ESSAY)
        await _save(mock_request, db, regular_user, ESSAY_UUID,
                    {"answer": "é" * (ESSAY_SNAPSHOT_MAX_CHARS // 6 + 10)})
        with pytest.raises(HTTPException) as exc:
            await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert exc.value.status_code == 400

    async def test_draft_save_over_limit_is_allowed(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        await _assignment(db, org, course, chapter, activity, self.LIMITED)
        saved = await _save(mock_request, db, regular_user, ESSAY_UUID,
                            {"answer": "one two three four five six seven"})
        assert saved.task_submission["answer"].startswith("one")


class TestAuditSnapshot:
    async def test_long_essay_text_is_in_snapshot(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, ESSAY)
        essay = "word " * 3000  # ~15k chars, over the default 8k snapshot cap
        await _save(mock_request, db, regular_user, ESSAY_UUID, {"answer": essay})
        await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        tasks = _submitted_audits(env)[0]["metadata"]["tasks"]
        assert tasks[0]["answer"] == {"answer": essay}

    async def test_non_essay_custom_keeps_upstream_cap(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        a = await _assignment(db, org, course, chapter, activity, {"anything": True})
        await _save(mock_request, db, regular_user, ESSAY_UUID, {"answer": "word " * 3000})
        await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        answer = _submitted_audits(env)[0]["metadata"]["tasks"][0]["answer"]
        assert answer.get("_truncated") is True


class TestNonEssayCustom:
    async def test_custom_without_marker_ignores_word_limits_and_stays_queued(
        self, env, mock_request, db, org, course, chapter, activity, regular_user,
    ):
        # min_words on a non-essay CUSTOM task is just caller-owned data.
        a = await _assignment(db, org, course, chapter, activity, {"min_words": 50})
        await _save(mock_request, db, regular_user, ESSAY_UUID, {"answer": {"free": "json"}})
        result = await create_assignment_submission(mock_request, a.assignment_uuid, regular_user, db)
        assert result.submission_status == AssignmentUserSubmissionStatus.SUBMITTED


class TestModelAnswerWithheld:
    async def test_student_read_has_no_model_answer_instructor_read_does(
        self, env, mock_request, db, org, course, chapter, activity, regular_user, admin_user,
    ):
        await _assignment(db, org, course, chapter, activity, ESSAY)
        student_view = await read_assignment_task(mock_request, ESSAY_UUID, regular_user, db)
        assert "explanation" not in student_view.contents
        assert student_view.contents["kind"] == "essay"
        teacher_view = await read_assignment_task(mock_request, ESSAY_UUID, admin_user, db)
        assert teacher_view.contents["explanation"] == "MODEL ANSWER"
