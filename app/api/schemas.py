"""Request bodies (validation only). Business rules stay in the services."""
from datetime import datetime
from pydantic import BaseModel, Field


class SubscriptionRequestIn(BaseModel):
    org_name: str = Field(min_length=2, max_length=120)
    contact_name: str = Field(min_length=2, max_length=120)
    contact_email: str = Field(max_length=254)
    country: str = Field(default="", max_length=60)
    expected_residents: int | None = Field(default=None, ge=1, le=100_000)
    use_case: str = Field(default="", max_length=1000)
    plan: str = "FREE"


class ApproveIn(BaseModel):
    plan: str | None = None
    note: str = Field(default="", max_length=500)


class RejectIn(BaseModel):
    note: str = Field(default="", max_length=500)


class KeyIn(BaseModel):
    label: str = Field(default="", max_length=80)
    role: str = "ADMIN"


class ResidentIn(BaseModel):
    name: str = Field(min_length=1, max_length=120)
    age: int = Field(ge=50, le=120)
    room: str = Field(min_length=1, max_length=40)
    conditions: str = Field(default="None", max_length=200)
    diet: str = Field(default="Regular", max_length=40)
    check_in_at: datetime | None = None


class StayIn(BaseModel):
    at: datetime | None = None


class VitalIn(BaseModel):
    hr: int
    temp: float
    spo2: int
    sleep_hrs: float
    steps: int = Field(ge=0)
    mood: int
    fluids_ml: int = Field(ge=0)


class MedicationIn(BaseModel):
    drug: str = Field(min_length=1, max_length=120)
    status: str


class TransactionIn(BaseModel):
    kind: str
    category: str
    amount: float = Field(gt=0)
    description: str = Field(default="", max_length=300)
    resident_id: int | None = None
    at: datetime | None = None
