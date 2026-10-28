import logging
from typing import Optional, Dict, Any
import httpx
from app.core.config import settings
from app.common.email_content import email_text, email_subject, first_name as recipient_first_name

logger = logging.getLogger("progressrooms.email")
BREVO_API_URL = "https://api.brevo.com/v3/smtp/email"

def _generate_otp_email_html(otp_code: str, recipient_name: str, purpose_display: str) -> str:
    """
    Renders an elegant, responsive HTML email template styled with
    the Progress Rooms nature studio aesthetic.
    """
    first_name = email_text(recipient_first_name(recipient_name))
    otp_code = email_text(otp_code)
    purpose_display = email_text(purpose_display)
    
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>Your Verification Passcode - Progress Rooms</title>
  <style>
    body {{
      margin: 0;
      padding: 0;
      background-color: #f4f7f4;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #1A2E22;
      -webkit-font-smoothing: antialiased;
    }}
    .wrapper {{
      width: 100%;
      background-color: #f4f7f4;
      padding: 40px 15px;
      box-sizing: border-box;
    }}
    .container {{
      max-width: 560px;
      margin: 0 auto;
      background-color: #ffffff;
      border-radius: 20px;
      overflow: hidden;
      box-shadow: 0 10px 30px rgba(12, 36, 23, 0.08);
      border: 1px solid #CEDECF;
    }}
    .header {{
      background: linear-gradient(135deg, #0C2417 0%, #133623 60%, #1b4332 100%);
      padding: 36px 30px;
      text-align: center;
      color: #ffffff;
    }}
    .logo-badge {{
      display: inline-block;
      font-size: 32px;
      margin-bottom: 8px;
    }}
    .header-title {{
      margin: 0;
      font-size: 22px;
      font-weight: 700;
      letter-spacing: 0.5px;
      color: #F4F7F4;
      font-family: Georgia, serif;
    }}
    .header-subtitle {{
      margin: 6px 0 0 0;
      font-size: 11px;
      color: #81A882;
      text-transform: uppercase;
      letter-spacing: 2px;
      font-weight: 600;
    }}
    .content {{
      padding: 36px 32px;
    }}
    .greeting {{
      font-size: 18px;
      font-weight: 700;
      color: #0C2417;
      margin-bottom: 12px;
    }}
    .message {{
      font-size: 15px;
      line-height: 1.6;
      color: #40514E;
      margin-bottom: 24px;
    }}
    .otp-box {{
      background: #E8F0E9;
      border: 2px dashed #81A882;
      border-radius: 14px;
      padding: 24px 20px;
      text-align: center;
      margin: 28px 0;
    }}
    .otp-label {{
      font-size: 11px;
      font-weight: 700;
      text-transform: uppercase;
      letter-spacing: 1.5px;
      color: #1b4332;
      margin-bottom: 8px;
    }}
    .otp-digits {{
      font-family: 'Courier New', Courier, monospace;
      font-size: 38px;
      font-weight: 700;
      letter-spacing: 8px;
      color: #0C2417;
      margin: 0;
      padding-left: 8px;
    }}
    .otp-expiry {{
      font-size: 12px;
      color: #2D7A58;
      margin-top: 10px;
      font-weight: 500;
    }}
    .security-callout {{
      background-color: #F4F7F4;
      border-left: 4px solid #81A882;
      padding: 14px 18px;
      border-radius: 0 10px 10px 0;
      margin-bottom: 24px;
      font-size: 13px;
      color: #40514E;
      line-height: 1.5;
    }}
    .footer {{
      background-color: #E8F0E9;
      padding: 24px 30px;
      text-align: center;
      font-size: 12px;
      color: #40514E;
      border-top: 1px solid #CEDECF;
    }}
    .footer p {{
      margin: 4px 0;
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="container">
      <div class="header">
        <div class="logo-badge">🌿</div>
        <h1 class="header-title">Progress Rooms</h1>
        <div class="header-subtitle">Virtual Studio</div>
      </div>
      
      <div class="content">
        <div class="greeting">Hello {first_name},</div>
        <p class="message">
          Please use the secure one-time passcode below to complete your {purpose_display.lower()}:
        </p>

        <div class="otp-box">
          <div class="otp-label">Your One-Time Passcode</div>
          <div class="otp-digits">{otp_code}</div>
          <div class="otp-expiry">⏱ This passcode is valid for <strong>10 minutes</strong>.</div>
        </div>

        <div class="security-callout">
          <strong>Security Note:</strong> Do not share this OTP with anyone. Progress Rooms instructors and staff will never ask for your one-time code.
        </div>

        <p class="message" style="margin-bottom: 0; font-size: 13px; color: #618D63;">
          If you did not initiate this request, you can safely ignore this email.
        </p>
      </div>

      <div class="footer">
        <p><strong>Progress Rooms Virtual Studio OS</strong></p>
        <p>Independent Teacher Sanctuaries & Scheduled Batch Classrooms</p>
        <p style="margin-top: 8px; font-size: 11px; color: #81A882;">
          © 2026 Progress Rooms. All rights reserved.
        </p>
      </div>
    </div>
  </div>
</body>
</html>"""


async def send_otp_email(
    to_email: str,
    to_name: str,
    otp_code: str,
    purpose: str = "LOGIN_VERIFICATION"
) -> Dict[str, Any]:
    """
    Dispatches a transactional OTP verification email via Brevo REST API v3 using httpx.
    """
    if not getattr(settings, "BREVO_ENABLED", True):
        logger.info("[Brevo Disabled] Verification email was not sent")
        return {"success": False, "error": "Brevo is disabled in configuration"}

    api_key = getattr(settings, "BREVO_API_KEY", "")
    if not api_key:
        logger.warning("[Brevo Missing Key] Verification email was not sent")
        return {"success": False, "error": "BREVO_API_KEY is not set"}

    purpose_labels = {
        "EMAIL_VERIFICATION": "Email Verification",
        "PASSWORD_RESET": "Password Reset",
        "LOGIN_VERIFICATION": "Login Verification"
    }
    purpose_display = purpose_labels.get(purpose, "Verification")
    subject = email_subject(f"🌿 Your Progress Rooms Code ({otp_code})")
    html_content = _generate_otp_email_html(otp_code, to_name, purpose_display)

    headers = {
        "api-key": api_key,
        "accept": "application/json",
        "content-type": "application/json"
    }

    payload = {
        "sender": {
            "name": getattr(settings, "BREVO_SENDER_NAME", "Progress Rooms"),
            "email": getattr(settings, "BREVO_SENDER_EMAIL", "sahilambekar.dev@gmail.com")
        },
        "to": [
            {
                "email": to_email,
                "name": to_name.strip() if to_name else to_email
            }
        ],
        "subject": subject,
        "htmlContent": html_content
    }

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            response = await client.post(BREVO_API_URL, headers=headers, json=payload)

        if response.status_code in [200, 201]:
            resp_data = response.json() if response.content else {}
            message_id = resp_data.get("messageId", "ok")
            logger.info("[Brevo Success] Verification email sent")
            return {"success": True, "message_id": message_id}
        elif response.status_code == 401:
            logger.warning("[Brevo 401] Verification email authorization failed")
            return {"success": False, "error": "IP_NOT_AUTHORIZED"}
        else:
            logger.error("[Brevo Error] Verification email failed with HTTP %s", response.status_code)
            return {"success": False, "error": f"HTTP_{response.status_code}", "details": response.text}
    except Exception as e:
        logger.error("[Brevo Exception] Verification email delivery failed")
        return {"success": False, "error": "EXCEPTION", "details": str(e)}
