"""Legacy bank-data migration: dry-run by default, --apply encrypts atomically.

Back up first; configure an independent FINANCIAL_DATA_ENCRYPTION_KEY and keep it
recoverable separately. Only row counts are printed. Existing ciphertext is checked.
"""

import argparse
import asyncio

from sqlalchemy import select

from app.core.config import settings
from app.core.database import AsyncSessionLocal, engine
from app.core.financial_data import PREFIX, cipher, decrypt_account, encrypt_account
from app.models.organization import OrganizationSetting


async def migrate(db, apply=False):
    if not settings.FINANCIAL_DATA_ENCRYPTION_KEY:
        raise RuntimeError("Configure the independent financial encryption key first")
    cipher()
    query = select(OrganizationSetting).where(
        OrganizationSetting.account_number_enc.is_not(None)
    )
    if apply:
        query = query.with_for_update()
    plaintext = encrypted = 0
    async for row in await db.stream_scalars(query.execution_options(yield_per=100)):
        value = row.account_number_enc
        if not value:
            continue
        if value.startswith(PREFIX):
            decrypt_account(value)
            encrypted += 1
        else:
            plaintext += 1
            if apply:
                row.account_number_enc = encrypt_account(value)
        if apply and (plaintext + encrypted) % 100 == 0:
            await (
                db.flush()
            )  # Release dirty ORM references without committing partial work.
    if apply:
        await db.commit()
    return {"plaintext_rows": plaintext, "encrypted_rows": encrypted, "applied": apply}


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()
    try:
        async with AsyncSessionLocal() as db:
            print(await migrate(db, args.apply))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())
