"""
Templates for ProgressRooms operational communications.
Includes:
- Nature-sanctuary styled responsive Brevo HTML email templates.
- Outbound Meta-compliant WhatsApp text templates.
- Automated system responses and opt-out acknowledgments.
"""

from typing import Dict, Any, Optional

WHATSAPP_AUTOREPLY_TEXT = (
    "This ProgressRooms number sends automated notifications. Replies are not monitored. "
    "Please open ProgressRooms for class details and available support options."
)

WHATSAPP_OPTOUT_CONFIRMATION_TEXT = (
    "You have unsubscribed from ProgressRooms operational notifications on this number. "
    "To opt back in at any time, manage communication preferences in your ProgressRooms profile."
)

# ---------------------------------------------------------------------------
# WhatsApp Outbound Templates (Classes Sender)
# ---------------------------------------------------------------------------

def render_class_reminder_2h_wa(
    studio_name: str,
    class_name: str,
    teacher_name: str,
    start_time_ist: str,
    suggested_join_time_ist: str,
    join_url: str,
    equipment_needed: Optional[str] = None
) -> str:
    eq_text = f"\nRequired Equipment: {equipment_needed}" if equipment_needed else ""
    return (
        f"🌿 *Class Reminder — 2 Hours*\n\n"
        f"Your class *{class_name}* at *{studio_name}* begins today at *{start_time_ist}*.\n\n"
        f"• Instructor: {teacher_name}\n"
        f"• Suggested Arrival: Please join 5 minutes early at *{suggested_join_time_ist}*{eq_text}\n\n"
        f"Open your secure class portal to view latest batch details and enter the classroom:\n"
        f"{join_url}"
    )


def render_class_reminder_15m_wa(
    studio_name: str,
    class_name: str,
    teacher_name: str,
    start_time_ist: str,
    suggested_join_time_ist: str,
    join_url: str
) -> str:
    return (
        f"🌿 *Starting Soon — 15 Minutes*\n\n"
        f"*{class_name}* with {teacher_name} ({studio_name}) starts in 15 minutes at *{start_time_ist}*.\n\n"
        f"Please take your seat 5 minutes early at *{suggested_join_time_ist}*.\n\n"
        f"Join your sanctuary here:\n"
        f"{join_url}"
    )


def render_class_correction_wa(
    studio_name: str,
    class_name: str,
    changes_summary: str,
    latest_start_time_ist: str,
    join_url: str
) -> str:
    return (
        f"⚠️ *Schedule Correction — {studio_name}*\n\n"
        f"Updated details for your upcoming class *{class_name}*:\n"
        f"{changes_summary}\n\n"
        f"Current Start Time: *{latest_start_time_ist}*\n"
        f"Please verify your updated schedule and join securely:\n"
        f"{join_url}"
    )


def render_class_cancellation_wa(
    studio_name: str,
    class_name: str,
    original_time_ist: str,
    reason: Optional[str] = None
) -> str:
    reason_text = f"\nReason: {reason}" if reason else ""
    return (
        f"🛑 *Class Cancelled — {studio_name}*\n\n"
        f"Please note that *{class_name}* scheduled for *{original_time_ist}* has been cancelled.{reason_text}\n\n"
        f"Any future reminders for this session have been automatically cancelled. "
        f"Please check your ProgressRooms schedule for next upcoming classes."
    )


# ---------------------------------------------------------------------------
# WhatsApp Outbound Templates (Admin Sender - Billing)
# ---------------------------------------------------------------------------

def render_billing_invoice_wa(
    studio_name: str,
    invoice_number: str,
    amount_inr: float,
    billing_period: str,
    due_date_str: str,
    invoice_url: str
) -> str:
    return (
        f"💳 *ProgressRooms Studio Subscription Invoice*\n\n"
        f"Studio: *{studio_name}*\n"
        f"Invoice: #{invoice_number}\n"
        f"Billing Period: {billing_period}\n"
        f"Amount: ₹{amount_inr:,.2f}\n"
        f"Due Date: {due_date_str}\n\n"
        f"View invoice & complete payment securely:\n"
        f"{invoice_url}"
    )


def render_billing_due_soon_wa(
    studio_name: str,
    amount_inr: float,
    due_date_str: str,
    invoice_url: str
) -> str:
    return (
        f"⏳ *Payment Due Soon — ProgressRooms*\n\n"
        f"Studio: *{studio_name}*\n"
        f"Your subscription balance of ₹{amount_inr:,.2f} is due on *{due_date_str}*.\n\n"
        f"Pay securely to maintain uninterrupted class operations:\n"
        f"{invoice_url}"
    )


def render_billing_overdue_wa(
    studio_name: str,
    amount_inr: float,
    days_overdue: int,
    invoice_url: str
) -> str:
    return (
        f"⚠️ *Overdue Notice — ProgressRooms*\n\n"
        f"Studio: *{studio_name}*\n"
        f"Your subscription payment of ₹{amount_inr:,.2f} is *{days_overdue} days overdue*.\n\n"
        f"Please complete payment immediately to avoid service restrictions on your studio sanctuary:\n"
        f"{invoice_url}"
    )


def render_billing_payment_received_wa(
    studio_name: str,
    amount_inr: float,
    receipt_url: str
) -> str:
    return (
        f"✅ *Payment Received — ProgressRooms*\n\n"
        f"Studio: *{studio_name}*\n"
        f"We have received your subscription payment of ₹{amount_inr:,.2f}. Thank you!\n\n"
        f"Your studio sanctuary is in good standing. View receipt:\n"
        f"{receipt_url}"
    )


# ---------------------------------------------------------------------------
# Brevo Responsive HTML Email Templates (Nature Theme)
# ---------------------------------------------------------------------------

def _email_wrapper(title: str, content_html: str) -> str:
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width, initial-scale=1.0">
  <title>{title}</title>
  <style>
    body {{
      margin: 0; padding: 0;
      background-color: #f4f7f4;
      font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif;
      color: #1A2E22;
      -webkit-font-smoothing: antialiased;
    }}
    .wrapper {{ width: 100%; background-color: #f4f7f4; padding: 40px 15px; box-sizing: border-box; }}
    .container {{
      max-width: 580px; margin: 0 auto;
      background-color: #ffffff;
      border-radius: 20px; overflow: hidden;
      box-shadow: 0 10px 30px rgba(12, 36, 23, 0.08);
      border: 1px solid #CEDECF;
    }}
    .header {{
      background: linear-gradient(135deg, #0C2417 0%, #133623 60%, #1b4332 100%);
      padding: 32px 30px; text-align: center; color: #ffffff;
    }}
    .header-title {{
      margin: 0; font-size: 22px; font-weight: 700;
      letter-spacing: 0.5px; color: #F4F7F4; font-family: Georgia, serif;
    }}
    .header-subtitle {{
      margin: 6px 0 0 0; font-size: 11px; color: #81A882;
      text-transform: uppercase; letter-spacing: 2px; font-weight: 600;
    }}
    .content {{ padding: 36px 32px; }}
    .greeting {{ font-size: 18px; font-weight: 700; color: #0C2417; margin-bottom: 12px; }}
    .badge {{
      display: inline-block; padding: 4px 12px;
      background-color: #E8F0E9; color: #133623;
      border-radius: 999px; font-size: 11px; font-weight: 700;
      text-transform: uppercase; letter-spacing: 1px; margin-bottom: 16px;
    }}
    .details-card {{
      background: #F4F7F4; border: 1px solid #CEDECF;
      border-radius: 14px; padding: 20px 24px; margin: 24px 0;
    }}
    .details-row {{ display: flex; justify-content: space-between; padding: 8px 0; border-bottom: 1px solid #E1E9E2; font-size: 13px; }}
    .details-row:last-child {{ border-bottom: none; }}
    .details-label {{ color: #40514E; font-weight: 500; }}
    .details-value {{ color: #0C2417; font-weight: 700; text-align: right; }}
    .cta-button {{
      display: block; width: 100%; box-sizing: border-box;
      background-color: #0C2417; color: #ffffff !important;
      text-decoration: none; text-align: center; padding: 16px 24px;
      border-radius: 12px; font-size: 14px; font-weight: 700;
      letter-spacing: 0.5px; margin: 28px 0 16px 0;
    }}
    .footer {{
      background-color: #E8F0E9; padding: 24px 30px;
      text-align: center; font-size: 11px; color: #40514E;
      border-top: 1px solid #CEDECF;
    }}
  </style>
</head>
<body>
  <div class="wrapper">
    <div class="container">
      <div class="header">
        <div style="font-size: 28px; margin-bottom: 6px;">🌿</div>
        <h1 class="header-title">ProgressRooms</h1>
        <div class="header-subtitle">Virtual Studio Sanctuary</div>
      </div>
      <div class="content">
        {content_html}
      </div>
      <div class="footer">
        <p style="margin: 0 0 6px 0;"><strong>ProgressRooms Virtual Studio Operating System</strong></p>
        <p style="margin: 0; color: #81A882;">Independent Teacher Sanctuaries & Scheduled Batch Classrooms • India</p>
      </div>
    </div>
  </div>
</body>
</html>"""


def render_class_reminder_2h_email(
    student_name: str,
    studio_name: str,
    class_name: str,
    teacher_name: str,
    start_time_ist: str,
    duration_minutes: int,
    suggested_join_time_ist: str,
    join_url: str,
    equipment_needed: Optional[str] = None,
    prep_instructions: Optional[str] = None
) -> tuple[str, str]:
    first_name = student_name.strip().split()[0] if student_name else "Yogi"
    subject = f"🌿 2-Hour Reminder: {class_name} at {studio_name} ({start_time_ist})"

    equipment_row = f"""<div class="details-row"><span class="details-label">Equipment</span><span class="details-value">{equipment_needed}</span></div>""" if equipment_needed else ""
    prep_block = f"""<div style="margin-top: 16px; padding: 14px; background: #E8F0E9; border-radius: 10px; font-size: 12px; color: #133623;"><strong>Preparation Note:</strong> {prep_instructions}</div>""" if prep_instructions else ""

    body = f"""
      <span class="badge">Preparation Reminder • 2 Hours</span>
      <div class="greeting">Namaste {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        Your upcoming class with <strong>{studio_name}</strong> is scheduled in two hours. Here are your final batch details:
      </p>

      <div class="details-card">
        <div class="details-row"><span class="details-label">Class</span><span class="details-value">{class_name}</span></div>
        <div class="details-row"><span class="details-label">Studio</span><span class="details-value">{studio_name}</span></div>
        <div class="details-row"><span class="details-label">Instructor</span><span class="details-value">{teacher_name}</span></div>
        <div class="details-row"><span class="details-label">Start Time</span><span class="details-value">{start_time_ist}</span></div>
        <div class="details-row"><span class="details-label">Duration</span><span class="details-value">{duration_minutes} Minutes</span></div>
        <div class="details-row" style="background:#E8F0E9; padding: 10px 8px; border-radius: 8px;"><span class="details-label" style="font-weight:700; color:#0C2417;">Please Join Early</span><span class="details-value" style="color:#0C2417;">{suggested_join_time_ist} (5m early)</span></div>
        {equipment_row}
      </div>

      {prep_block}

      <a href="{join_url}" class="cta-button">Open Class Portal & Join Live</a>
      <p style="font-size: 11px; text-align: center; color: #81A882; margin: 0;">
        Joining securely verifies your student pass and always displays real-time class status.
      </p>
    """
    return subject, _email_wrapper(subject, body)


def render_class_reminder_15m_email(
    student_name: str,
    studio_name: str,
    class_name: str,
    teacher_name: str,
    start_time_ist: str,
    suggested_join_time_ist: str,
    join_url: str
) -> tuple[str, str]:
    first_name = student_name.strip().split()[0] if student_name else "Yogi"
    subject = f"🌿 Starting in 15 Minutes: {class_name} ({start_time_ist})"

    body = f"""
      <span class="badge" style="background: #0C2417; color: #ffffff;">Starting Soon • 15 Minutes</span>
      <div class="greeting">Namaste {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        Your sanctuary for <strong>{class_name}</strong> with <strong>{teacher_name}</strong> is opening.
      </p>

      <div class="details-card">
        <div class="details-row"><span class="details-label">Class</span><span class="details-value">{class_name}</span></div>
        <div class="details-row"><span class="details-label">Start Time</span><span class="details-value">{start_time_ist}</span></div>
        <div class="details-row" style="background:#E8F0E9; padding: 10px 8px; border-radius: 8px;"><span class="details-label" style="font-weight:700; color:#0C2417;">Suggested Arrival</span><span class="details-value" style="color:#0C2417;">{suggested_join_time_ist} (5 min early)</span></div>
      </div>

      <a href="{join_url}" class="cta-button">Join Sanctuary Now</a>
    """
    return subject, _email_wrapper(subject, body)


def render_class_correction_email(
    student_name: str,
    studio_name: str,
    class_name: str,
    changes_summary: str,
    latest_start_time_ist: str,
    join_url: str
) -> tuple[str, str]:
    first_name = student_name.strip().split()[0] if student_name else "Yogi"
    subject = f"⚠️ Schedule Correction: {class_name} at {studio_name}"

    body = f"""
      <span class="badge" style="background: #FFF3CD; color: #856404;">Schedule Updated</span>
      <div class="greeting">Namaste {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        Please note the following consolidated changes for your upcoming class <strong>{class_name}</strong>:
      </p>

      <div style="background: #FFF9E6; border: 1px solid #FFEBAA; border-radius: 12px; padding: 16px; margin: 18px 0; font-size: 13px; color: #533F03; line-height: 1.6;">
        {changes_summary}
      </div>

      <div class="details-card">
        <div class="details-row"><span class="details-label">Updated Start Time</span><span class="details-value">{latest_start_time_ist}</span></div>
        <div class="details-row"><span class="details-label">Studio</span><span class="details-value">{studio_name}</span></div>
      </div>

      <a href="{join_url}" class="cta-button">View Updated Class Details</a>
    """
    return subject, _email_wrapper(subject, body)


def render_class_cancellation_email(
    student_name: str,
    studio_name: str,
    class_name: str,
    original_time_ist: str,
    reason: Optional[str] = None
) -> tuple[str, str]:
    first_name = student_name.strip().split()[0] if student_name else "Yogi"
    subject = f"🛑 Cancelled: {class_name} at {studio_name}"

    reason_html = f"""<div style="margin: 16px 0; padding: 14px; background: #F8D7DA; border-radius: 10px; font-size: 12px; color: #721C24;"><strong>Reason:</strong> {reason}</div>""" if reason else ""

    body = f"""
      <span class="badge" style="background: #F8D7DA; color: #721C24;">Class Cancelled</span>
      <div class="greeting">Namaste {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        We regret to inform you that <strong>{class_name}</strong> scheduled for <strong>{original_time_ist}</strong> at <strong>{studio_name}</strong> has been cancelled.
      </p>

      {reason_html}

      <p style="font-size: 13px; color: #618D63; line-height: 1.5;">
        All subsequent reminder notifications for this session have been cancelled. Please visit your student portal to view future available sessions.
      </p>
    """
    return subject, _email_wrapper(subject, body)


# ---------------------------------------------------------------------------
# Student Payment Templates (Dispatched via Admin Sender)
# ---------------------------------------------------------------------------

def render_student_payment_received_wa(
    student_name: str,
    studio_name: str,
    class_or_pass_name: str,
    amount_inr: float,
    receipt_url: str
) -> str:
    first_name = student_name.strip().split()[0] if student_name else "Student"
    return (
        f"✅ *Payment Confirmed — {studio_name}*\n\n"
        f"Namaste {first_name},\n"
        f"Your payment of ₹{amount_inr:,.2f} for *{class_or_pass_name}* at *{studio_name}* was successful.\n\n"
        f"Your booking has been activated. View receipt & access your classroom:\n"
        f"{receipt_url}"
    )


def render_student_payment_received_email(
    student_name: str,
    studio_name: str,
    class_or_pass_name: str,
    amount_inr: float,
    receipt_url: str,
    payment_reference: str
) -> tuple[str, str]:
    first_name = student_name.strip().split()[0] if student_name else "Yogi"
    subject = f"✅ Payment Confirmed: {class_or_pass_name} ({studio_name})"

    body = f"""
      <span class="badge" style="background: #E8F0E9; color: #133623;">Payment Received</span>
      <div class="greeting">Namaste {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        Thank you for your payment. Your enrollment with <strong>{studio_name}</strong> is now confirmed and active.
      </p>

      <div class="details-card">
        <div class="details-row"><span class="details-label">Offering</span><span class="details-value">{class_or_pass_name}</span></div>
        <div class="details-row"><span class="details-label">Studio</span><span class="details-value">{studio_name}</span></div>
        <div class="details-row"><span class="details-label">Amount Paid</span><span class="details-value">₹{amount_inr:,.2f}</span></div>
        <div class="details-row"><span class="details-label">Payment ID</span><span class="details-value">{payment_reference}</span></div>
      </div>

      <a href="{receipt_url}" class="cta-button">Open Student Sanctuary & View Details</a>
      <p style="font-size: 11px; text-align: center; color: #81A882; margin: 0;">
        You will receive scheduled reminders prior to class commencement.
      </p>
    """
    return subject, _email_wrapper(subject, body)


# ---------------------------------------------------------------------------
# Studio Subscription Billing Email Templates (Brevo Responsive HTML)
# ---------------------------------------------------------------------------

def render_billing_invoice_email(
    recipient_name: str,
    studio_name: str,
    invoice_number: str,
    amount_inr: float,
    billing_period: str,
    due_date_str: str,
    invoice_url: str
) -> tuple[str, str]:
    first_name = recipient_name.strip().split()[0] if recipient_name else "Partner"
    subject = f"💳 ProgressRooms Subscription Invoice #{invoice_number} ({studio_name})"

    body = f"""
      <span class="badge" style="background: #E8F0E9; color: #133623;">Invoice Issued</span>
      <div class="greeting">Dear {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        Your ProgressRooms studio platform invoice for <strong>{studio_name}</strong> has been generated.
      </p>

      <div class="details-card">
        <div class="details-row"><span class="details-label">Studio</span><span class="details-value">{studio_name}</span></div>
        <div class="details-row"><span class="details-label">Invoice #</span><span class="details-value">{invoice_number}</span></div>
        <div class="details-row"><span class="details-label">Billing Period</span><span class="details-value">{billing_period}</span></div>
        <div class="details-row"><span class="details-label">Amount Due</span><span class="details-value">₹{amount_inr:,.2f}</span></div>
        <div class="details-row" style="background:#FFF9E6; padding: 10px 8px; border-radius: 8px;"><span class="details-label" style="font-weight:700; color:#856404;">Due Date</span><span class="details-value" style="color:#856404;">{due_date_str}</span></div>
      </div>

      <a href="{invoice_url}" class="cta-button">View Invoice & Pay Securely</a>
      <p style="font-size: 11px; text-align: center; color: #81A882; margin: 0;">
        ProgressRooms Virtual Studio Operating System • Invoices & Platform Services
      </p>
    """
    return subject, _email_wrapper(subject, body)


def render_billing_due_soon_email(
    recipient_name: str,
    studio_name: str,
    amount_inr: float,
    due_date_str: str,
    invoice_url: str
) -> tuple[str, str]:
    first_name = recipient_name.strip().split()[0] if recipient_name else "Partner"
    subject = f"⏳ Payment Due Soon: ProgressRooms Subscription ({studio_name})"

    body = f"""
      <span class="badge" style="background: #FFF3CD; color: #856404;">Payment Due Soon</span>
      <div class="greeting">Dear {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        This is a friendly reminder that your studio subscription balance for <strong>{studio_name}</strong> is due on <strong>{due_date_str}</strong>.
      </p>

      <div class="details-card">
        <div class="details-row"><span class="details-label">Studio</span><span class="details-value">{studio_name}</span></div>
        <div class="details-row"><span class="details-label">Balance Due</span><span class="details-value">₹{amount_inr:,.2f}</span></div>
        <div class="details-row"><span class="details-label">Due Date</span><span class="details-value">{due_date_str}</span></div>
      </div>

      <a href="{invoice_url}" class="cta-button">Review & Complete Payment</a>
      <p style="font-size: 11px; text-align: center; color: #81A882; margin: 0;">
        Paying on time ensures continuous classroom availability and student reminder delivery.
      </p>
    """
    return subject, _email_wrapper(subject, body)


def render_billing_overdue_email(
    recipient_name: str,
    studio_name: str,
    amount_inr: float,
    days_overdue: int,
    invoice_url: str
) -> tuple[str, str]:
    first_name = recipient_name.strip().split()[0] if recipient_name else "Partner"
    subject = f"⚠️ Overdue Notice: ProgressRooms Subscription ({studio_name})"

    body = f"""
      <span class="badge" style="background: #F8D7DA; color: #721C24;">Payment Overdue • {days_overdue} Days</span>
      <div class="greeting">Dear {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        Your platform subscription balance for <strong>{studio_name}</strong> is now <strong>{days_overdue} days overdue</strong>.
      </p>

      <div class="details-card" style="border-color: #F5C6CB;">
        <div class="details-row"><span class="details-label">Studio</span><span class="details-value">{studio_name}</span></div>
        <div class="details-row"><span class="details-label">Overdue Amount</span><span class="details-value" style="color:#721C24;">₹{amount_inr:,.2f}</span></div>
        <div class="details-row"><span class="details-label">Status</span><span class="details-value" style="color:#721C24;">Action Required</span></div>
      </div>

      <a href="{invoice_url}" class="cta-button" style="background-color: #721C24;">Clear Overdue Balance Now</a>
      <p style="font-size: 11px; text-align: center; color: #81A882; margin: 0;">
        Please settle this balance to prevent operational service restrictions on your studio sanctuary.
      </p>
    """
    return subject, _email_wrapper(subject, body)


def render_billing_payment_received_email(
    recipient_name: str,
    studio_name: str,
    amount_inr: float,
    receipt_url: str,
    payment_reference: str
) -> tuple[str, str]:
    first_name = recipient_name.strip().split()[0] if recipient_name else "Partner"
    subject = f"✅ Payment Received: ProgressRooms Subscription ({studio_name})"

    body = f"""
      <span class="badge" style="background: #E8F0E9; color: #133623;">Subscription Active</span>
      <div class="greeting">Dear {first_name},</div>
      <p style="font-size: 14px; line-height: 1.6; color: #40514E; margin: 0 0 16px 0;">
        We have received your payment of <strong>₹{amount_inr:,.2f}</strong> for <strong>{studio_name}</strong>. Thank you for partnering with ProgressRooms!
      </p>

      <div class="details-card">
        <div class="details-row"><span class="details-label">Studio</span><span class="details-value">{studio_name}</span></div>
        <div class="details-row"><span class="details-label">Amount Paid</span><span class="details-value">₹{amount_inr:,.2f}</span></div>
        <div class="details-row"><span class="details-label">Reference</span><span class="details-value">{payment_reference}</span></div>
        <div class="details-row"><span class="details-label">Status</span><span class="details-value" style="color:#133623;">Good Standing</span></div>
      </div>

      <a href="{receipt_url}" class="cta-button">View Official Receipt</a>
    """
    return subject, _email_wrapper(subject, body)

