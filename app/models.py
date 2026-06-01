from datetime import datetime
from typing import Any, Optional
from pydantic import BaseModel, Field, field_validator, model_validator
 
 
VALID_EVENT_TYPES = {
    "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
    "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
}
 
 
class EventMetadata(BaseModel):
    queue_depth: Optional[int] = None
    sku_zone: Optional[str] = None
    session_seq: Optional[int] = None
 
    model_config = {"extra": "allow"} 
 
 
class InboundEvent(BaseModel):
    event_id: str = Field(..., min_length=1, max_length=36)
    store_id: str = Field(..., min_length=1, max_length=64)
    camera_id: str = Field(..., min_length=1, max_length=64)
    visitor_id: str = Field(..., min_length=1, max_length=64)
    event_type: str
    timestamp: str = Field(..., description="ISO-8601 UTC")
    zone_id: Optional[str] = None
    dwell_ms: int = Field(default=0, ge=0)
    is_staff: bool = False
    confidence: float = Field(..., ge=0.0, le=1.0)
    metadata: EventMetadata = Field(default_factory=EventMetadata)
 
    @field_validator("event_type")
    @classmethod
    def validate_event_type(cls, v: str) -> str:
        if v not in VALID_EVENT_TYPES:
            raise ValueError(f"Unknown event_type '{v}'. Valid: {VALID_EVENT_TYPES}")
        return v
 
    @field_validator("timestamp")
    @classmethod
    def validate_timestamp(cls, v: str) -> str:
        try:
            datetime.fromisoformat(v.replace("Z", "+00:00"))
        except ValueError:
            raise ValueError(f"timestamp must be ISO-8601, got: {v!r}")
        return v
 
 
class IngestRequest(BaseModel):
    events: list[InboundEvent] = Field(..., min_length=1, max_length=500)
 

class IngestResponse(BaseModel):
    accepted: int
    duplicates: int
    rejected: int
    errors: list[dict[str, Any]] = Field(default_factory=list)
 
class ZoneDwell(BaseModel):
    zone_id: str
    avg_dwell_ms: float
    visit_count: int
 
 
class MetricsResponse(BaseModel):
    store_id: str
    window_start: str
    window_end: str
    unique_visitors: int
    conversion_rate: float
    avg_dwell_ms: float
    zone_dwells: list[ZoneDwell]
    queue_depth: int
    abandonment_rate: float 
    data_confidence: bool 
 
## funnel
class FunnelStage(BaseModel):
    stage: str
    count: int
    drop_off_pct: float
 
 
class FunnelResponse(BaseModel):
    store_id: str
    window: str
    stages: list[FunnelStage]
 
#heatmap
class HeatmapZone(BaseModel):
    zone_id: str
    visit_count: int
    avg_dwell_ms: float
    normalised: float
 
 
class HeatmapResponse(BaseModel):
    store_id: str
    window: str
    zones: list[HeatmapZone]
    data_confidence: bool
 
## anomolies
class Anomaly(BaseModel):
    anomaly_type: str
    severity: str
    description: str
    suggested_action: str
    detected_at: str
    value: Optional[float] = None
    threshold: Optional[float] = None
 
 
class AnomaliesResponse(BaseModel):
    store_id: str
    anomalies: list[Anomaly]
 
## health
class StoreFeedStatus(BaseModel):
    store_id: str
    last_event_ts: Optional[str]
    lag_seconds:  Optional[float]
    status: str   # OK | STALE_FEED | NO_DATA
 
 
class HealthResponse(BaseModel):
    status: str   # healthy | degraded
    db_connected: bool
    stores: list[StoreFeedStatus]
    checked_at: str