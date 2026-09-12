import pytest
import uuid
from sqlalchemy import select
from app.models.organization import Organization, OrganizationMember, OrganizationSetting
from app.models.integration import OrganizationIntegration
from app.models.user import User
from app.modules.auth.service import AuthService
from app.modules.organizations.router import (
    get_studio_profile,
    update_studio_profile,
    connect_zoom,
    disconnect_zoom,
    generate_zoom_meeting,
    StudioProfileUpdate,
    ZoomConnectRequest,
    ZoomMeetingGenerateRequest
)

@pytest.mark.asyncio
async def test_studio_registration_and_profile_flow(db_session):
    # 1. Register a new studio user
    email = "newstudio.owner@yogastudio.test"
    otp_code, rec_id, msg = await AuthService.register_user(
        db=db_session,
        full_name="Maya Sharma",
        email=email,
        password="securepass123",
        phone="+91 98765 11223",
        role="STUDIO",
        studio_name="Maya Sanctuary"
    )

    # 2. Verify OTP
    token, user = await AuthService.verify_otp(
        db=db_session,
        email=email,
        otp_code=otp_code,
        full_name="Maya Sharma"
    )

    # 3. Find created organization and member
    stmt = select(OrganizationMember, Organization).join(
        Organization, Organization.id == OrganizationMember.organization_id
    ).where(OrganizationMember.user_id == user.id)
    res = await db_session.execute(stmt)
    mem_row = res.first()
    assert mem_row is not None
    member, org = mem_row

    # 4. Check initial OrganizationSetting
    set_stmt = select(OrganizationSetting).where(OrganizationSetting.organization_id == org.id)
    settings = (await db_session.execute(set_stmt)).scalar_one_or_none()
    assert settings is not None
    assert settings.account_completed is False
    assert settings.completion_step == 1
    assert settings.phone == "+91 98765 11223"

    # 5. Fetch profile via endpoint helper
    profile = await get_studio_profile(
        org_id=org.id,
        tenant=(org, member),
        db=db_session
    )
    assert profile["name"] == "Maya Sanctuary"
    assert profile["account_completed"] is False
    assert profile["zoom_connected"] is False

    # 6. Update Profile with Location, GST, Bank Details
    update_payload = StudioProfileUpdate(
        phone="+91 98765 11223",
        whatsapp_number="+91 98765 11223",
        studio_tagline="Sacred Movement & Aerial Flow",
        disciplines="Hatha, Aerial, Vinyasa, Pranayama",
        teaching_mode="HYBRID",
        address_line1="108 Lotus Pavilion, Koregaon Park",
        city="Pune",
        state="Maharashtra",
        pincode="411001",
        country="India",
        has_gst=True,
        gst_number="27AABCU9603R1ZM",
        legal_business_name="Maya Wellness Enterprises",
        bank_name="HDFC Bank",
        account_holder_name="Maya Sharma",
        account_number="50100234567890",
        ifsc_code="HDFC0001234",
        upi_id="mayashala@okhdfcbank",
        completion_step=4
    )
    up_res = await update_studio_profile(
        org_id=org.id,
        payload=update_payload,
        tenant=(org, member),
        current_user=user,
        db=db_session
    )
    assert up_res["status"] == "success"
    assert up_res["completion_percentage"] > 0
    assert user.phone == "+91 98765 11223"

    # 7. Connect Zoom
    zoom_req = ZoomConnectRequest(
        account_email="maya.zoom@shala.org",
        auto_meeting_enabled=True,
        waiting_room=True,
        host_video=True
    )
    zoom_res = await connect_zoom(
        org_id=org.id,
        payload=zoom_req,
        tenant=(org, member),
        current_user=user,
        db=db_session
    )
    assert zoom_res["status"] == "connected"
    assert zoom_res["account_email"] == "maya.zoom@shala.org"

    # 8. Mark Account as Completed (Step 6 Complete)
    complete_payload = StudioProfileUpdate(
        account_completed=True,
        completion_step=6
    )
    final_res = await update_studio_profile(
        org_id=org.id,
        payload=complete_payload,
        tenant=(org, member),
        current_user=user,
        db=db_session
    )
    assert final_res["account_completed"] is True
    assert final_res["completion_percentage"] == 100
    assert final_res["zoom_connected"] is True

    # 9. Verify generated Zoom meeting
    meet_req = ZoomMeetingGenerateRequest(
        topic="Sunrise Vinyasa Flow",
        duration_minutes=60
    )
    meet_res = await generate_zoom_meeting(
        org_id=org.id,
        payload=meet_req,
        tenant=(org, member),
        db=db_session
    )
    assert meet_res["provider"] == "ZOOM"
    assert "us05web.zoom.us/j/" in meet_res["join_url"]
    assert len(meet_res["passcode"]) >= 6

    # 10. Verify Profiler data
    final_profile = await get_studio_profile(
        org_id=org.id,
        tenant=(org, member),
        db=db_session
    )
    assert final_profile["account_completed"] is True
    assert final_profile["gst_number"] == "27AABCU9603R1ZM"
    assert final_profile["account_number_masked"].endswith("7890")
    assert final_profile["upi_id"] == "mayashala@okhdfcbank"
    assert final_profile["zoom_connected"] is True
