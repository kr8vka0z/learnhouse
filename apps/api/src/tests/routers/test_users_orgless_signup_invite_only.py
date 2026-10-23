"""Rescue Ready fork: anonymous org-less signup (POST /users/) honours invite-only.

On a single-organization deployment an org-less account is a way into the one
org: an attacker could register a future student's address with a password of
their choosing (auto-verified outside SaaS), and the real student, once
invited, would end up signed in to that pre-made account. So when multi-org is
off and the org is invite-only, the org-less route must refuse like the
org-scoped one does.
"""

from datetime import datetime
from unittest.mock import patch

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from sqlmodel import select

from src.core.events.database import get_db_session
from src.db.organization_config import OrganizationConfig
from src.db.users import User
from src.routers.users import router as users_router
from src.security.auth import get_authenticated_user, get_current_user

EMAIL = "future.student@test.com"
BODY = {
    "username": "prereg",
    "first_name": "Pre",
    "last_name": "Reg",
    "email": EMAIL,
    "password": "Password123!",
}


@pytest.fixture
def app(db, anonymous_user):
    app = FastAPI()
    app.include_router(users_router, prefix="/api/v1/users")
    app.dependency_overrides[get_db_session] = lambda: db
    app.dependency_overrides[get_current_user] = lambda: anonymous_user
    app.dependency_overrides[get_authenticated_user] = lambda: anonymous_user
    yield app
    app.dependency_overrides.clear()


@pytest.fixture
async def client(app):
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as c:
        yield c


@pytest.fixture(autouse=True)
def _no_signup_rate_limit():
    with patch(
        "src.routers.users.check_signup_rate_limit", return_value=(True, 0)
    ):
        yield


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


async def _user_exists(db) -> bool:
    found = (await db.execute(select(User).where(User.email == EMAIL))).scalars().first()
    return found is not None


async def test_refused_when_single_org_is_invite_only(client, db, org):
    await _set_signup_mode(db, org, "inviteOnly")

    with patch("src.core.ee_hooks.is_multi_org_allowed", return_value=False):
        response = await client.post("/api/v1/users/", json=BODY)

    assert response.status_code == 403
    assert not await _user_exists(db)


async def test_allowed_when_single_org_is_open(client, db, org):
    await _set_signup_mode(db, org, "open")

    with patch("src.core.ee_hooks.is_multi_org_allowed", return_value=False):
        response = await client.post("/api/v1/users/", json=BODY)

    assert response.status_code == 200
    assert await _user_exists(db)


async def test_allowed_on_fresh_install_with_no_org(client, db):
    with patch("src.core.ee_hooks.is_multi_org_allowed", return_value=False):
        response = await client.post("/api/v1/users/", json=BODY)

    assert response.status_code == 200
    assert await _user_exists(db)


async def test_multi_org_deployments_keep_upstream_behaviour(client, db, org):
    # With multi-org on, an org-less account is a platform account, not a way
    # into any one org, so one invite-only org must not close platform signup.
    await _set_signup_mode(db, org, "inviteOnly")

    with patch("src.core.ee_hooks.is_multi_org_allowed", return_value=True):
        response = await client.post("/api/v1/users/", json=BODY)

    assert response.status_code == 200
    assert await _user_exists(db)
