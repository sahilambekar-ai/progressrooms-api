import uuid
from decimal import Decimal
from pydantic import BaseModel, Field, model_validator

class ClassPackageSelection(BaseModel):
    session_id: uuid.UUID
    package_id: uuid.UUID

class MembershipTierCreate(BaseModel):
    name: str
    tier_type: str = "MONTHLY"  # MONTHLY, PACKAGE, DROP_IN
    price: Decimal = Field(ge=0, max_digits=10, decimal_places=2)
    offer_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: str = "INR"
    validity_days: int = 30
    total_classes: int | None = None
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
    name: str | None = None
    tier_type: str | None = None
    price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    offer_price: Decimal | None = Field(default=None, ge=0, max_digits=10, decimal_places=2)
    currency: str | None = None
    validity_days: int | None = None
    total_classes: int | None = None
    description: str | None = None
    is_active: bool | None = None
    class_ids: list[uuid.UUID] | None = None
    class_packages: list[ClassPackageSelection] | None = Field(default=None, max_length=100)

class MembershipTierToggleStatus(BaseModel):
    is_active: bool | None = None
