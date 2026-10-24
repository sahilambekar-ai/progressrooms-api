"""Explicit legacy-secret cutover. Preview by default; never prints credentials."""
import argparse
import asyncio
import uuid
from sqlalchemy import select, update, func
from app.core.database import AsyncSessionLocal
from app.models.integration import OrganizationIntegration
from app.models.organization import OrganizationSetting
from app.models.zoom import TeacherZoomConnection
from app.modules.zoom.service import lock_studio

async def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--organization', type=uuid.UUID, required=True)
    parser.add_argument('--purge', action='store_true')
    parser.add_argument('--acknowledge-backup-and-live-acceptance', action='store_true')
    args = parser.parse_args()
    async with AsyncSessionLocal() as db:
        count = await db.scalar(select(func.count(OrganizationIntegration.id)).where(
            OrganizationIntegration.organization_id == args.organization, OrganizationIntegration.provider == 'ZOOM',
            OrganizationIntegration.credentials_enc.is_not(None)))
        print(f'Legacy credential records in selected studio: {count}')
        if not args.purge:
            print('Preview only. No credential material changed.')
            return
        if not args.acknowledge_backup_and_live_acceptance:
            parser.error('Purge requires acknowledgment of backup and successful live OAuth acceptance')
        await lock_studio(db, args.organization)
        active = await db.scalar(select(TeacherZoomConnection.id).where(TeacherZoomConnection.organization_id == args.organization,
            TeacherZoomConnection.mode == 'live', TeacherZoomConnection.status == 'ACTIVE').limit(1))
        if not active:
            parser.error('At least one active live OAuth connection is required before legacy credential purge')
        await db.execute(update(OrganizationIntegration).where(OrganizationIntegration.organization_id == args.organization,
            OrganizationIntegration.provider == 'ZOOM').values(credentials_enc=None, is_active=False))
        await db.execute(update(OrganizationSetting).where(OrganizationSetting.organization_id == args.organization)
            .values(zoom_host_key=None, zoom_connected=False, zoom_account_email=None))
        await db.commit()
        print('Legacy credential material and shared host key removed; historical meetings retained.')

if __name__ == '__main__':
    asyncio.run(main())
