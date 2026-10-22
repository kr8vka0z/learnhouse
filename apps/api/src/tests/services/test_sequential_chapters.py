"""Rescue Ready fork: pass-to-unlock chapter gating ("Sequential chapters").

With ``Course.extra_metadata["rr_sequential_chapters"]`` on, chapter N+1 stays
locked for a student until every published activity in chapter N is done. The
lock must hold on every path that hands out activity content or accepts work:
the chapter tree, the single-activity read, completion, assignment submission
and AI retrieval. Admins and course editors bypass it.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException

from src.db.courses.activities import Activity, ActivitySubTypeEnum, ActivityTypeEnum
from src.db.courses.assignments import (
    Assignment,
    AssignmentTask,
    AssignmentTaskTypeEnum,
    AssignmentUserSubmission,
    AssignmentUserSubmissionStatus,
    GradingTypeEnum,
)
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.chapters import Chapter
from src.db.courses.course_chapters import CourseChapter
from src.db.resource_authors import (
    ResourceAuthor,
    ResourceAuthorshipEnum,
    ResourceAuthorshipStatusEnum,
)
from src.db.usergroup_resources import UserGroupResource
from src.db.usergroup_user import UserGroupUser
from src.db.usergroups import UserGroup
from src.services.ai.rag.access import can_read_activity
from src.services.courses.activities.access import verify_activity_reader_access
from src.services.courses.activities.activities import get_activity
from src.services.courses.activities.assignments import create_assignment_submission
from src.services.courses.chapters import get_chapter, get_course_chapters
from src.services.courses.sequential import (
    ChapterOutline,
    compute_sequential_locks,
)
from src.services.trail.trail import add_activity_to_trail

pytestmark = pytest.mark.asyncio

NOW = str(datetime.now())


def _outline(*chapters):
    return [
        ChapterOutline(chapter_id=i, chapter_uuid=f"ch{i}", name=f"Chapter {i}", activity_ids=tuple(ids))
        for i, ids in chapters
    ]


class TestComputeLocks:
    def test_earliest_unfinished_chapter_blocks_every_later_chapter(self):
        # Chapter 2's work being done early does not open chapter 3 while
        # chapter 1 is still unfinished.
        locks = compute_sequential_locks(_outline((1, [10, 11]), (2, [20]), (3, [30])), {11, 20})
        assert set(locks) == {2, 3}
        assert locks[3].chapter_uuid == "ch1"

    def test_empty_chapter_does_not_block(self):
        locks = compute_sequential_locks(_outline((1, [10]), (2, []), (3, [30])), {10})
        assert locks == {}


# ---------------------------------------------------------------------------
# Course layout: chapter 1 = page + quiz assignment; chapter 2 = page + quiz.
# ---------------------------------------------------------------------------


async def _add_chapter(db, org, course, chapter_id, order):
    ch = Chapter(
        id=chapter_id, name=f"Chapter {order}", description="d", org_id=org.id,
        course_id=course.id, chapter_uuid=f"chapter_{chapter_id}",
        creation_date=NOW, update_date=NOW,
    )
    db.add(ch)
    db.add(CourseChapter(
        chapter_id=chapter_id, course_id=course.id, org_id=org.id, order=order,
        creation_date=NOW, update_date=NOW,
    ))
    await db.commit()
    return ch


async def _add_activity(db, org, course, chapter, activity_id, order, *, assignment=False, published=True):
    a = Activity(
        id=activity_id, name=f"Activity {activity_id}",
        activity_type=ActivityTypeEnum.TYPE_ASSIGNMENT if assignment else ActivityTypeEnum.TYPE_DYNAMIC,
        activity_sub_type=(
            ActivitySubTypeEnum.SUBTYPE_ASSIGNMENT_ANY if assignment
            else ActivitySubTypeEnum.SUBTYPE_DYNAMIC_PAGE
        ),
        content={"type": "doc", "content": [{"type": "text", "text": "secret"}]},
        published=published, org_id=org.id, course_id=course.id,
        activity_uuid=f"activity_{activity_id}", creation_date=NOW, update_date=NOW,
    )
    db.add(a)
    db.add(ChapterActivity(
        order=order, chapter_id=chapter.id, activity_id=activity_id, course_id=course.id,
        org_id=org.id, creation_date=NOW, update_date=NOW,
    ))
    await db.commit()
    return a


async def _add_assignment(db, org, course, chapter, activity, *, auto_grading, task_type):
    asg = Assignment(
        id=activity.id, title=f"Quiz {activity.id}", description="x", due_date="2099-01-01",
        published=True, grading_type=GradingTypeEnum.PERCENTAGE, auto_grading=auto_grading,
        org_id=org.id, course_id=course.id, chapter_id=chapter.id, activity_id=activity.id,
        assignment_uuid=f"assignment_{activity.id}", creation_date=NOW, update_date=NOW,
    )
    db.add(asg)
    db.add(AssignmentTask(
        title="Q", description="", hint="", reference_file=None, assignment_type=task_type,
        contents={}, max_grade_value=100, assignment_id=asg.id, org_id=org.id,
        course_id=course.id, chapter_id=chapter.id, activity_id=activity.id,
        assignment_task_uuid=f"assignmenttask_{activity.id}", creation_date=NOW, update_date=NOW,
    ))
    await db.commit()
    return asg


async def _submission(db, user, assignment, status):
    db.add(AssignmentUserSubmission(
        user_id=user.id, assignment_id=assignment.id, grade=0, submission_status=status,
        attempt_number=1, assignmentusersubmission_uuid=f"aus_{assignment.id}_{user.id}",
        creation_date=NOW, update_date=NOW,
    ))
    await db.commit()


@pytest.fixture
async def layout(db, org, course, chapter, activity):
    """``chapter``/``activity`` (page) from conftest are chapter 1's first item."""
    course.extra_metadata = {"rr_sequential_chapters": True, "other": "kept"}
    db.add(course)
    await db.commit()
    ch1_quiz = await _add_activity(db, org, course, chapter, 2, 2, assignment=True)
    ch1_assignment = await _add_assignment(
        db, org, course, chapter, ch1_quiz, auto_grading=True, task_type=AssignmentTaskTypeEnum.QUIZ,
    )
    # An unpublished activity in chapter 1 must not count as required.
    await _add_activity(db, org, course, chapter, 3, 3, published=False)
    ch2 = await _add_chapter(db, org, course, 2, 2)
    ch2_page = await _add_activity(db, org, course, ch2, 4, 1)
    ch2_quiz = await _add_activity(db, org, course, ch2, 5, 2, assignment=True)
    ch2_assignment = await _add_assignment(
        db, org, course, ch2, ch2_quiz, auto_grading=True, task_type=AssignmentTaskTypeEnum.QUIZ,
    )
    return {
        "ch1_page": activity, "ch1_quiz": ch1_quiz, "ch1_assignment": ch1_assignment,
        "ch2": ch2, "ch2_page": ch2_page, "ch2_quiz": ch2_quiz, "ch2_assignment": ch2_assignment,
    }


@pytest.fixture(autouse=True)
def quiet_side_effects():
    # Completion fires analytics/webhooks/certificate checks; not under test.
    with patch("src.services.trail.trail.track", new_callable=AsyncMock, create=True), \
         patch("src.services.trail.trail.dispatch_webhooks", new_callable=AsyncMock, create=True), \
         patch("src.services.courses.activities.assignments.dispatch_webhooks", new_callable=AsyncMock):
        yield


async def _tree(mock_request, db, course, user):
    chapters = await get_course_chapters(mock_request, course.id, db, user, False, course=course)
    return {c.id: c for c in chapters}


async def _complete(mock_request, db, user, activity):
    await add_activity_to_trail(mock_request, user, activity.activity_uuid, db)


async def _finish_chapter_one(mock_request, db, user, layout):
    """Page through the real completion path; the auto-graded quiz as the
    state a passed quiz leaves (complete trail step + GRADED submission)."""
    await _complete(mock_request, db, user, layout["ch1_page"])
    await _mark_trail_step(db, user, layout["ch1_quiz"], complete=True)
    await _submission(db, user, layout["ch1_assignment"], AssignmentUserSubmissionStatus.GRADED)


async def _mark_trail_step(db, user, activity, *, complete):
    from sqlmodel import select

    from src.db.trail_runs import TrailRun
    from src.db.trail_steps import TrailStep
    from src.db.trails import Trail

    run = (await db.execute(
        select(TrailRun).where(TrailRun.user_id == user.id, TrailRun.course_id == activity.course_id)
    )).scalars().first()
    if run is None:
        trail = Trail(org_id=activity.org_id, user_id=user.id, trail_uuid=f"trail_{user.id}",
                      creation_date=NOW, update_date=NOW)
        db.add(trail)
        await db.commit()
        run = TrailRun(trail_id=trail.id, course_id=activity.course_id, org_id=activity.org_id,
                       user_id=user.id, creation_date=NOW, update_date=NOW)
        db.add(run)
        await db.commit()
    db.add(TrailStep(
        complete=complete, teacher_verified=False, grade="", trailrun_id=run.id,
        trail_id=run.trail_id, activity_id=activity.id, course_id=activity.course_id,
        org_id=activity.org_id, user_id=user.id, creation_date=NOW, update_date=NOW,
    ))
    await db.commit()


class TestSettingOff:
    async def test_no_sequential_lock_when_setting_off(self, mock_request, db, course, regular_user, layout):
        course.extra_metadata = {"rr_sequential_chapters": False}
        db.add(course)
        await db.commit()
        tree = await _tree(mock_request, db, course, regular_user)
        assert not tree[2].is_locked
        assert all(not a.is_locked and a.lock_reason is None for a in tree[2].activities)
        result = await get_activity(mock_request, layout["ch2_page"].activity_uuid, regular_user, db)
        assert not result.is_locked and result.content


class TestTreeGate:
    async def test_chapter_two_locked_until_chapter_one_done(self, mock_request, db, course, regular_user, layout):
        tree = await _tree(mock_request, db, course, regular_user)
        assert not tree[1].is_locked
        assert tree[2].is_locked
        assert tree[2].lock_reason == "sequential"
        assert tree[2].lock_requires_chapter["chapter_uuid"] == "chapter_test"
        # Straight to the first unfinished activity of chapter 1.
        assert tree[2].lock_requires_chapter["next_activity_uuid"] == "activity_test"
        for a in tree[2].activities:
            assert a.is_locked and a.lock_reason == "sequential" and a.content == {}

        await _finish_chapter_one(mock_request, db, regular_user, layout)
        tree = await _tree(mock_request, db, course, regular_user)
        assert not tree[2].is_locked
        assert all(not a.is_locked for a in tree[2].activities)

    async def test_failed_auto_graded_quiz_keeps_chapter_two_locked(
        self, mock_request, db, course, regular_user, layout
    ):
        # A failed auto-graded quiz: graded, but its trail step is not complete
        # (completion = passed). Being graded must not count as done.
        await _complete(mock_request, db, regular_user, layout["ch1_page"])
        await _mark_trail_step(db, regular_user, layout["ch1_quiz"], complete=False)
        await _submission(db, regular_user, layout["ch1_assignment"], AssignmentUserSubmissionStatus.GRADED)
        tree = await _tree(mock_request, db, course, regular_user)
        assert tree[2].is_locked

    async def test_submitted_manual_graded_assignment_counts(
        self, mock_request, db, course, regular_user, layout
    ):
        layout["ch1_assignment"].auto_grading = False
        db.add(layout["ch1_assignment"])
        await db.commit()
        await _complete(mock_request, db, regular_user, layout["ch1_page"])
        # Submitted, waiting for the instructor, no complete trail step yet.
        await _submission(db, regular_user, layout["ch1_assignment"], AssignmentUserSubmissionStatus.SUBMITTED)
        tree = await _tree(mock_request, db, course, regular_user)
        assert not tree[2].is_locked

    async def test_retry_reset_manual_submission_does_not_count(
        self, mock_request, db, course, regular_user, layout
    ):
        layout["ch1_assignment"].auto_grading = False
        db.add(layout["ch1_assignment"])
        await db.commit()
        await _complete(mock_request, db, regular_user, layout["ch1_page"])
        await _submission(db, regular_user, layout["ch1_assignment"], AssignmentUserSubmissionStatus.PENDING)
        tree = await _tree(mock_request, db, course, regular_user)
        assert tree[2].is_locked

    async def test_admin_bypasses(self, mock_request, db, course, admin_user, layout):
        tree = await _tree(mock_request, db, course, admin_user)
        assert not tree[2].is_locked
        result = await get_activity(mock_request, layout["ch2_page"].activity_uuid, admin_user, db)
        assert not result.is_locked and result.content

    async def test_course_contributor_bypasses(self, mock_request, db, org, course, regular_user, layout):
        db.add(ResourceAuthor(
            resource_uuid=course.course_uuid, user_id=regular_user.id,
            authorship=ResourceAuthorshipEnum.CONTRIBUTOR,
            authorship_status=ResourceAuthorshipStatusEnum.ACTIVE,
            creation_date=NOW, update_date=NOW,
        ))
        await db.commit()
        tree = await _tree(mock_request, db, course, regular_user)
        assert not tree[2].is_locked
        result = await get_activity(mock_request, layout["ch2_page"].activity_uuid, regular_user, db)
        assert not result.is_locked and result.lock_reason is None and result.content


class TestDirectAccessRefused:
    async def test_single_chapter_read_is_locked(self, mock_request, db, regular_user, layout):
        # The one-chapter read sees only that chapter; the lock still needs
        # the whole course's order.
        chapter = await get_chapter(mock_request, layout["ch2"].id, regular_user, db)
        assert chapter.is_locked
        assert all(a.is_locked and a.content == {} for a in chapter.activities)

    async def test_single_activity_read_is_scrubbed(self, mock_request, db, regular_user, layout):
        result = await get_activity(mock_request, layout["ch2_page"].activity_uuid, regular_user, db)
        assert result.is_locked and result.content == {} and result.lock_reason == "sequential"

    async def test_reader_gate_refuses(self, mock_request, db, course, regular_user, layout):
        with pytest.raises(HTTPException) as exc:
            await verify_activity_reader_access(mock_request, layout["ch2_page"], course, regular_user, db)
        assert exc.value.status_code == 403

    async def test_completing_locked_activity_refused(self, mock_request, db, regular_user, layout):
        with pytest.raises(HTTPException) as exc:
            await _complete(mock_request, db, regular_user, layout["ch2_page"])
        assert exc.value.status_code == 403

    async def test_submitting_locked_quiz_refused(self, mock_request, db, regular_user, layout):
        with pytest.raises(HTTPException) as exc:
            await create_assignment_submission(
                mock_request, layout["ch2_assignment"].assignment_uuid, regular_user, db
            )
        assert exc.value.status_code == 403

    async def test_course_usergroup_grant_does_not_skip_gate(
        self, mock_request, db, org, course, regular_user, layout
    ):
        # The single-activity gate returns early for a course-level usergroup
        # member; the sequential lock must still apply.
        db.add(UserGroup(id=7, name="g", description="", org_id=org.id, usergroup_uuid="ug_7",
                         creation_date=NOW, update_date=NOW))
        db.add(UserGroupResource(usergroup_id=7, resource_uuid=course.course_uuid, org_id=org.id,
                                 creation_date=NOW, update_date=NOW))
        db.add(UserGroupUser(usergroup_id=7, user_id=regular_user.id, org_id=org.id,
                             creation_date=NOW, update_date=NOW))
        await db.commit()
        result = await get_activity(mock_request, layout["ch2_page"].activity_uuid, regular_user, db)
        assert result.is_locked and result.content == {}


class TestRagAccess:
    async def test_rag_respects_lock(self, mock_request, db, course, regular_user, layout):
        assert not await can_read_activity(mock_request, regular_user, course, layout["ch2_page"].id, db)
        assert await can_read_activity(mock_request, regular_user, course, layout["ch1_page"].id, db)
        await _finish_chapter_one(mock_request, db, regular_user, layout)
        assert await can_read_activity(mock_request, regular_user, course, layout["ch2_page"].id, db)
