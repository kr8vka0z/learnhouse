"""Rescue Ready fork: refuse org-less account creation on an invite-only single-org deployment.

On a single-org deployment an org-less account is a way into the one org.
Anyone could pre-register a future student's address with their own password
(auto-verified outside SaaS); once staff invite that address, the student lands
in the pre-made account and the registrant keeps password access to it. So every
path that creates an account with no org (POST /users/, Google sign-in without
an org) closes whenever an org is invite-only, matching the org-scoped signup's
refusal.
"""

from fastapi import HTTPException, Request
from sqlmodel import select
from sqlmodel.ext.asyncio.session import AsyncSession

from src.core import ee_hooks
from src.db.organizations import Organization
from src.services.orgs.orgs import get_org_join_mechanism


async def enforce_orgless_signup_allowed(
    request: Request, db_session: AsyncSession, current_user
) -> None:
    # Multi-org: an org-less account is a platform account that still needs an
    # invite to enter any invite-only org, so upstream behaviour stands.
    # Looked up on the module so tests can patch it.
    if ee_hooks.is_multi_org_allowed():
        return
    # Every org, not just the default: OSS should have one, and checking all of
    # them costs nothing if a stray second row exists.
    org_ids = (await db_session.execute(select(Organization.id))).scalars().all()
    for org_id in org_ids:
        try:
            mechanism = await get_org_join_mechanism(
                request, org_id, current_user, db_session
            )
        except HTTPException as exc:
            # No config row: upstream's default signup_mode is "open", and the
            # org-scoped route can't admit anyone to such an org either.
            if exc.status_code == 404:
                continue
            raise
        if mechanism == "inviteOnly":
            raise HTTPException(
                status_code=403,
                detail="You need an invite to join this organization",
            )
