"""Per-student completion report (Rescue Ready fork).

The report is the school's official record of module completion, so these tests
build their data through the production write paths (``add_activity_to_trail``,
``create_assignment_submission``, ``_apply_grade_and_finalize``,
``retry_assignment_submission`` and the real ``record_audit_event``) rather than
hand-inserting trail or audit rows. Only side channels (analytics, webhooks,
certificates, RBAC/reader gates owned by other code) are patched.
"""
import csv
import io
from contextlib import ExitStack
from datetime import UTC, datetime
from unittest.mock import AsyncMock, patch
from zoneinfo import ZoneInfo

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import async_sessionmaker
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.events.database import get_db_session
from src.db.courses.activities import Activity, ActivitySubTypeEnum, ActivityTypeEnum
from src.db.courses.assignments import (
    Assignment,
    AssignmentTask,
    AssignmentTaskSubmission,
    AssignmentTaskTypeEnum,
    AssignmentUserSubmission,
    GradingTypeEnum,
)
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.chapters import Chapter
from src.db.courses.course_chapters import CourseChapter
from src.db.organization_config import OrganizationConfig
from src.db.user_audit_events import UserAuditEvent, UserAuditEventType
from src.db.user_organizations import UserOrganization
from src.db.users import AnonymousUser, User
from src.routers.completion_report import router as completion_router
from src.security.auth import get_current_user
from src.services.audit import completion_report as cr
from src.services.courses.activities.assignments import (
    _apply_grade_and_finalize,
    create_assignment_submission,
    retry_assignment_submission,
)
from src.services.trail.trail import add_activity_to_trail

DENVER = ZoneInfo("America/Denver")


def _now() -> str:
    return str(datetime.now())


# ---------------------------------------------------------------------------
# Course structure: chapter/activity ids deliberately run opposite to their
# display order so a report that sorts by id instead of `order` fails.
# ---------------------------------------------------------------------------
async def _add_chapter(db, org, course, cid, name, order):
    db.add(Chapter(
        id=cid, name=name, description="", org_id=org.id, course_id=course.id,
        chapter_uuid=f"chapter_{cid}", creation_date=_now(), update_date=_now(),
    ))
    await db.commit()
    db.add(CourseChapter(
        chapter_id=cid, course_id=course.id, org_id=org.id, order=order,
        creation_date=_now(), update_date=_now(),
    ))
    await db.commit()


async def _add_activity(db, org, course, chapter_id, aid, name, order, atype, published=True):
    sub = {
        ActivityTypeEnum.TYPE_VIDEO: ActivitySubTypeEnum.SUBTYPE_VIDEO_HOSTED,
        ActivityTypeEnum.TYPE_DOCUMENT: ActivitySubTypeEnum.SUBTYPE_DOCUMENT_PDF,
        ActivityTypeEnum.TYPE_ASSIGNMENT: ActivitySubTypeEnum.SUBTYPE_ASSIGNMENT_ANY,
    }.get(atype, ActivitySubTypeEnum.SUBTYPE_DYNAMIC_PAGE)
    db.add(Activity(
        id=aid, name=name, activity_type=atype, activity_sub_type=sub, content={},
        published=published, org_id=org.id, course_id=course.id,
        activity_uuid=f"activity_{aid}", creation_date=_now(), update_date=_now(),
    ))
    await db.commit()
    db.add(ChapterActivity(
        order=order, chapter_id=chapter_id, activity_id=aid, course_id=course.id,
        org_id=org.id, creation_date=_now(), update_date=_now(),
    ))
    await db.commit()


@pytest.fixture
async def structure(db, org, course):
    # Chapter 1 on screen = id 11; chapter 2 = id 10.
    await _add_chapter(db, org, course, 10, "Unit Safety", order=2)
    await _add_chapter(db, org, course, 11, "Unit Basics", order=1)
    await _add_activity(db, org, course, 11, 20, "Basics video", 2, ActivityTypeEnum.TYPE_VIDEO)
    await _add_activity(db, org, course, 11, 21, "Basics reading", 1, ActivityTypeEnum.TYPE_DOCUMENT)
    await _add_activity(db, org, course, 10, 22, "Safety quiz", 1, ActivityTypeEnum.TYPE_ASSIGNMENT)
    await _add_activity(db, org, course, 10, 23, "Old draft page", 2, ActivityTypeEnum.TYPE_DYNAMIC, published=False)
    await _add_activity(db, org, course, 10, 24, "Hidden page", 3, ActivityTypeEnum.TYPE_DYNAMIC, published=False)

    assignment = Assignment(
        id=5, title="Safety quiz", description="", due_date="2030-01-01", published=True,
        grading_type=GradingTypeEnum.NUMERIC, auto_grading=False, allow_retries=True,
        max_retries=0, org_id=org.id, course_id=course.id, chapter_id=10, activity_id=22,
        assignment_uuid="assignment_safety", creation_date=_now(), update_date=_now(),
    )
    db.add(assignment)
    await db.commit()
    task = AssignmentTask(
        id=6, title="Q1", description="", hint="", reference_file=None,
        assignment_type=AssignmentTaskTypeEnum.SHORT_ANSWER,
        contents={"correct_answers": ["4"], "match_mode": "exact"}, max_grade_value=100,
        assignment_id=5, org_id=org.id, course_id=course.id, chapter_id=10, activity_id=22,
        assignment_task_uuid="assignmenttask_q1", creation_date=_now(), update_date=_now(),
    )
    db.add(task)
    await db.commit()
    await db.refresh(assignment)
    await db.refresh(task)
    return {"assignment": assignment, "task": task}


@pytest.fixture
async def pro_plan(db, org):
    db.add(OrganizationConfig(org_id=org.id, config={"config_version": "2.0", "plan": "pro"}))
    await db.commit()


@pytest.fixture
def real_audit_writes(engine):
    """Point the production audit writer at the test database."""
    factory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    with patch("src.core.events.database._async_session_factory", factory):
        yield


def _side_channels():
    """Patch analytics, webhooks, certificates and the RBAC/reader gates (owned
    elsewhere and tested elsewhere); everything that writes records stays real."""
    stack = ExitStack()
    for target in (
        "src.services.trail.trail.track",
        "src.services.trail.trail.dispatch_webhooks",
        "src.services.trail.trail.verify_activity_reader_access",
        "src.services.trail.trail.check_course_completion_and_create_certificate",
        "src.services.courses.activities.assignments.check_resource_access",
        "src.services.courses.activities.assignments.track",
        "src.services.courses.activities.assignments.dispatch_webhooks",
        "src.services.courses.activities.assignments.check_course_completion_and_create_certificate",
    ):
        stack.enter_context(patch(target, new_callable=AsyncMock))
    return stack


async def _answer(db, task, user_id, grade):
    """The student's per-task answer, graded by the teacher."""
    db.add(AssignmentTaskSubmission(
        assignment_task_submission_uuid=f"ats_{user_id}_{grade}",
        task_submission={"answer": "x"}, grade=grade, manually_graded=True,
        task_submission_grade_feedback="", assignment_type=task.assignment_type,
        user_id=user_id, activity_id=task.activity_id, course_id=task.course_id,
        chapter_id=task.chapter_id, assignment_task_id=task.id,
        creation_date=_now(), update_date=_now(),
    ))
    await db.commit()


async def _submit_and_grade(db, mock_request, user, course, assignment, task, grade):
    await create_assignment_submission(mock_request, assignment.assignment_uuid, user, db)
    await _answer(db, task, user.id, grade)
    sub = (await db.execute(select(AssignmentUserSubmission).where(
        AssignmentUserSubmission.assignment_id == assignment.id,
        AssignmentUserSubmission.user_id == user.id,
    ))).scalars().first()
    await _apply_grade_and_finalize(
        assignment=assignment, course=course, user_id=user.id,
        assignment_user_submission=sub, db_session=db,
    )


@pytest.fixture
async def progress(db, mock_request, org, course, structure, regular_user, real_audit_writes):
    """Student completes the reading and a since-unpublished page, skips the
    video, fails the quiz, retries, then passes it."""
    assignment, task = structure["assignment"], structure["task"]
    with _side_channels():
        await add_activity_to_trail(mock_request, regular_user, "activity_21", db)
        await add_activity_to_trail(mock_request, regular_user, "activity_23", db)
        await _submit_and_grade(db, mock_request, regular_user, course, assignment, task, 20)
        await retry_assignment_submission(mock_request, assignment.assignment_uuid, regular_user, db)
        await _submit_and_grade(db, mock_request, regular_user, course, assignment, task, 90)
    return structure


async def _report(db, user_id, org, course):
    return await cr.build_completion_report(db, user_id=user_id, org_id=org.id, course=course)


# ---------------------------------------------------------------------------
# Report content
# ---------------------------------------------------------------------------
class TestReportContent:
    async def test_rows_follow_course_order_with_names(self, db, org, course, regular_user, progress):
        report = await _report(db, regular_user.id, org, course)
        got = [
            (r["chapter_number"], r["chapter_name"], r["activity_order"], r["activity_name"])
            for r in report["rows"]
        ]
        # The unpublished page the student never opened (id 24) is left out.
        assert got == [
            (1, "Unit Basics", 1, "Basics reading"),
            (1, "Unit Basics", 2, "Basics video"),
            (2, "Unit Safety", 1, "Safety quiz"),
            (2, "Unit Safety", 2, "Old draft page"),
        ]
        assert report["student"]["email"] == "regular@test.com"
        assert report["course"]["name"] == course.name
        assert report["enrolled_at"] is not None

    async def test_completed_vs_not_completed(self, db, org, course, regular_user, progress):
        rows = {r["activity_name"]: r for r in (await _report(db, regular_user.id, org, course))["rows"]}
        assert rows["Basics reading"]["completed"] is True
        assert rows["Basics reading"]["completed_at"] is not None
        assert rows["Basics video"]["completed"] is False
        assert rows["Basics video"]["completed_at"] is None
        assert rows["Safety quiz"]["completed"] is True

    async def test_unpublished_activity_only_when_completed_and_flagged(
        self, db, org, course, regular_user, progress
    ):
        rows = {r["activity_name"]: r for r in (await _report(db, regular_user.id, org, course))["rows"]}
        assert rows["Old draft page"]["published"] is False
        assert rows["Old draft page"]["completed"] is True
        assert "Hidden page" not in rows

    async def test_fail_then_pass_both_attempts_survive_the_retry(
        self, db, org, course, regular_user, progress
    ):
        # The retry deleted attempt 1's answers from the live tables...
        left = (await db.execute(select(AssignmentTaskSubmission).where(
            AssignmentTaskSubmission.user_id == regular_user.id
        ))).scalars().all()
        assert [ts.grade for ts in left] == [90]

        quiz = next(r for r in (await _report(db, regular_user.id, org, course))["rows"]
                    if r["activity_name"] == "Safety quiz")
        # ...but the report still carries both attempts from the audit log.
        assert [(a["attempt_number"], a["score_pct"], a["passed"]) for a in quiz["attempts"]] == [
            (1, 20.0, False),
            (2, 90.0, True),
        ]
        passed_row = (await db.execute(select(UserAuditEvent).where(
            UserAuditEvent.event_type == UserAuditEventType.ASSIGNMENT_GRADED,
        ).order_by(UserAuditEvent.id.desc()))).scalars().first()
        assert passed_row.audit_metadata["passed"] is True
        expected = cr.to_display(passed_row.created_at, DENVER)
        assert quiz["first_passed_at"] == expected
        assert quiz["attempts"][1]["graded_at"] == expected

    async def test_first_passed_is_earliest_pass_not_latest(
        self, db, mock_request, org, course, regular_user, structure, real_audit_writes
    ):
        """A later passing regrade must not move the first-passed date. Graded rows
        are inserted directly here only because the test needs fixed timestamps."""
        with _side_channels():
            await add_activity_to_trail(mock_request, regular_user, "activity_21", db)
        early = datetime(2026, 3, 1, 15, 0, tzinfo=UTC)
        late = datetime(2026, 3, 5, 15, 0, tzinfo=UTC)
        for when, attempt in ((early, 1), (late, 2)):
            db.add(UserAuditEvent(
                event_type=UserAuditEventType.ASSIGNMENT_GRADED, user_id=regular_user.id,
                org_id=org.id, target_uuid="assignment_safety", created_at=when,
                audit_metadata={"percentage": 80.0, "passed": True, "attempt_number": attempt},
            ))
        await db.commit()
        quiz = next(r for r in (await _report(db, regular_user.id, org, course))["rows"]
                    if r["activity_name"] == "Safety quiz")
        assert quiz["first_passed_at"] == early.astimezone(DENVER)

    @pytest.mark.parametrize("grades, expected_day", [
        ([(1, True), (2, False)], None),            # pass -> fail regrade clears it
        ([(1, False), (2, True)], 2),               # fail -> pass regrade dates from the regrade
        ([(1, True), (2, True)], 1),                # pass -> pass keeps the original date
        ([(1, True), (2, False), (3, True)], 3),    # passing again restarts the date
    ])
    async def test_teacher_regrade_of_one_attempt_drives_first_passed(
        self, db, mock_request, org, course, regular_user, structure, real_audit_writes,
        grades, expected_day,
    ):
        """First passed must agree with the attempt's FINAL grade on an official
        record. Fixed timestamps, so the graded rows are inserted directly."""
        with _side_channels():
            await add_activity_to_trail(mock_request, regular_user, "activity_21", db)
        for day, passed in grades:
            db.add(UserAuditEvent(
                event_type=UserAuditEventType.ASSIGNMENT_GRADED, user_id=regular_user.id,
                org_id=org.id, target_uuid="assignment_safety",
                created_at=datetime(2026, 3, day, 15, 0, tzinfo=UTC),
                audit_metadata={"percentage": 80.0 if passed else 30.0, "passed": passed, "attempt_number": 1},
            ))
        await db.commit()
        quiz = next(r for r in (await _report(db, regular_user.id, org, course))["rows"]
                    if r["activity_name"] == "Safety quiz")
        assert [a["passed"] for a in quiz["attempts"]] == [grades[-1][1]]
        expected = None if expected_day is None else datetime(2026, 3, expected_day, 15, 0, tzinfo=UTC).astimezone(DENVER)
        assert quiz["first_passed_at"] == expected

    async def test_not_enrolled_returns_none(self, db, org, course, regular_user, structure):
        assert await _report(db, regular_user.id, org, course) is None


# ---------------------------------------------------------------------------
# Time zone handling
# ---------------------------------------------------------------------------
class TestTimeZone:
    def test_naive_trail_string_is_treated_as_utc(self):
        # Trail rows store str(datetime.now()) from a UTC container clock.
        got = cr.to_display("2026-10-22 01:27:38.444103", DENVER)
        assert got == datetime(2026, 10, 21, 19, 27, 38, 444103, tzinfo=DENVER)

    def test_aware_datetime_is_converted_not_relabelled(self):
        aware = datetime(2026, 1, 15, 12, 0, tzinfo=UTC)
        got = cr.to_display(aware, DENVER)
        assert (got.hour, got.utcoffset().total_seconds()) == (5, -7 * 3600)

    def test_naive_datetime_is_treated_as_utc(self):
        got = cr.to_display(datetime(2026, 7, 1, 12, 0), DENVER)
        assert (got.hour, got.utcoffset().total_seconds()) == (6, -6 * 3600)

    def test_aware_string_keeps_its_offset(self):
        got = cr.to_display("2026-07-01T12:00:00+02:00", DENVER)
        assert got.hour == 4

    @pytest.mark.parametrize("value", [None, "", "   ", "not a date"])
    def test_blank_or_garbage_is_none(self, value):
        assert cr.to_display(value, DENVER) is None

    def test_zone_from_env_and_bad_zone_falls_back_to_utc(self, monkeypatch):
        monkeypatch.setenv(cr.REPORT_TZ_ENV, "America/New_York")
        assert cr.display_zone().key == "America/New_York"
        monkeypatch.setenv(cr.REPORT_TZ_ENV, "Not/AZone")
        assert cr.display_zone().key == "UTC"
        monkeypatch.delenv(cr.REPORT_TZ_ENV)
        assert cr.display_zone().key == cr.DEFAULT_REPORT_TZ


# ---------------------------------------------------------------------------
# CSV safety
# ---------------------------------------------------------------------------
class TestCsvSafety:
    @pytest.mark.parametrize("evil", ["=HYPERLINK(\"x\")", "+1+1", "-2+3", "@SUM(A1)"])
    async def test_formula_cells_are_neutralised(self, db, org, course, regular_user, progress, evil):
        activity = await db.get(Activity, 21)
        activity.name = evil
        await db.commit()
        text = cr.render_student_csv(await _report(db, regular_user.id, org, course))
        cells = [c for row in csv.reader(io.StringIO(text)) for c in row]
        assert "'" + evil in cells
        assert evil not in cells


# ---------------------------------------------------------------------------
# HTTP: permissions and formats
# ---------------------------------------------------------------------------
@pytest.fixture
def app(db, admin_user):
    app = FastAPI()
    app.include_router(completion_router, prefix="/api/v1/audit")
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: admin_user
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as c:
        yield c


def _user_url(user_id, fmt="csv"):
    return f"/api/v1/audit/completion-report/user/{user_id}?org_id=1&course_uuid=course_test&format={fmt}"


COURSE_URL = "/api/v1/audit/completion-report/course/course_test?org_id=1"


class TestPermissions:
    async def test_admin_gets_csv(self, client, pro_plan, regular_user, progress):
        resp = await client.get(_user_url(regular_user.id))
        assert resp.status_code == 200
        assert resp.headers["content-type"].startswith("text/csv")
        assert "Safety quiz" in resp.text

    @pytest.mark.parametrize("url", ["user", "course"])
    async def test_regular_user_forbidden(self, app, client, pro_plan, regular_user, progress, url):
        app.dependency_overrides[get_current_user] = lambda: regular_user
        resp = await client.get(_user_url(regular_user.id) if url == "user" else COURSE_URL)
        assert resp.status_code == 403

    @pytest.mark.parametrize("url", ["user", "course"])
    async def test_anonymous_unauthorized(self, app, client, pro_plan, regular_user, url):
        app.dependency_overrides[get_current_user] = lambda: AnonymousUser()
        resp = await client.get(_user_url(regular_user.id) if url == "user" else COURSE_URL)
        assert resp.status_code == 401

    async def test_target_outside_org_is_404(self, db, client, pro_plan, other_org, user_role, structure):
        db.add(User(
            id=99, username="outsider", first_name="Out", last_name="Sider", email="out@example.com",
            password="x", user_uuid="user_outsider", creation_date=_now(), update_date=_now(),
        ))
        await db.commit()
        db.add(UserOrganization(
            user_id=99, org_id=other_org.id, role_id=user_role.id,
            creation_date=_now(), update_date=_now(),
        ))
        await db.commit()
        resp = await client.get(_user_url(99))
        assert resp.status_code == 404

    async def test_course_from_another_org_is_404(self, db, client, pro_plan, other_org, regular_user):
        from src.db.courses.courses import Course
        db.add(Course(
            id=50, name="Elsewhere", description="", public=True, published=True,
            open_to_contributors=False, org_id=other_org.id, course_uuid="course_elsewhere",
            creation_date=_now(), update_date=_now(),
        ))
        await db.commit()
        resp = await client.get(
            f"/api/v1/audit/completion-report/user/{regular_user.id}?org_id=1&course_uuid=course_elsewhere"
        )
        assert resp.status_code == 404
        resp = await client.get("/api/v1/audit/completion-report/course/course_elsewhere?org_id=1")
        assert resp.status_code == 404


class TestFormats:
    async def test_json_variant_carries_zone_and_iso_offsets(self, client, pro_plan, regular_user, progress):
        resp = await client.get(_user_url(regular_user.id, "json"))
        assert resp.status_code == 200
        body = resp.json()
        assert body["timezone"] == "America/Denver"
        quiz = next(r for r in body["rows"] if r["activity_name"] == "Safety quiz")
        assert len(quiz["attempts"]) == 2
        assert quiz["first_passed_at"][-6:] in ("-06:00", "-07:00")

    async def test_bulk_csv_has_one_row_per_student_per_activity(
        self, db, client, pro_plan, regular_user, admin_user, progress, mock_request, real_audit_writes
    ):
        # A second student, enrolled through the real trail path.
        with _side_channels():
            await add_activity_to_trail(mock_request, admin_user, "activity_20", db)
        resp = await client.get(COURSE_URL)
        assert resp.status_code == 200
        rows = list(csv.DictReader(io.StringIO(resp.text)))
        by_student = {}
        for r in rows:
            by_student.setdefault(r["Student email"], []).append(r)
        assert len(by_student["regular@test.com"]) == 4  # includes the completed draft page
        assert len(by_student["admin@test.com"]) == 3   # published activities only
        tz_col = "Completed at (America/Denver)"
        done = {r["Activity"]: r[tz_col] for r in by_student["admin@test.com"]}
        assert done["Basics video"] and not done["Basics reading"]
