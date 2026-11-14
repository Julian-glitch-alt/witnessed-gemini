from typing import Literal
from pydantic import BaseModel, Field

FindingStatus = Literal["red", "amber", "green", "unchecked"]
DamageArea = Literal["front", "rear", "left", "right", "none", "unsure"]
DamageType = Literal["dent", "scratch", "crush", "crack", "broken_light", "other", "none", "unsure"]
DamageSeverity = Literal["minor", "moderate", "severe", "unsure"]


class Finding(BaseModel):
    check: str
    status: FindingStatus
    reason: str


class WideReading(BaseModel):
    make: str | None = None
    model: str | None = None
    color: str | None = None
    damage_visible: bool = False
    damage_area: DamageArea | str | None = "unsure"


class DamageReading(BaseModel):
    damage_visible: bool = False
    damage_type: DamageType | str | None = "none"
    damage_area: DamageArea | str | None = "unsure"
    severity: DamageSeverity | str | None = "unsure"


class PlateReading(BaseModel):
    plate_text: str | None = None
    state: str | None = None
    readable: bool = False


class VinReading(BaseModel):
    vin: str | None = None
    readable: bool = False


class Policy(BaseModel):
    make: str | None = None
    model: str | None = None
    color: str | None = None
    plate: str | None = None
    state: str | None = None
    claimant_name: str | None = None
    claimant_statement: str | None = None
    claimed_damage_area: str | None = None
