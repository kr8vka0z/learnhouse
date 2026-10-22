"""Rescue Ready fork: two overlapping grade passes record ONE completion.

Needs a real Postgres (SQLite ignores SELECT ... FOR UPDATE, so it cannot show
the race). Skipped unless ``RR_TEST_POSTGRES_URL`` points at a throwaway
database (asyncpg URL); the test drops and recreates every table in it. E.g.:

    docker run --rm -d -e POSTGRES_PASSWORD=test -e POSTGRES_DB=rrtest \\
        -p 127.0.0.1:55439:5432 postgres:16-alpine
    RR_TEST_POSTGRES_URL=postgresql+asyncpg://postgres:test@127.0.0.1:55439/rrtest \\
        TESTING=true uv run pytest src/tests/services/test_rr_completion_lock_pg.py
"""

import asyncio
import os

import pytest
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine
from sqlmodel import SQLModel
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.user_audit_events import UserAuditEventType
from src.services.courses.activities.assignments import grade_assignment_submission
from src.tests.services.test_rr_assignment_completion import (  # noqa: F401 (env is a fixture)
    _assignment,
    _attempt,
    _audits,
    _step,
    env,
)

PG_URL = os.environ.get("RR_TEST_POSTGRES_URL")
pytestmark = pytest.mark.skipif(not PG_URL, reason="RR_TEST_POSTGRES_URL not set")


@pytest.fixture
async def engine():
    """Overrides conftest's SQLite engine, so every conftest fixture seeds Postgres."""
    # Plain postgres image: skip the pgvector (RAG) tables, not used here.
    tables = [
        t for t in SQLModel.metadata.sorted_tables
        if not any(type(c.type).__name__ == "VECTOR" for c in t.columns)
    ]
    eng = create_async_engine(PG_URL)
    async with eng.begin() as conn:
        await conn.run_sync(lambda c: SQLModel.metadata.drop_all(c, tables=tables))
        await conn.run_sync(lambda c: SQLModel.metadata.create_all(c, tables=tables))
    yield eng
    await eng.dispose()


async def test_concurrent_passing_grades_write_one_completion(
    env, engine, mock_request, db, org, course, chapter, activity, regular_user, admin_user
):
    a = await _assignment(db, org, course, chapter, activity, auto_grading=False)
    await _attempt(mock_request, db, a, regular_user, "4")  # correct, awaits a manual grade

    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)

    async def grade():
        async with factory() as s:
            await grade_assignment_submission(
                mock_request, regular_user.id, a.assignment_uuid, admin_user, s
            )

    await asyncio.gather(grade(), grade())

    completed = _audits(env, UserAuditEventType.ACTIVITY_COMPLETED)
    assert len(completed) == 1
    assert [c.kwargs.get("event_name") for c in env["dispatch"].await_args_list].count(
        "course_completed"
    ) <= 1
    step = await _step(db, activity, regular_user)
    assert step.complete is True
