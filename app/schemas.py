"""Request payload models."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class StaticAOIRequest(BaseModel):
    longitude: float = Field(..., ge=-180, le=180)
    latitude: float = Field(..., ge=-90, le=90)
    date: date
    buffer_m: float = Field(default=3000, gt=0)
    context_buffer_m: float = Field(default=3000, ge=0)
    risk_profile: str = Field(default="regional", pattern="^(regional|finca)$")


class WildfireCalculationRequest(BaseModel):
    user_id: str
    model_id: str
    session_id: str
    country: str | None = None
    lkr: str | None = None
    callback_url: str | None = None
    start_date: datetime
    end_date: datetime
    resolution: int | None = None
    buffer_distance: float = Field(default=0, ge=0)
    coordinates: dict[str, Any] | None = None
    topology: list[dict[str, Any]] = Field(default_factory=list)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FWIAreaSummaryRequest(BaseModel):
    date: date
    aoi: dict[str, Any]
    hour_index: int = Field(default=15, ge=0, le=95)
