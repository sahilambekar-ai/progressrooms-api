import uuid
from datetime import time, date
from pydantic import BaseModel
from app.common.enums import SessionType, SkillLevel, PricingType, InstructorRole

class ClassTypeCreate(BaseModel):
    name: str
    slug: str
    description: str | None = None

class ClassTypeResponse(BaseModel):
    id: uuid.UUID
    name: str
    slug: str
    description: str | None

    class Config:
        from_attributes = True

class SessionPricingCreate(BaseModel):
    pricing_type: PricingType = PricingType.MONTHLY
    price: float
    currency: str = "INR"
    total_classes: int | None = None
    validity_days: int | None = 30

class SessionRequirementCreate(BaseModel):
    prerequisites: str | None = None
    required_equipment: str | None = None
    preparation_instructions: str | None = None
    additional_notes: str | None = None

class ScheduleRuleCreate(BaseModel):
    day_of_week: int # 0=Monday, 6=Sunday
    start_time: str # "07:00:00"
    end_time: str # "08:00:00"
    effective_start_date: date
    effective_end_date: date | None = None

class SessionCreate(BaseModel):
    class_type_id: uuid.UUID
    name: str
    slug: str
    short_description: str | None = None
    full_description: str | None = None
    session_type: SessionType = SessionType.GROUP
    capacity: int = 20
    min_age: int | None = None
    max_age: int | None = None
    skill_level: SkillLevel = SkillLevel.ALL_LEVELS
    duration_minutes: int = 60
    language: str = "English"
    is_public: bool = True
    instructor_member_ids: list[uuid.UUID] = []
    pricing: SessionPricingCreate
    requirements: SessionRequirementCreate | None = None
    schedule_rules: list[ScheduleRuleCreate] = []
