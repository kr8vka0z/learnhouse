"""Rescue Ready fork: essay questions (long written answer, graded by an instructor).

An essay is a CUSTOM assignment task whose ``contents`` carries
``{"kind": "essay", "prompt", "min_words"?, "max_words"?, "explanation"?}``.
CUSTOM was chosen over a new task type because ``assignmenttasktypeenum`` is a
real Postgres enum (a new value means a migration on every upgrade) and CUSTOM
already has the behaviour an essay needs: never auto-graded, so the attempt
waits in the grading queue. The model answer / rubric lives in ``explanation``
so the existing ``_strip_answer_key`` hides it from students until grading
(and only reveals it when the assignment shows correct answers).

The learner answer is ``task_submission = {"answer": "<plain text>"}``. CUSTOM
tasks without ``kind == "essay"`` are untouched by everything here.
"""

from fastapi import HTTPException
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.courses.assignments import (
    AssignmentTask,
    AssignmentTaskSubmission,
    AssignmentTaskTypeEnum,
)

# Audit snapshot cap for essay answers, in characters of JSON. The default cap
# (8000) would replace a long essay with a size marker, losing the one record
# of what the student wrote on an attempt a retry later wipes. 100k characters
# is roughly 15,000 words; the 2 MB submission cap still bounds the payload.
ESSAY_SNAPSHOT_MAX_CHARS = 100_000


def is_essay_task(task) -> bool:
    contents = getattr(task, "contents", None)
    return (
        getattr(task, "assignment_type", None) == AssignmentTaskTypeEnum.CUSTOM
        and isinstance(contents, dict)
        and contents.get("kind") == "essay"
    )


def count_words(text: str) -> int:
    # Must match countWords() in the web essay component (split on any run of
    # whitespace), or a student could pass the client check and fail this one.
    return len(text.split())


def _limit(value) -> int | None:
    # ponytail: a malformed limit (non-int, bool, <= 0) counts as "no limit"
    # rather than being validated when the instructor saves the task.
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        return None
    return value


def word_limits(contents: dict) -> tuple[int | None, int | None]:
    lo, hi = _limit(contents.get("min_words")), _limit(contents.get("max_words"))
    if lo is not None and hi is not None and lo > hi:
        return None, None
    return lo, hi


async def check_essay_word_limits(
    assignment_id: int | None, user_id: int, db_session: AsyncSession
) -> None:
    """Refuse a hand-in whose essay answers break the task's word limits.

    Run at hand-in rather than on each draft save: drafts legitimately pass
    through over-limit states while the student edits, and the instructor's
    manual grade re-sends the stored answer through the save path.
    """
    tasks = [
        t for t in (await db_session.execute(
            select(AssignmentTask).where(AssignmentTask.assignment_id == assignment_id)
        )).scalars().all()
        if is_essay_task(t)
    ]
    if not tasks:
        return
    rows = {
        ts.assignment_task_id: ts
        for ts in (await db_session.execute(
            select(AssignmentTaskSubmission).where(
                AssignmentTaskSubmission.user_id == user_id,
                AssignmentTaskSubmission.assignment_task_id.in_(  # type: ignore[attr-defined]
                    [t.id for t in tasks]
                ),
            )
        )).scalars().all()
    }
    for task in tasks:
        lo, hi = word_limits(task.contents)
        ts = rows.get(task.id)
        answer = (ts.task_submission or {}).get("answer", "") if ts else ""
        if not isinstance(answer, str):
            raise HTTPException(
                status_code=400,
                detail=f"The answer to \"{task.title}\" must be plain text.",
            )
        words = count_words(answer)
        if lo is not None and words < lo:
            raise HTTPException(
                status_code=400,
                detail=f"\"{task.title}\" needs at least {lo} words (you have {words}).",
            )
        if hi is not None and words > hi:
            raise HTTPException(
                status_code=400,
                detail=f"\"{task.title}\" allows at most {hi} words (you have {words}).",
            )
