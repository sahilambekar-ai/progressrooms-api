import uuid
from datetime import datetime, timezone
from pydantic import BaseModel
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import select, and_, or_, desc
from sqlalchemy.orm import selectinload
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.database import get_db
from app.models.organization import Organization, OrganizationMember
from app.models.user import User
from app.models.support import SupportTicket, SupportMessage
from app.modules.organizations.dependencies import require_org_role
from app.modules.auth.dependencies import get_current_user
from app.common.enums import UserRole

router = APIRouter(tags=["Support"])

class CreateTicketPayload(BaseModel):
    organization_id: uuid.UUID
    subject: str
    category: str = "GENERAL"
    initial_message: str

class SendMessagePayload(BaseModel):
    message: str

class UpdateTicketStatusPayload(BaseModel):
    status: str

# -----------------
# Organization Endpoints
# -----------------
@router.get("/organizations/{org_id}/support-tickets")
async def list_org_tickets(
    org_id: uuid.UUID,
    status_filter: str | None = None,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.TEACHER])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(SupportTicket).options(
        selectinload(SupportTicket.messages)
    ).where(SupportTicket.organization_id == org_id)

    if status_filter and status_filter != "ALL":
        stmt = stmt.where(SupportTicket.status == status_filter)

    stmt = stmt.order_by(desc(SupportTicket.updated_at))
    tickets = (await db.execute(stmt)).scalars().all()

    return [
        {
            "id": str(t.id),
            "student_id": str(t.student_id) if t.student_id else None,
            "student_name": t.student_name,
            "student_email": t.student_email,
            "subject": t.subject,
            "category": t.category,
            "status": t.status,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            "message_count": len(t.messages),
            "latest_message": t.messages[-1].message if t.messages else None,
            "latest_message_sender": t.messages[-1].sender_name if t.messages else None,
        }
        for t in tickets
    ]

@router.get("/organizations/{org_id}/support-tickets/{ticket_id}")
async def get_org_ticket_detail(
    org_id: uuid.UUID,
    ticket_id: uuid.UUID,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.TEACHER])),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(SupportTicket).options(
        selectinload(SupportTicket.messages)
    ).where(SupportTicket.id == ticket_id, SupportTicket.organization_id == org_id)
    ticket = (await db.execute(stmt)).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Support ticket not found")

    return {
        "id": str(ticket.id),
        "student_id": str(ticket.student_id) if ticket.student_id else None,
        "student_name": ticket.student_name,
        "student_email": ticket.student_email,
        "subject": ticket.subject,
        "category": ticket.category,
        "status": ticket.status,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "messages": [
            {
                "id": str(m.id),
                "sender_name": m.sender_name,
                "sender_role": m.sender_role,
                "message": m.message,
                "created_at": m.created_at.isoformat(),
            }
            for m in ticket.messages
        ]
    }

@router.post("/organizations/{org_id}/support-tickets/{ticket_id}/messages")
async def send_org_ticket_message(
    org_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: SendMessagePayload,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.TEACHER])),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    ticket = await db.scalar(select(SupportTicket).where(
        SupportTicket.id == ticket_id, SupportTicket.organization_id == org_id
    ))
    if not ticket:
        raise HTTPException(404, "Support ticket not found")

    if not payload.message.strip():
        raise HTTPException(422, "Message cannot be empty")

    org, member = tenant
    sender_name = current_user.full_name or "Studio Team"
    sender_role = "TEACHER" if member and member.role == "TEACHER" else "STUDIO"

    msg = SupportMessage(
        ticket_id=ticket.id,
        sender_user_id=current_user.id,
        sender_name=sender_name,
        sender_role=sender_role,
        message=payload.message.strip()
    )
    db.add(msg)

    if ticket.status == "RESOLVED":
        ticket.status = "IN_PROGRESS"
        ticket.resolved_at = None

    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "id": str(msg.id),
        "sender_name": msg.sender_name,
        "sender_role": msg.sender_role,
        "message": msg.message,
        "created_at": msg.created_at.isoformat()
    }

@router.patch("/organizations/{org_id}/support-tickets/{ticket_id}/status")
async def update_org_ticket_status(
    org_id: uuid.UUID,
    ticket_id: uuid.UUID,
    payload: UpdateTicketStatusPayload,
    tenant: tuple[Organization, OrganizationMember | None] = Depends(require_org_role([UserRole.OWNER, UserRole.ADMIN, UserRole.TEACHER])),
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    ticket = await db.scalar(select(SupportTicket).where(
        SupportTicket.id == ticket_id, SupportTicket.organization_id == org_id
    ))
    if not ticket:
        raise HTTPException(404, "Support ticket not found")

    valid_statuses = ["OPEN", "IN_PROGRESS", "RESOLVED"]
    if payload.status not in valid_statuses:
        raise HTTPException(422, f"Invalid status. Must be one of {valid_statuses}")

    ticket.status = payload.status
    if payload.status == "RESOLVED":
        ticket.resolved_at = datetime.now(timezone.utc)
        ticket.resolved_by_member_id = tenant[1].id if tenant[1] else None
    else:
        ticket.resolved_at = None

    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "id": str(ticket.id),
        "status": ticket.status,
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None
    }

# -----------------
# Student Endpoints
# -----------------
@router.get("/student/support-tickets")
async def list_student_tickets(
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(SupportTicket).options(
        selectinload(SupportTicket.messages),
        selectinload(SupportTicket.organization)
    ).where(
        or_(
            SupportTicket.student_id == current_user.id,
            SupportTicket.student_email == current_user.email
        )
    ).order_by(desc(SupportTicket.updated_at))

    tickets = (await db.execute(stmt)).scalars().all()
    return [
        {
            "id": str(t.id),
            "organization_id": str(t.organization_id),
            "organization_name": t.organization.name if t.organization else "Studio",
            "subject": t.subject,
            "category": t.category,
            "status": t.status,
            "created_at": t.created_at.isoformat(),
            "updated_at": t.updated_at.isoformat(),
            "resolved_at": t.resolved_at.isoformat() if t.resolved_at else None,
            "message_count": len(t.messages),
            "latest_message": t.messages[-1].message if t.messages else None,
            "latest_message_sender": t.messages[-1].sender_name if t.messages else None,
        }
        for t in tickets
    ]

@router.post("/student/support-tickets", status_code=status.HTTP_201_CREATED)
async def create_student_ticket(
    payload: CreateTicketPayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    org = await db.scalar(select(Organization).where(Organization.id == payload.organization_id))
    if not org:
        raise HTTPException(404, "Studio organization not found")

    if not payload.subject.strip() or not payload.initial_message.strip():
        raise HTTPException(422, "Subject and message are required")

    ticket = SupportTicket(
        organization_id=payload.organization_id,
        student_id=current_user.id,
        student_name=current_user.full_name or "Student",
        student_email=current_user.email,
        subject=payload.subject.strip(),
        category=payload.category,
        status="OPEN"
    )
    db.add(ticket)
    await db.flush()

    msg = SupportMessage(
        ticket_id=ticket.id,
        sender_user_id=current_user.id,
        sender_name=current_user.full_name or "Student",
        sender_role="STUDENT",
        message=payload.initial_message.strip()
    )
    db.add(msg)
    await db.commit()
    await db.refresh(ticket)

    return {
        "id": str(ticket.id),
        "organization_id": str(ticket.organization_id),
        "organization_name": org.name,
        "subject": ticket.subject,
        "category": ticket.category,
        "status": ticket.status,
        "created_at": ticket.created_at.isoformat(),
    }

@router.get("/student/support-tickets/{ticket_id}")
async def get_student_ticket_detail(
    ticket_id: uuid.UUID,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(SupportTicket).options(
        selectinload(SupportTicket.messages),
        selectinload(SupportTicket.organization)
    ).where(
        SupportTicket.id == ticket_id,
        or_(
            SupportTicket.student_id == current_user.id,
            SupportTicket.student_email == current_user.email
        )
    )
    ticket = (await db.execute(stmt)).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    return {
        "id": str(ticket.id),
        "organization_id": str(ticket.organization_id),
        "organization_name": ticket.organization.name if ticket.organization else "Studio",
        "subject": ticket.subject,
        "category": ticket.category,
        "status": ticket.status,
        "created_at": ticket.created_at.isoformat(),
        "updated_at": ticket.updated_at.isoformat(),
        "resolved_at": ticket.resolved_at.isoformat() if ticket.resolved_at else None,
        "messages": [
            {
                "id": str(m.id),
                "sender_name": m.sender_name,
                "sender_role": m.sender_role,
                "message": m.message,
                "created_at": m.created_at.isoformat(),
            }
            for m in ticket.messages
        ]
    }

@router.post("/student/support-tickets/{ticket_id}/messages")
async def send_student_ticket_message(
    ticket_id: uuid.UUID,
    payload: SendMessagePayload,
    current_user: User = Depends(get_current_user),
    db: AsyncSession = Depends(get_db)
):
    stmt = select(SupportTicket).where(
        SupportTicket.id == ticket_id,
        or_(
            SupportTicket.student_id == current_user.id,
            SupportTicket.student_email == current_user.email
        )
    )
    ticket = (await db.execute(stmt)).scalar_one_or_none()
    if not ticket:
        raise HTTPException(404, "Ticket not found")

    if not payload.message.strip():
        raise HTTPException(422, "Message cannot be empty")

    msg = SupportMessage(
        ticket_id=ticket.id,
        sender_user_id=current_user.id,
        sender_name=current_user.full_name or "Student",
        sender_role="STUDENT",
        message=payload.message.strip()
    )
    db.add(msg)

    if ticket.status == "RESOLVED":
        ticket.status = "OPEN"
        ticket.resolved_at = None

    ticket.updated_at = datetime.now(timezone.utc)
    await db.commit()

    return {
        "id": str(msg.id),
        "sender_name": msg.sender_name,
        "sender_role": msg.sender_role,
        "message": msg.message,
        "created_at": msg.created_at.isoformat()
    }
