"""Rescue Ready fork: white-label org email on the open-source edition.

Renders a real org invitation through the same branding resolution the invite
path uses, and checks the "Powered by LearnHouse" footer follows the org's
watermark toggle instead of being forced on.
"""

from types import SimpleNamespace
from unittest.mock import patch

import src.services.users.emails as emails
from src.services.email.branding import POWERED_BY_URL, resolve_org_email_branding


def _render_invitation(watermark):
    general = {"color": "#0B1F5C", "email_sender_name": "Sample School"}
    if watermark is not None:
        general["watermark"] = watermark
    org = SimpleNamespace(name="Sample School", org_uuid="org_uuid", logo_image="")
    cfg = SimpleNamespace(config={"customization": {"general": general}})
    sent = {}
    with patch("src.core.deployment_mode.get_deployment_mode", return_value="oss"), patch(
        "src.services.email.utils.get_media_base_url", return_value="https://api.test"
    ), patch.object(emails, "send_email", side_effect=lambda **kw: sent.update(kw)):
        branding = resolve_org_email_branding(org, cfg)
        emails.send_invitation_email(
            email="student@example.com",
            org_name=org.name,
            inviter_username="admin",
            signup_url="https://school.test/signup",
            invite_code="ABC123",
            **branding.as_kwargs(),
        )
    return sent


def test_watermark_off_drops_the_powered_by_footer():
    sent = _render_invitation(watermark=False)
    assert POWERED_BY_URL not in sent["body"]
    assert sent["sender_name"] == "Sample School"


def test_default_still_carries_the_powered_by_footer():
    sent = _render_invitation(watermark=None)
    assert POWERED_BY_URL in sent["body"]
