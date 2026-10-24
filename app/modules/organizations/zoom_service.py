"""Compatibility entry point for class callers; all Zoom operations use teacher grants."""
class ZoomAuthError(Exception):
    def __init__(self, message: str, status_code: int = 400, details: dict = None):
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.details = details or {}

async def get_or_create_occurrence_meeting(db, occurrence, org, is_host=False, actor_user_id=None):
    from app.modules.zoom.meetings import meeting_access
    return await meeting_access(db, occurrence, org, is_host=is_host, actor_user_id=actor_user_id)
