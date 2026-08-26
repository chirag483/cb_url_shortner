from datetime import datetime
from typing import Optional

from pydantic import AnyUrl, BaseModel, Field


class ShortenRequest(BaseModel):
    url: AnyUrl
    custom_code: Optional[str] = Field(default=None, min_length=3, max_length=32)
    expires_in_days: Optional[int] = Field(default=None, ge=1, le=3650)


class ShortenResponse(BaseModel):
    short_code: str
    short_url: str
    long_url: str
    expires_at: Optional[datetime] = None


class StatsResponse(BaseModel):
    short_code: str
    long_url: str
    created_at: datetime
    expires_at: Optional[datetime] = None
    hits: int
    last_accessed_at: Optional[datetime] = None


class HealthResponse(BaseModel):
    status: str
    cache_size: int
