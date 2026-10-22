"""Completion report routes (Rescue Ready fork).

The school's official record of module completion, per student and per course
(see ``services/audit/completion_report.py``). Same gate as the audit dossier:
caller must be an admin of ``org_id`` on a plan that unlocks student records,
the target student must be a member of that org, and the course must belong to
it. Kept in its own router so upstream changes to ``routers/audit.py`` merge
cleanly.
"""
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import HTMLResponse, StreamingResponse
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core.events.database import get_db_session
from src.db.courses.courses import Course
from src.db.users import AnonymousUser, APITokenUser, PublicUser
from src.routers.audit import (
    _enforce_plan,
    _org_member_ids,
    _require_admin,
    _verify_target_in_org,
)
from src.security.auth import get_current_user
from src.services.audit.completion_report import (
    build_completion_report,
    bulk_csv_header,
    bulk_csv_rows,
    display_zone,
    enrolled_user_ids,
    render_student_csv,
    render_student_html,
    report_to_json,
)

router = APIRouter()


async def _course_in_org(course_uuid: str, org_id: int, db_session: AsyncSession) -> Course:
    course = (await db_session.execute(
        select(Course).where(Course.course_uuid == course_uuid, Course.org_id == org_id)
    )).scalars().first()
    if not course:
        raise HTTPException(status_code=404, detail="Course not found in this organization")
    return course


def _slug(text: str) -> str:
    return "".join(c if c.isalnum() else "-" for c in (text or "")).strip("-")[:40] or "course"


@router.get(
    "/completion-report/user/{user_id}",
    summary="Per-student completion report for one course",
    description=(
        "One row per activity in course order: chapter, activity, type, completed and when, "
        "and for assignments every attempt's score and pass/fail plus the date first passed. "
        "format=csv (default), json, or html (printable). Times are in the configured display "
        "zone, named in the header. Org admin + plan."
    ),
    responses={
        200: {"description": "The report"},
        401: {"description": "Authentication required"},
        403: {"description": "Caller is not an org admin or the org lacks the plan"},
        404: {"description": "Student, course or enrollment not found in this organization"},
    },
)
async def get_completion_report(
    user_id: int,
    org_id: int,
    course_uuid: str,
    format: str = Query("csv", pattern="^(csv|json|html)$"),
    current_user: PublicUser | AnonymousUser | APITokenUser = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db_session),
):
    acting_user_id = await _require_admin(current_user, org_id, db_session)
    await _enforce_plan(org_id, db_session)
    await _verify_target_in_org(user_id, org_id, db_session, acting_user_id)
    course = await _course_in_org(course_uuid, org_id, db_session)

    report = await build_completion_report(db_session, user_id=user_id, org_id=org_id, course=course)
    if not report:
        raise HTTPException(status_code=404, detail="Student is not enrolled in this course")

    if format == "json":
        return report_to_json(report)
    if format == "html":
        return HTMLResponse(render_student_html(report))
    filename = f"completion-{_slug(report['student']['username'])}-{_slug(course.name)}.csv"
    return StreamingResponse(
        iter([render_student_csv(report)]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get(
    "/completion-report/course/{course_uuid}",
    summary="Completion records for every enrolled student in a course (CSV)",
    description=(
        "One row per enrolled student per activity, with the same columns as the per-student "
        "report plus student name and email. Org admin + plan."
    ),
    responses={
        200: {"description": "CSV"},
        401: {"description": "Authentication required"},
        403: {"description": "Caller is not an org admin or the org lacks the plan"},
        404: {"description": "Course not found in this organization"},
    },
)
async def get_course_completion_records(
    course_uuid: str,
    org_id: int,
    current_user: PublicUser | AnonymousUser | APITokenUser = Depends(get_current_user),
    db_session: AsyncSession = Depends(get_db_session),
):
    acting_user_id = await _require_admin(current_user, org_id, db_session)
    await _enforce_plan(org_id, db_session)
    course = await _course_in_org(course_uuid, org_id, db_session)

    enrolled = await enrolled_user_ids(db_session, course.id)
    # Same membership rule as the dossier export (including the demo-org filter):
    # a student who left the org is not exported.
    members = await _org_member_ids(enrolled, org_id, db_session, acting_user_id) if enrolled else set()
    user_ids = [u for u in enrolled if u in members]
    zone_name = display_zone().key

    # Built before streaming (as /audit/export does) so no query runs after the
    # request's DB session dependency has been torn down.
    # ponytail: one report build (a handful of queries) per student; fine for a
    # school's roster. Batch the queries if a course grows to thousands.
    chunks = [bulk_csv_header(zone_name)]
    for uid in user_ids:
        report = await build_completion_report(db_session, user_id=uid, org_id=org_id, course=course)
        if report:
            chunks.append(bulk_csv_rows(report))

    return StreamingResponse(
        iter(chunks),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="completion-records-{_slug(course.name)}.csv"'},
    )
