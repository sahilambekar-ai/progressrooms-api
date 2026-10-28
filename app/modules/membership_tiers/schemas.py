import uuid
from decimal import Decimal
from typing import Literal
from app.common.validation import Name, Currency
from pydantic import BaseModel, Field, model_validator

class ClassPackageSelection(BaseModel):
    session_id: uuid.UUID
    package_id: uuid.UUID

class MembershipTierCreate(BaseModel):
    name: Name
    tier_type: Literal["MONTHLY", "PACKAGE", "DROP_IN"] = "MONTHLY"  # MONTHLY, PACKAGE, DROP_IN
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    offer_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: Currency = "INR"
    validity_days: int = Field(default=30, ge=1, le=3650)
    total_classes: int | None = Field(default=None, ge=1, le=1000000)
    description: str | None = None
    is_active: bool = True
    class_ids: list[uuid.UUID] = []
    class_packages: list[ClassPackageSelection] = Field(default_factory=list, max_length=100)

    @model_validator(mode="after")
    def validate_offer_price(self):
        if self.offer_price is not None and self.offer_price > self.price:
            raise ValueError("Offer price must not be greater than price")
        return self

class MembershipTierUpdate(BaseModel):
    name: Name | None = None
    tier_type: Literal["MONTHLY", "PACKAGE", "DROP_IN"] | None = None
    price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    offer_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: Currency | None = None
    validity_days: int | None = Field(default=None, ge=1, le=3650)
    total_classes: int | None = Field(default=None, ge=1, le=1000000)
    description: str | None = None
    is_active: bool | None = None
    class_ids: list[uuid.UUID] | None = None
    class_packages: list[ClassPackageSelection] | None = Field(default=None, max_length=100)

    @model_validator(mode="after")
    def reject_null_required_values(self):
        for field in {"name", "tier_type", "price", "currency", "validity_days", "is_active"} & self.model_fields_set:
            if getattr(self, field) is None:
                raise ValueError(f"{field} cannot be null")
        return self

class MembershipTierToggleStatus(BaseModel):
    is_active: bool | None = None
