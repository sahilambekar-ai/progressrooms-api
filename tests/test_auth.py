import pytest
from fastapi import HTTPException
from app.modules.auth.service import AuthService
from app.core.security import decode_access_token

@pytest.mark.asyncio
async def test_request_and_verify_otp(db_session):
    email = "test.teacher@progressrooms.local"

    otp_code, record_id, is_verified, requires_activation, msg = await AuthService.request_otp(db_session, email)
    assert len(otp_code) == 6

    # Verify OTP
    result = await AuthService.verify_otp(db_session, email, otp_code, full_name="Test Teacher")
    assert result is not None
    token, user = result
    assert user.email == email
    assert user.full_name == "Test Teacher"

    payload = decode_access_token(token)
    assert payload is not None
    assert payload["email"] == email

@pytest.mark.asyncio
async def test_invalid_otp_fails(db_session):
    email = "test.fail@progressrooms.local"
    await AuthService.request_otp(db_session, email)

    with pytest.raises(HTTPException):
        await AuthService.verify_otp(db_session, email, "000000")

