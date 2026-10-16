import uuid
from datetime import time, date
from pydantic import BaseModel, Field, model_validator
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
    price: float = Field(ge=0)
    currency: str = "INR"
    total_classes: int | None = None
    validity_days: int | None = 30

class SessionRequirementCreate(BaseModel):
    prerequisites: str | None = None
    required_equipment: str | None = None
    preparation_instructions: str | None = None
    additional_notes: str | None = None

class ScheduleRuleCreate(BaseModel):
    day_of_week: int = Field(ge=0, le=6) # 0=Monday, 6=Sunday
    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
    end_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
    effective_start_date: date
    effective_end_date: date | None = None

    @model_validator(mode="after")
    def validate_range(self):
        if time.fromisoformat(self.end_time) <= time.fromisoformat(self.start_time):
            raise ValueError("End time must be after start time")
        if self.effective_end_date and self.effective_end_date < self.effective_start_date:
            raise ValueError("End date must be on or after start date")
        return self

class SessionCreate(BaseModel):
    class_type_id: uuid.UUID
    name: str
    slug: str
    short_description: str | None = None
    full_description: str | None = None
    session_type: SessionType = SessionType.GROUP
    capacity: int = Field(default=20, ge=1)
    min_age: int | None = None
    max_age: int | None = None
    skill_level: SkillLevel = SkillLevel.ALL_LEVELS
    duration_minutes: int = 60
    language: str = "English"
    is_public: bool = True
    is_active: bool = True
    instructor_member_ids: list[uuid.UUID] = []
    pricing: SessionPricingCreate
    requirements: SessionRequirementCreate | None = None
    schedule_rules: list[ScheduleRuleCreate] = []

class SessionUpdate(BaseModel):
    name: str | None = None
    capacity: int | None = Field(default=None, ge=1)
    duration_minutes: int | None = None
    is_active: bool | None = None
    is_public: bool | None = None
    short_description: str | None = None
    full_description: str | None = None
    skill_level: SkillLevel | None = None
    session_type: SessionType | None = None
    instructor_member_ids: list[uuid.UUID] | None = None
    pricing: SessionPricingCreate | None = None
    requirements: SessionRequirementCreate | None = None
    schedule_rules: list[ScheduleRuleCreate] | None = None

class SessionToggleStatus(BaseModel):
    is_active: bool | None = None

class OneOnOneSessionCreate(BaseModel):
    title: str = Field(default="1-on-1 Private Session", min_length=1, max_length=255)
    teacher_member_id: uuid.UUID
    student_user_id: uuid.UUID
    date: date
    start_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
    end_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d(?::[0-5]\d)?$")
    price: float = Field(default=0.0, ge=0)

    @model_validator(mode="after")
    def validate_range(self):
        st = time.fromisoformat(self.start_time[:5])
        et = time.fromisoformat(self.end_time[:5])
        if et <= st:
            raise ValueError("End time must be after start time")
        return self

