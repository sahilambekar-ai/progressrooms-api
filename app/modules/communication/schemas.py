import uuid
from datetime import datetime
from pydantic import BaseModel, Field
from typing import Dict, Any, List

class ChannelHealthStatus(BaseModel):
    configured: bool
    status: str  # "CONNECTED", "NOT_CONFIGURED", "WARNING"
    details: str
    sender_identity: str

class ConnectionStatusResponse(BaseModel):
    email: ChannelHealthStatus
    whatsapp_admin: ChannelHealthStatus
    whatsapp_classes: ChannelHealthStatus

class CommunicationSettingsResponse(BaseModel):
    whatsapp_enabled: bool
    email_enabled: bool
    both_channels_disabled: bool
    updated_at: datetime | None = None
    updated_by_user_id: str | None = None
    updated_by_email: str | None = None
    scope: str = "Applies to all studios"
    connection_status: ConnectionStatusResponse

class CommunicationSettingsUpdate(BaseModel):
    whatsapp_enabled: bool | None = None
    email_enabled: bool | None = None
    notes: str | None = None

class CommunicationTestPayload(BaseModel):
    channel: str = Field(..., description="Target channel: 'EMAIL', 'WHATSAPP_ADMIN', or 'WHATSAPP_CLASSES'")
    recipient: str = Field(..., description="Target recipient email or E.164 phone number")
    message: str | None = Field(None, description="Optional custom test message")

class CommunicationTestResponse(BaseModel):
    success: bool
    channel: str
    recipient: str
    status: str  # "SENT", "SUPPRESSED", "FAILED"
    message_id: str | None = None
    suppression_reason: str | None = None
    details: str | None = None

class CommunicationLogItem(BaseModel):
    id: str
    recipient_email: str | None = None
    recipient_phone: str | None = None
    recipient_name: str | None = None
    channel: str
    message_type: str
    status: str
    suppression_reason: str | None = None
    subject_or_title: str | None = None
    body_preview: str | None = None
    created_at: str
    error_details: str | None = None

class CommunicationLogListResponse(BaseModel):
    items: List[CommunicationLogItem]
    total_items: int
    page: int
    page_size: int
    total_pages: int

class CommunicationAuditLogItem(BaseModel):
    id: str
    channel: str
    previous_state: bool
    new_state: bool
    changed_by_email: str | None = None
    changed_at: str
    notes: str | None = None

class StudioBudgetResponse(BaseModel):
    organization_id: str
    studio_name: str
    monthly_allowance_inr: float
    current_month_usage_inr: float
    current_month_message_count: int
    overage_policy: str
    alert_level_sent: int
    usage_percentage: float
    estimated_messages_remaining: int

class StudioBudgetUpdate(BaseModel):
    monthly_allowance_inr: float | None = None
    overage_policy: str | None = None

class EngineCycleResponse(BaseModel):
    timestamp: str
    reminders_dispatched: int
    corrections_dispatched: int
    billing_summary: dict
    details: str


