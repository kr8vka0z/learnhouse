"""Rescue Ready fork: Google sign-in with no org honours invite-only for NEW accounts.

Same rule as anonymous POST /users/: when multi-org is off and the org is
invite-only, a Google sign-in without an org must not mint a new account.
Someone who already has an account still signs in.
"""

from datetime import datetime
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import HTTPException
from sqlmodel import select
from starlette.requests import Request

from src.db.organization_config import OrganizationConfig
from src.db.users import AnonymousUser, User
from src.services.auth.utils import signWithGoogle

EMAIL = "newcomer@test.com"


def _request():
    return Request({"type": "http", "method": "POST", "headers": [], "client": ("127.0.0.1", 0)})


async def _set_signup_mode(db, org, mode: str):
    db.add(
        OrganizationConfig(
            org_id=org.id,
            config={
                "config_version": "2.0",
                "admin_toggles": {"members": {"signup_mode": mode}},
            },
            creation_date=str(datetime.now()),
            update_date=str(datetime.now()),
        )
    )
    await db.commit()


async def _sign_in(db, email=EMAIL, multi_org=False):
    payload = {"email": email, "email_verified": True, "given_name": "New", "family_name": "Comer"}
    with patch(
        "src.services.auth.utils.get_google_user_info", new=AsyncMock(return_value=payload)
    ), patch("src.services.auth.utils.update_login_info", return_value=None), patch(
        "src.core.ee_hooks.is_multi_org_allowed", return_value=multi_org
    ):
        return await signWithGoogle(
            _request(), access_token="t", email=email, org_id=None,
            current_user=AnonymousUser(), db_session=db,
        )


async def _user(db, email=EMAIL):
    return (await db.execute(select(User).where(User.email == email))).scalars().first()


async def test_new_account_refused_when_single_org_is_invite_only(db, org):
    await _set_signup_mode(db, org, "inviteOnly")

    with pytest.raises(HTTPException) as exc:
        await _sign_in(db)

    assert exc.value.status_code == 403
    assert await _user(db) is None


async def test_existing_account_still_signs_in_when_invite_only(db, org, regular_user):
    await _set_signup_mode(db, org, "inviteOnly")

    user = await _sign_in(db, email=regular_user.email)

    assert user.id == regular_user.id


async def test_new_account_created_when_single_org_is_open(db, org):
    await _set_signup_mode(db, org, "open")

    await _sign_in(db)

    assert await _user(db) is not None


async def test_multi_org_keeps_upstream_behaviour(db, org):
    await _set_signup_mode(db, org, "inviteOnly")

    await _sign_in(db, multi_org=True)

    assert await _user(db) is not None
