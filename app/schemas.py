"""Request payload models."""
from __future__ import annotations

from datetime import date, datetime
from typing import Any

from pydantic import BaseModel, Field


class StaticAOIRequest(BaseModel):
    longitude: float = Field(..., ge=-180, le=180)
    latitude: float = Field(..., ge=-90, le=90)
    date: date
    buffer_m: float = Field(default=3000, gt=0, le=100_000)
    context_buffer_m: float = Field(default=3000, ge=0, le=100_000)
    risk_profile: str = Field(default="regional", pattern="^(regional|finca)$")


class WildfireCalculationRequest(BaseModel):
    user_id: str = Field(min_length=1, max_length=128)
    model_id: str = Field(min_length=1, max_length=128)
    session_id: str = Field(min_length=1, max_length=128)
    country: str | None = Field(default=None, max_length=128)
    lkr: str | None = Field(default=None, max_length=512)
    callback_url: str | None = Field(default=None, max_length=2048)
    start_date: datetime
    end_date: datetime
    resolution: int | None = Field(default=None, ge=10, le=1000)
    buffer_distance: float = Field(default=0, ge=0, le=100_000)
    coordinates: dict[str, Any] | None = None
    topology: list[dict[str, Any]] = Field(default_factory=list, max_length=100)
    parameters: dict[str, Any] = Field(default_factory=dict)


class FWIAreaSummaryRequest(BaseModel):
    date: date
    start_date: date | None = None
    aoi: dict[str, Any]
    hour_index: int | None = Field(default=None, ge=0, le=95)
