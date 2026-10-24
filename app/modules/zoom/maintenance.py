"""Retry provider revocations and purge expired authorization candidates. No token logging."""
import asyncio
import sys
from app.core.database import AsyncSessionLocal
from app.modules.zoom.service import process_revocations

async def main():
    while True:
        try:
            async with AsyncSessionLocal() as db:
                await process_revocations(db)
        except Exception:
            # Keep the worker alive; connection status and owner retry UI expose pending work.
            print("Zoom maintenance needs attention; retrying on the next cycle", file=sys.stderr)
        if "--once" in sys.argv:
            return
        await asyncio.sleep(60)

if __name__ == "__main__":
    asyncio.run(main())
