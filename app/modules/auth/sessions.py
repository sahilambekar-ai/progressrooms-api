from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.user import User


async def revoke_sessions(db: AsyncSession, user: User) -> None:
    # An SQL increment avoids lost revocations from concurrent reset/logout.
    await db.execute(update(User).where(User.id == user.id)
                     .values(session_version=User.session_version + 1))
