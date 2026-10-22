"""Per-student, per-course completion report (Rescue Ready fork).

The school's official record of module completion: one row per activity in
course order, with completion state/date and, for assignments, every attempt's
score and pass/fail. Read-only; it stores nothing new.

Sources:
- Course structure: ``CourseChapter``/``ChapterActivity`` order, read directly
  (not through ``get_course_chapters``, which applies reader locks and hides
  drafts; a records report must show what the student did, not what a reader
  may see today).
- Completion: ``TrailStep``. ``update_date`` is written only when ``complete``
  flips (first completion, retry reset, re-completion), so it is the moment the
  current completion state began; readable steps are never updated after insert.
- Attempts: ``user_audit_event`` ASSIGNMENT_SUBMITTED / ASSIGNMENT_GRADED rows,
  because a retry deletes the earlier attempt's answers from the live tables.
- Cross-check: the latest ACTIVITY_COMPLETED audit row per activity is exposed
  separately (JSON only), never merged into ``completed_at``.

Time zones: trail, enrollment and certificate dates are ``str(datetime.now())``
strings written with no zone. The API container runs on UTC (no ``TZ`` in the
Dockerfiles; a trial deployment showed trail strings matching the timestamptz
audit rows to the millisecond in UTC), so naive values are read as UTC. Audit
rows are timestamptz and keep their own offset. Everything is converted to one
display zone, ``LEARNHOUSE_REPORT_TIMEZONE`` (default ``America/Denver``).
"""
import csv
import html
import io
import logging
import os
from collections.abc import Iterable
from datetime import UTC, datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.courses.activities import Activity, ActivityTypeEnum
from src.db.courses.assignments import Assignment
from src.db.courses.certifications import CertificateUser, Certifications
from src.db.courses.chapter_activities import ChapterActivity
from src.db.courses.chapters import Chapter
from src.db.courses.course_chapters import CourseChapter
from src.db.courses.courses import Course
from src.db.trail_runs import StatusEnum, TrailRun
from src.db.trail_steps import TrailStep
from src.db.user_audit_events import UserAuditEvent, UserAuditEventType
from src.db.users import User
from src.services.orgs.users import _csv_safe

logger = logging.getLogger(__name__)

REPORT_TZ_ENV = "LEARNHOUSE_REPORT_TIMEZONE"
DEFAULT_REPORT_TZ = "America/Denver"

_TYPE_LABELS = {
    ActivityTypeEnum.TYPE_VIDEO.value: "Video",
    ActivityTypeEnum.TYPE_DOCUMENT.value: "Document",
    ActivityTypeEnum.TYPE_DYNAMIC.value: "Page",
    ActivityTypeEnum.TYPE_ASSIGNMENT.value: "Assignment",
    ActivityTypeEnum.TYPE_CUSTOM.value: "Custom",
    ActivityTypeEnum.TYPE_SCORM.value: "SCORM",
}


# ---------------------------------------------------------------------------
# Time handling
# ---------------------------------------------------------------------------
def display_zone() -> ZoneInfo:
    """The report's display zone. A bad value falls back to UTC (and the report
    header names the zone actually used) rather than failing the export."""
    name = (os.environ.get(REPORT_TZ_ENV) or DEFAULT_REPORT_TZ).strip()
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("Unknown %s=%r; completion reports fall back to UTC", REPORT_TZ_ENV, name)
        return ZoneInfo("UTC")


def parse_timestamp(value) -> datetime | None:
    """Datetime or stored string -> aware datetime. Naive means UTC (see module
    docstring; SQLite also hands timestamptz columns back naive). Blank or unparseable -> None: one bad legacy string must not
    break an official export."""
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None
        try:
            dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            logger.warning("Unparseable timestamp in completion report: %r", text)
            return None
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def to_display(value, zone: ZoneInfo) -> datetime | None:
    dt = parse_timestamp(value)
    return dt.astimezone(zone) if dt else None


def _fmt_local(dt: datetime | None) -> str:
    # Spreadsheet-parseable; the zone is named in the column header.
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else ""


def _fmt_iso(dt: datetime | None) -> str | None:
    return dt.isoformat(timespec="seconds") if dt else None


# ---------------------------------------------------------------------------
# Builder
# ---------------------------------------------------------------------------
async def _course_activities(db: AsyncSession, course_id: int) -> list[tuple]:
    """(chapter_number, chapter, activity_order, activity) in display order."""
    rows = (await db.execute(
        select(CourseChapter.order, Chapter, ChapterActivity.order, Activity)
        .join(Chapter, Chapter.id == CourseChapter.chapter_id)
        .join(ChapterActivity, ChapterActivity.chapter_id == Chapter.id)
        .join(Activity, Activity.id == ChapterActivity.activity_id)
        .where(CourseChapter.course_id == course_id, Activity.course_id == course_id)
        .order_by(CourseChapter.order, Chapter.id, ChapterActivity.order, Activity.id)
    )).all()
    out: list[tuple] = []
    chapter_numbers: dict[int, int] = {}
    activity_counter: dict[int, int] = {}
    for _corder, chapter, _aorder, activity in rows:
        # Numbered by position, not by the stored order value, which can have
        # gaps or duplicates after edits.
        num = chapter_numbers.setdefault(chapter.id, len(chapter_numbers) + 1)
        activity_counter[chapter.id] = activity_counter.get(chapter.id, 0) + 1
        out.append((num, chapter, activity_counter[chapter.id], activity))
    return out


def _attempts(events: Iterable[UserAuditEvent], zone: ZoneInfo) -> tuple[list[dict], datetime | None]:
    """Attempt history from audit rows (oldest first). The latest GRADED row per
    attempt wins (a teacher regrade replaces the earlier grade).

    First passed is worked out from each attempt's FINAL grade, so it can never
    contradict the attempt results: the earliest attempt whose final grade passed,
    dated when that attempt started passing without a later failing grade in
    between (a pass->fail regrade clears it; a pass->pass regrade keeps the
    original date; a fail->pass regrade dates from the regrade)."""
    by_attempt: dict[int, dict] = {}
    passing_since: dict[int, datetime | None] = {}
    for ev in sorted(events, key=lambda e: (parse_timestamp(e.created_at), e.id or 0)):
        meta = ev.audit_metadata or {}
        try:
            n = int(meta.get("attempt_number") or 1)
        except (TypeError, ValueError):
            n = 1
        at = to_display(ev.created_at, zone)
        a = by_attempt.setdefault(n, {
            "attempt_number": n, "submitted_at": None, "graded_at": None,
            "score_pct": None, "passed": None,
        })
        if ev.event_type == UserAuditEventType.ASSIGNMENT_SUBMITTED:
            a["submitted_at"] = a["submitted_at"] or at
        else:
            pct = meta.get("percentage")
            a["score_pct"] = round(float(pct), 1) if pct is not None else None
            a["passed"] = bool(meta.get("passed")) if meta.get("passed") is not None else None
            a["graded_at"] = at
            passing_since[n] = (passing_since.get(n) or at) if a["passed"] else None
    attempts = [by_attempt[k] for k in sorted(by_attempt)]
    first_passed = next(
        (passing_since[a["attempt_number"]] for a in attempts
         if passing_since.get(a["attempt_number"])),
        None,
    )
    return attempts, first_passed


async def build_completion_report(
    db: AsyncSession, *, user_id: int, org_id: int, course: Course
) -> dict | None:
    """Completion report for one student in one course, or None when the student
    is not enrolled. Caller enforces permissions and that ``course`` is in
    ``org_id``. Datetimes in the result are aware, in the display zone."""
    zone = display_zone()
    user = (await db.execute(select(User).where(User.id == user_id))).scalars().first()
    run = (await db.execute(select(TrailRun).where(
        TrailRun.user_id == user_id, TrailRun.course_id == course.id,
    ))).scalars().first()
    if not user or not run:
        return None

    steps = {
        s.activity_id: s for s in (await db.execute(select(TrailStep).where(
            TrailStep.user_id == user_id, TrailStep.course_id == course.id,
        ))).scalars().all()
    }
    structure = await _course_activities(db, course.id)

    assignments = {
        a.activity_id: a for a in (await db.execute(
            select(Assignment).where(Assignment.course_id == course.id)
        )).scalars().all()
    }
    uuid_targets = [a.assignment_uuid for a in assignments.values()] + [
        act.activity_uuid for *_x, act in structure
    ] + [course.course_uuid]
    events = (await db.execute(select(UserAuditEvent).where(
        UserAuditEvent.user_id == user_id,
        UserAuditEvent.org_id == org_id,
        UserAuditEvent.target_uuid.in_(uuid_targets),  # type: ignore[union-attr]
        UserAuditEvent.event_type.in_([  # type: ignore[attr-defined]
            UserAuditEventType.ASSIGNMENT_SUBMITTED,
            UserAuditEventType.ASSIGNMENT_GRADED,
            UserAuditEventType.ACTIVITY_COMPLETED,
            UserAuditEventType.COURSE_COMPLETED,
        ]),
    ))).scalars().all()
    by_target: dict[str, list[UserAuditEvent]] = {}
    for ev in events:
        by_target.setdefault(ev.target_uuid or "", []).append(ev)

    def latest(target: str, event_type: str) -> datetime | None:
        times = [parse_timestamp(e.created_at) for e in by_target.get(target, []) if e.event_type == event_type]
        times = [t for t in times if t]
        return max(times).astimezone(zone) if times else None

    rows = []
    for chapter_number, chapter, activity_order, activity in structure:
        step = steps.get(activity.id)
        completed = bool(step and step.complete)
        if not activity.published and not completed:
            continue
        atype = activity.activity_type.value if hasattr(activity.activity_type, "value") else str(activity.activity_type)
        row = {
            "chapter_number": chapter_number,
            "chapter_name": chapter.name,
            "activity_order": activity_order,
            "activity_name": activity.name,
            "activity_uuid": activity.activity_uuid,
            "activity_type": _TYPE_LABELS.get(atype, atype),
            "published": bool(activity.published),
            "completed": completed,
            "completed_at": to_display(step.update_date or step.creation_date, zone) if completed else None,
            "audit_completed_at": latest(activity.activity_uuid, UserAuditEventType.ACTIVITY_COMPLETED),
            "is_assignment": activity.id in assignments,
            "attempts": [],
            "first_passed_at": None,
        }
        assignment = assignments.get(activity.id)
        if assignment:
            attempt_events = [
                e for e in by_target.get(assignment.assignment_uuid, [])
                if e.event_type in (UserAuditEventType.ASSIGNMENT_SUBMITTED, UserAuditEventType.ASSIGNMENT_GRADED)
            ]
            row["attempts"], row["first_passed_at"] = _attempts(attempt_events, zone)
        rows.append(row)

    course_done = run.status == StatusEnum.STATUS_COMPLETED
    cert = (await db.execute(
        select(CertificateUser.created_at)
        .join(Certifications, Certifications.id == CertificateUser.certification_id)
        .where(CertificateUser.user_id == user_id, Certifications.course_id == course.id)
    )).scalars().first()

    return {
        "schema": "completion_report/1",
        "timezone": zone.key,
        "generated_at": datetime.now(UTC).astimezone(zone),
        "student": {
            "id": user.id,
            "name": f"{user.first_name or ''} {user.last_name or ''}".strip() or user.username,
            "username": user.username,
            "email": user.email,
        },
        "course": {"id": course.id, "uuid": course.course_uuid, "name": course.name},
        "enrolled_at": to_display(run.creation_date, zone),
        # Only while the run is COMPLETED: a retry demotes the run, and a stale
        # COURSE_COMPLETED audit row must not make the course look finished.
        "course_completed_at": to_display(run.update_date, zone) if course_done else None,
        "audit_course_completed_at": latest(course.course_uuid, UserAuditEventType.COURSE_COMPLETED) if course_done else None,
        "certificate_issued_at": to_display(cert, zone),
        "rows": rows,
    }


async def enrolled_user_ids(db: AsyncSession, course_id: int) -> list[int]:
    return list((await db.execute(
        select(TrailRun.user_id).where(TrailRun.course_id == course_id).order_by(TrailRun.user_id)
    )).scalars().all())


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------
def report_to_json(report: dict) -> dict:
    """Same data, datetimes as ISO 8601 with offset (unambiguous across DST)."""
    def conv(v):
        if isinstance(v, datetime):
            return _fmt_iso(v)
        if isinstance(v, dict):
            return {k: conv(x) for k, x in v.items()}
        if isinstance(v, list):
            return [conv(x) for x in v]
        return v
    return conv(report)


def _yes_no(v: bool | None) -> str:
    return "" if v is None else ("yes" if v else "no")


def _attempt_text(attempts: list[dict]) -> str:
    parts = []
    for a in attempts:
        score = f"{a['score_pct']:g}%" if a["score_pct"] is not None else "not graded"
        verdict = {True: "pass", False: "fail", None: ""}[a["passed"]]
        parts.append(f"#{a['attempt_number']} {score} {verdict}".strip())
    return "; ".join(parts)


def table_columns(zone_name: str) -> list[str]:
    return [
        "Chapter #", "Chapter", "Activity #", "Activity", "Type", "Published",
        "Completed", f"Completed at ({zone_name})",
        "Attempts", "Attempt results", f"First passed at ({zone_name})",
    ]


def _table_cells(row: dict) -> list:
    return [
        row["chapter_number"], row["chapter_name"], row["activity_order"], row["activity_name"],
        row["activity_type"], _yes_no(row["published"]), _yes_no(row["completed"]),
        _fmt_local(row["completed_at"]),
        len(row["attempts"]) if row["is_assignment"] else "",
        _attempt_text(row["attempts"]),
        _fmt_local(row["first_passed_at"]),
    ]


def _header_pairs(report: dict) -> list[tuple[str, str]]:
    return [
        ("Student", report["student"]["name"]),
        ("Email", report["student"]["email"]),
        ("Course", report["course"]["name"]),
        ("Enrolled at", _fmt_local(report["enrolled_at"])),
        ("Course completed at", _fmt_local(report["course_completed_at"])),
        ("Certificate issued at", _fmt_local(report["certificate_issued_at"])),
        ("Report generated at", _fmt_local(report["generated_at"])),
        ("Time zone", report["timezone"]),
    ]


def _write(rows: Iterable[list]) -> str:
    buf = io.StringIO()
    writer = csv.writer(buf)
    for r in rows:
        # Formula-injection guard on every cell, names included: activity and
        # chapter names are editable by course authors.
        writer.writerow([_csv_safe(c) for c in r])
    return buf.getvalue()


def render_student_csv(report: dict) -> str:
    """Header block, blank line, then the activity table."""
    lines: list[list] = [["Completion report"]]
    lines += [[k, v] for k, v in _header_pairs(report)]
    lines.append([])
    lines.append(table_columns(report["timezone"]))
    lines += [_table_cells(r) for r in report["rows"]]
    return _write(lines)


def bulk_csv_header(zone_name: str) -> str:
    return _write([["Student", "Student email"] + table_columns(zone_name)])


def bulk_csv_rows(report: dict) -> str:
    s = report["student"]
    return _write([[s["name"], s["email"]] + _table_cells(r) for r in report["rows"]])


def render_student_html(report: dict) -> str:
    """Printable view: static HTML, every value escaped, no scripts."""
    e = lambda v: html.escape("" if v is None else str(v))
    head = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in _header_pairs(report))
    cols = "".join(f"<th>{e(c)}</th>" for c in table_columns(report["timezone"]))
    body = "".join(
        "<tr>" + "".join(f"<td>{e(c)}</td>" for c in _table_cells(r)) + "</tr>"
        for r in report["rows"]
    )
    title = f"Completion report: {report['student']['name']}, {report['course']['name']}"
    return (
        "<!doctype html><html><head><meta charset='utf-8'>"
        f"<title>{e(title)}</title><style>"
        "body{font:12px/1.4 system-ui,sans-serif;margin:24px;color:#111}"
        "table{border-collapse:collapse;margin-bottom:16px}"
        "th,td{border:1px solid #999;padding:4px 6px;text-align:left;vertical-align:top}"
        ".meta th{background:#f3f3f3}"
        "@media print{body{margin:0}}"
        f"</style></head><body><h1>{e(title)}</h1>"
        f"<table class='meta'>{head}</table>"
        f"<table><thead><tr>{cols}</tr></thead><tbody>{body}</tbody></table>"
        "</body></html>"
    )
