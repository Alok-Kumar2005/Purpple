import pytest
from pydantic import ValidationError
from app.models import InboundEvent, IngestRequest
 
 
class TestInboundEventValidation:
    def _base(self, **overrides) -> dict:
        base = {
            "event_id":   "test-uuid-001",
            "store_id":   "ST1008",
            "camera_id":  "CAM_ENTRY_01",
            "visitor_id": "VIS_abc123",
            "event_type": "ENTRY",
            "timestamp":  "2026-04-10T20:00:00Z",
            "zone_id":    None,
            "dwell_ms":   0,
            "is_staff":   False,
            "confidence": 0.90,
            "metadata":   {},
        }
        base.update(overrides)
        return base
 
    def test_valid_event_parses(self):
        e = InboundEvent(**self._base())
        assert e.store_id == "ST1008"
        assert e.is_staff is False
 
    def test_invalid_event_type_raises(self):
        with pytest.raises(ValidationError, match="event_type"):
            InboundEvent(**self._base(event_type="FAKE_EVENT"))
 
    def test_confidence_above_1_raises(self):
        with pytest.raises(ValidationError):
            InboundEvent(**self._base(confidence=1.5))
 
    def test_confidence_below_0_raises(self):
        with pytest.raises(ValidationError):
            InboundEvent(**self._base(confidence=-0.1))
 
    def test_negative_dwell_ms_raises(self):
        with pytest.raises(ValidationError):
            InboundEvent(**self._base(dwell_ms=-1))
 
    def test_timestamp_z_suffix(self):
        e = InboundEvent(**self._base(timestamp="2026-04-10T20:00:00Z"))
        assert e.timestamp == "2026-04-10T20:00:00Z"
 
    def test_timestamp_offset_accepted(self):
        # +05:30 offset should be accepted (IST)
        e = InboundEvent(**self._base(timestamp="2026-04-10T20:00:00+05:30"))
        assert e.timestamp.endswith("+05:30")
 
    def test_malformed_timestamp_raises(self):
        with pytest.raises(ValidationError, match="timestamp"):
            InboundEvent(**self._base(timestamp="not-a-date"))
 
    def test_batch_over_500_raises(self):
        import uuid
        events = [
            InboundEvent(**self._base(event_id=str(uuid.uuid4())))
            for _ in range(501)
        ]
        with pytest.raises(ValidationError):
            IngestRequest(events=events)
 
    def test_all_valid_event_types_accepted(self):
        valid_types = [
            "ENTRY", "EXIT", "ZONE_ENTER", "ZONE_EXIT", "ZONE_DWELL",
            "BILLING_QUEUE_JOIN", "BILLING_QUEUE_ABANDON", "REENTRY",
        ]
        for et in valid_types:
            e = InboundEvent(**self._base(event_type=et))
            assert e.event_type == et
 
    def test_missing_required_field_raises(self):
        data = self._base()
        del data["store_id"]
        with pytest.raises(ValidationError):
            InboundEvent(**data)
 
    def test_metadata_extra_fields_allowed(self):
        """EventMetadata uses extra='allow' for forward compatibility."""
        e = InboundEvent(**self._base(metadata={"queue_depth": 3, "future_field": "ok"}))
        assert e.metadata.queue_depth == 3
        assert e.metadata.model_extra["future_field"] == "ok"