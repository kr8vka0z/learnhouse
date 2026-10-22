"""Rescue Ready fork: keep a student's official record from being erased by an
account deletion.

``user_audit_event``, ``trailrun``, ``trailstep``, ``assignmentusersubmission``
and ``certificateuser`` all reference ``user.id`` (and most ``organization.id``)
with ``ON DELETE CASCADE``, so a hard delete of the user, or of an org, silently
wipes the completion record. Rather than change those foreign keys (a migration
against upstream's schema), the one hard-delete path refuses while records
exist. Removing someone from the organization (membership only, nothing
cascades) and anonymizing them (admin API; keeps the rows, scrubs the PII) stay
available.

Only LEARNING records count (owner decision): enrollments, progress,
submissions, certificates, and audit rows other than sign-in/sign-out. An
account that only ever signed in (staff, or a student who never started) can
still be deleted, taking its connection log with it.
"""
from fastapi import HTTPException
from sqlmodel import func, select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.db.courses.assignments import AssignmentUserSubmission
from src.db.courses.certifications import CertificateUser
from src.db.trail_runs import TrailRun
from src.db.trail_steps import TrailStep
from src.db.user_audit_events import UserAuditEvent, UserAuditEventType
from src.db.user_organizations import UserOrganization
from src.security.rbac.constants import ADMIN_ROLE_ID

_USER_RECORD_TABLES = (TrailRun, TrailStep, AssignmentUserSubmission, CertificateUser, UserAuditEvent)
# Every row an org's student records live in carries org_id on these two.
_ORG_RECORD_TABLES = (TrailRun, UserAuditEvent)
_CONNECTION_EVENTS = (UserAuditEventType.LOGIN, UserAuditEventType.LOGOUT)


async def _any(db_session: AsyncSession, table, condition) -> bool:
    stmt = select(table.id).where(condition)
    if table is UserAuditEvent:
        stmt = stmt.where(UserAuditEvent.event_type.not_in(_CONNECTION_EVENTS))  # type: ignore[attr-defined]
    return (await db_session.execute(stmt.limit(1))).first() is not None


async def user_has_records(db_session: AsyncSession, user_id: int) -> bool:
    for table in _USER_RECORD_TABLES:
        if await _any(db_session, table, table.user_id == user_id):
            return True
    return False


async def org_has_records(db_session: AsyncSession, org_id: int) -> bool:
    for table in _ORG_RECORD_TABLES:
        if await _any(db_session, table, table.org_id == org_id):
            return True
    return False


async def ensure_user_deletion_keeps_records(db_session: AsyncSession, user_id: int) -> None:
    """Raise 409 if deleting ``user_id`` would cascade away student records.

    Covers both the user's own records and those of any organization the
    account deletion would auto-delete (orgs where this user is the sole admin);
    otherwise an admin deleting their own account would erase every student.
    """
    if await user_has_records(db_session, user_id):
        raise HTTPException(
            status_code=409,
            detail=(
                "This account has course enrollment or completion records, which are kept "
                "as the official record. Remove the user from the organization or "
                "anonymize them instead of deleting the account."
            ),
        )

    admin_org_ids = (await db_session.execute(
        select(UserOrganization.org_id).where(
            UserOrganization.user_id == user_id,
            UserOrganization.role_id == ADMIN_ROLE_ID,
        )
    )).scalars().all()
    for org_id in admin_org_ids:
        other_admins = (await db_session.execute(
            select(func.count()).select_from(UserOrganization).where(
                UserOrganization.org_id == org_id,
                UserOrganization.role_id == ADMIN_ROLE_ID,
                UserOrganization.user_id != user_id,
            )
        )).scalar_one()
        if not other_admins and await org_has_records(db_session, org_id):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Deleting this account would delete an organization whose student "
                    "records must be kept. Add another admin to the organization first."
                ),
            )
