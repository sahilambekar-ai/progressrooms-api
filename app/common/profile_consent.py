"""Audit a staff attestation without copying personal values into the log."""

from fastapi import HTTPException

from app.models.audit import AuditLog


def attest_profile_change(
    db, *, actor, subject, organization_id, changed_fields, confirmed, reference
):
    if actor.id == subject.id or not changed_fields:
        return
    if not confirmed or not reference or not reference.strip():
        raise HTTPException(
            422, "Confirm prior profile-change consent and supply its reference"
        )
    db.add(
        AuditLog(
            organization_id=organization_id,
            user_id=actor.id,
            entity_type="user_profile",
            entity_id=subject.id,
            action="PROFILE_CONSENT_ATTESTED",
            new_data={
                "fields": sorted(changed_fields),
                "consent_reference": reference.strip(),
            },
        )
    )
