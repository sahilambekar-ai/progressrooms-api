import secrets
from fastapi import HTTPException
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.organization import OrganizationMember

async def generate_teacher_code(db: AsyncSession) -> str:
    for _ in range(20):
        code = f"{secrets.randbelow(90_000_000) + 10_000_000:08d}"
        if not await db.scalar(select(OrganizationMember.id).where(OrganizationMember.teacher_code == code)):
            return code
    raise HTTPException(503, "A teacher code could not be allocated. Please try again.")

