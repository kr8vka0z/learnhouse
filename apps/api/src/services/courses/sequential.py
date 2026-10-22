"""Rescue Ready fork: pass-to-unlock chapter gating ("Sequential chapters").

A per-course setting, stored as ``Course.extra_metadata["rr_sequential_chapters"]``
so no migration is needed. When it is on, chapter N+1 (and every activity in
it) stays locked for a student until every published activity in chapter N is
done. "Done" means ``TrailStep.complete``, except that an assignment needing
instructor grading counts once the student has submitted it, so students are
not held up waiting for a grader.

This is the one place that decides the sequential lock. The chapter tree
(``chapters._apply_locks_to_chapters``), the single-activity gate
(``activities.access.apply_activity_lock``, which every content, file and
submission route goes through) and AI retrieval (``ai.rag.access``) all call
in here, and each composes it with the existing public/authenticated/
restricted lock tiers: locked if either says locked.

Kept out of the upstream modules on purpose so upstream releases merge
cleanly; the hooks into upstream files are a few marked lines each.
"""

from dataclasses import dataclass
from typing import Iterable, Optional, Sequence

from sqlalchemy import and_, exists, or_, union
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.courses.activities import Activity
from src.db.courses.assignments import (
    Assignment,
    AssignmentTask,
    AssignmentUserSubmission,
    AssignmentUserSubmissionStatus,
)
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.chapters import Chapter
from src.db.courses.course_chapters import CourseChapter
from src.db.trail_steps import TrailStep
from src.db.users import AnonymousUser
from src.security.auth import resolve_acting_user_id

SEQUENTIAL_SETTING_KEY = "rr_sequential_chapters"
LOCK_REASON_SEQUENTIAL = "sequential"

# A manual-graded assignment counts as done once it has left the draft states.
# PENDING / NOT_SUBMITTED are what "Try again" resets a submission to, so they
# must not count.
_SUBMITTED_STATES = (
    AssignmentUserSubmissionStatus.SUBMITTED,
    AssignmentUserSubmissionStatus.GRADED,
    AssignmentUserSubmissionStatus.LATE,
)


@dataclass(frozen=True)
class ChapterOutline:
    """One chapter in course order, with its published activity ids in order."""

    chapter_id: int
    chapter_uuid: str
    name: str
    activity_ids: tuple[int, ...]
    activity_uuids: tuple[str, ...] = ()


@dataclass(frozen=True)
class SequentialLock:
    """Why a chapter is locked: the earlier chapter the student must finish."""

    chapter_uuid: str
    chapter_name: str
    # First not-yet-done activity in that chapter, so the UI can link straight
    # to where the student should pick up.
    next_activity_uuid: Optional[str] = None

    def as_dict(self) -> dict:
        return {
            "chapter_uuid": self.chapter_uuid,
            "chapter_name": self.chapter_name,
            "next_activity_uuid": self.next_activity_uuid,
        }


def is_sequential(course) -> bool:
    meta = getattr(course, "extra_metadata", None)
    return isinstance(meta, dict) and meta.get(SEQUENTIAL_SETTING_KEY) is True


def compute_sequential_locks(
    outline: Sequence[ChapterOutline], done_activity_ids: set[int]
) -> dict[int, SequentialLock]:
    """Map chapter_id -> lock for every chapter after the first unfinished one.

    The earliest unfinished chapter blocks every later chapter (not only the
    next one), so finishing chapter 2's work early does not open chapter 3
    while chapter 1 is still open. A chapter with no published activities is
    trivially finished and never blocks.
    """
    locks: dict[int, SequentialLock] = {}
    blocker: Optional[SequentialLock] = None
    for chapter in outline:
        if blocker is not None:
            locks[chapter.chapter_id] = blocker
            continue
        pending = [
            i for i, aid in enumerate(chapter.activity_ids) if aid not in done_activity_ids
        ]
        if pending:
            uuids = chapter.activity_uuids
            blocker = SequentialLock(
                chapter_uuid=chapter.chapter_uuid,
                chapter_name=chapter.name,
                next_activity_uuid=uuids[pending[0]] if len(uuids) > pending[0] else None,
            )
    return locks


async def load_course_outline(course_id: int, db_session: AsyncSession) -> list[ChapterOutline]:
    """Chapters in course order with their published activities, in one query.

    Same chapter filter and order as ``get_course_chapters``.
    """
    rows = (await db_session.execute(
        select(
            Chapter.id, Chapter.chapter_uuid, Chapter.name,
            Activity.id, Activity.activity_uuid,
        )
        .join(CourseChapter, CourseChapter.chapter_id == Chapter.id)  # type: ignore[arg-type]
        .outerjoin(ChapterActivity, ChapterActivity.chapter_id == Chapter.id)  # type: ignore[arg-type]
        .outerjoin(
            Activity,
            and_(Activity.id == ChapterActivity.activity_id, Activity.published == True),  # noqa: E712
        )
        .where(CourseChapter.course_id == course_id, Chapter.course_id == course_id)
        .order_by(CourseChapter.order, Chapter.id, ChapterActivity.order, ChapterActivity.id)  # type: ignore[arg-type]
    )).all()
    order: list[int] = []
    meta: dict[int, tuple[str, str]] = {}
    acts: dict[int, list[tuple[int, str]]] = {}
    for chapter_id, chapter_uuid, name, activity_id, activity_uuid in rows:
        if chapter_id not in meta:
            order.append(chapter_id)
            meta[chapter_id] = (chapter_uuid, name)
            acts[chapter_id] = []
        if activity_id is not None and all(a != activity_id for a, _ in acts[chapter_id]):
            acts[chapter_id].append((activity_id, activity_uuid))
    return [
        ChapterOutline(
            chapter_id=cid,
            chapter_uuid=meta[cid][0],
            name=meta[cid][1],
            activity_ids=tuple(a for a, _ in acts[cid]),
            activity_uuids=tuple(u for _, u in acts[cid]),
        )
        for cid in order
    ]


def outline_from_chapter_reads(chapters: Iterable) -> list[ChapterOutline]:
    """Build the outline from an already-loaded chapter tree (``ChapterRead``)."""
    outline = []
    for chapter in chapters:
        published = [a for a in chapter.activities if a.published]
        outline.append(ChapterOutline(
            chapter_id=chapter.id,
            chapter_uuid=chapter.chapter_uuid,
            name=chapter.name,
            activity_ids=tuple(a.id for a in published),
            activity_uuids=tuple(a.activity_uuid for a in published),
        ))
    return outline


async def done_activity_ids(user_id: int, course_id: int, db_session: AsyncSession) -> set[int]:
    """Activities this user has done in this course, in one query.

    Done = a complete trail step, OR a submitted assignment that needs an
    instructor to grade it (auto-grading off, or any task type outside the
    auto-gradable allow-list). Fully auto-graded assignments only count
    through their trail step.
    """
    # Imported here: assignments.py imports the activity access gate, which
    # imports this module.
    from src.services.courses.activities.assignments import AUTO_GRADABLE_TASK_TYPES

    completed = select(TrailStep.activity_id).where(
        TrailStep.user_id == user_id,
        TrailStep.course_id == course_id,
        TrailStep.complete == True,  # noqa: E712
    )
    has_manual_task = exists().where(
        AssignmentTask.assignment_id == Assignment.id,
        AssignmentTask.assignment_type.notin_(list(AUTO_GRADABLE_TASK_TYPES)),  # type: ignore[attr-defined]
    )
    submitted_manual = (
        select(Assignment.activity_id)
        .join(AssignmentUserSubmission, AssignmentUserSubmission.assignment_id == Assignment.id)  # type: ignore[arg-type]
        .where(
            Assignment.course_id == course_id,
            AssignmentUserSubmission.user_id == user_id,
            AssignmentUserSubmission.submission_status.in_(_SUBMITTED_STATES),  # type: ignore[attr-defined]
            or_(Assignment.auto_grading == False, has_manual_task),  # noqa: E712
        )
    )
    rows = (await db_session.execute(union(completed, submitted_manual))).all()
    return {row[0] for row in rows if row[0] is not None}


async def sequential_locks_for_user(
    course,
    current_user,
    db_session: AsyncSession,
    *,
    outline: Optional[Sequence[ChapterOutline]] = None,
    bypass: bool = False,
) -> dict[int, SequentialLock]:
    """chapter_id -> lock for ``current_user`` in ``course``; empty when the
    setting is off or the caller bypasses.

    Callers resolve the bypass (org admins/maintainers, course editors) with
    the same checks they already use for the existing lock tiers. Anonymous
    readers have no progress, so everything past the first unfinished chapter
    is locked for them.
    """
    if bypass or course is None or course.id is None or not is_sequential(course):
        return {}
    if outline is None:
        outline = await load_course_outline(course.id, db_session)
    if len(outline) < 2:
        return {}
    done: set[int] = set()
    if not isinstance(current_user, AnonymousUser):
        done = await done_activity_ids(resolve_acting_user_id(current_user), course.id, db_session)
    return compute_sequential_locks(outline, done)


async def sequential_lock_for_chapter(
    course,
    chapter_id: Optional[int],
    current_user,
    db_session: AsyncSession,
) -> Optional[SequentialLock]:
    """The lock on one chapter, for single-activity reads."""
    if chapter_id is None or not is_sequential(course):
        return None
    locks = await sequential_locks_for_user(course, current_user, db_session)
    return locks.get(chapter_id)


async def sequential_locked_activity_ids(
    candidates: Sequence[tuple[int, int, Optional[int]]],
    current_user,
    db_session: AsyncSession,
) -> set[int]:
    """Of ``(activity_id, course_id, chapter_id)`` candidates, the activity ids
    behind a sequential lock. For batch readers (AI retrieval) that have
    already applied their own bypass. One course query, then the per-course
    lock queries only for courses with the setting on."""
    if not candidates:
        return set()
    from src.db.courses.courses import Course

    course_ids = {course_id for _, course_id, _ in candidates}
    courses = (await db_session.execute(
        select(Course).where(Course.id.in_(course_ids))  # type: ignore[union-attr]
    )).scalars().all()
    locked: set[int] = set()
    for course in courses:
        if not is_sequential(course):
            continue
        locks = await sequential_locks_for_user(course, current_user, db_session)
        locked |= {
            aid for aid, cid, chapter_id in candidates
            if cid == course.id and chapter_id in locks
        }
    return locked


def mark_activity_sequential_locked(activity_read, lock: Optional[SequentialLock]) -> None:
    """Scrub a single-activity read the same way the lock tiers do, with the
    sequential reason attached. No-op without a lock."""
    if lock is None:
        return
    activity_read.content = {}
    activity_read.details = None
    activity_read.is_locked = True
    activity_read.lock_reason = LOCK_REASON_SEQUENTIAL
    activity_read.lock_requires_chapter = lock.as_dict()


async def apply_sequential_locks_to_chapters(
    chapters: list,
    course,
    current_user,
    db_session: AsyncSession,
    request=None,
) -> None:
    """Mark sequential-locked chapters/activities in an already-built tree.

    Called after the lock tiers have run (admins have already returned).
    Activity content is stripped like any other lock. Chapter name,
    description and thumbnail stay visible: this is a progress gate for an
    enrolled student, not an access tier, and the student should see what is
    coming next.
    """
    if not chapters or not is_sequential(course):
        return
    locks = await sequential_locks_for_user(
        course, current_user, db_session, outline=outline_from_chapter_reads(chapters)
    )
    if not locks:
        return
    # Course editors (contributors/authors) bypass, as they do on the
    # single-activity read. Only checked when something would be locked; with
    # no request we cannot run the RBAC check, so stay locked (fail closed).
    if request is not None and not isinstance(current_user, AnonymousUser):
        from src.security.rbac.resource_access import AccessAction, check_resource_access

        decision = await check_resource_access(
            request, db_session, current_user, course.course_uuid, AccessAction.UPDATE,
            raise_on_deny=False,
        )
        if decision.allowed:
            return
    for chapter in chapters:
        lock = locks.get(chapter.id)
        if lock is None:
            continue
        # An item already locked by an access tier keeps that reason: finishing
        # the earlier chapter would not open it.
        if not chapter.is_locked:
            chapter.lock_reason = LOCK_REASON_SEQUENTIAL
            chapter.lock_requires_chapter = lock.as_dict()
        chapter.is_locked = True
        for activity in chapter.activities:
            if not activity.is_locked:
                activity.lock_reason = LOCK_REASON_SEQUENTIAL
                activity.lock_requires_chapter = lock.as_dict()
            activity.is_locked = True
            activity.content = {}
            activity.details = None
