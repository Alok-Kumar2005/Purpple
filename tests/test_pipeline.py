import json
from pathlib import Path
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import numpy as np
import pytest

from pipeline.zones import ZoneMapper
from pipeline.emit import EventEmitter, SessionState
from pipeline.tracker import PersonTracker, DARK_VALUE_THRES


# ── Fixtures ─────────────────────────────────────────────────────────────────

MINIMAL_LAYOUT = {
    "store_id": "ST_TEST",
    "cameras": {
        "CAM_ENTRY_01": {"type": "entry", "frame_width": 1920, "frame_height": 1080},
        "CAM_FLOOR_01": {"type": "floor", "frame_width": 1920, "frame_height": 1080},
        "CAM_BILLING_01": {"type": "billing", "frame_width": 1920, "frame_height": 1080},
    },
    "entry_zone": {
        "camera_id": "CAM_ENTRY_01",
        "line_y_outer": 450,
        "line_y_inner": 700,
        "direction_down": "ENTRY",
        "direction_up": "EXIT",
    },
    "billing_zone": {
        "zone_id": "BILLING",
        "cameras": ["CAM_BILLING_01"],
        "polygon": [[0, 0], [1920, 0], [1920, 1080], [0, 1080]],
    },
    "zones": [
        {
            "zone_id": "SKINCARE",
            "sku_zone": "SKINCARE",
            "cameras": ["CAM_FLOOR_01"],
            "polygon": [[0, 0], [960, 0], [960, 1080], [0, 1080]],
        },
        {
            "zone_id": "MAKEUP",
            "sku_zone": "MAKEUP",
            "cameras": ["CAM_FLOOR_01"],
            "polygon": [[960, 0], [1920, 0], [1920, 1080], [960, 1080]],
        },
    ],
}


@pytest.fixture
def entry_zone_mapper():
    return ZoneMapper(MINIMAL_LAYOUT, "CAM_ENTRY_01")


@pytest.fixture
def floor_zone_mapper():
    return ZoneMapper(MINIMAL_LAYOUT, "CAM_FLOOR_01")


@pytest.fixture
def billing_zone_mapper():
    return ZoneMapper(MINIMAL_LAYOUT, "CAM_BILLING_01")


def make_emitter(tmp_path: Path, camera_id: str = "CAM_ENTRY_01") -> EventEmitter:
    zm = ZoneMapper(MINIMAL_LAYOUT, camera_id)
    return EventEmitter(
        store_id="ST_TEST",
        camera_id=camera_id,
        clip_start=datetime(2026, 4, 10, 20, 0, 0, tzinfo=timezone.utc),
        fps=15.0,
        zone_mapper=zm,
        out_path=str(tmp_path / f"events_{camera_id}.jsonl"),
    )


def make_detection(
    track_id: int,
    bbox: list[float],
    is_staff: bool = False,
    conf: float = 0.85,
    frame_idx: int = 0,
) -> dict:
    return {
        "track_id": track_id,
        "bbox_xyxy": bbox,
        "bbox_norm": [b / 1920.0 if i % 2 == 0 else b / 1080.0 for i, b in enumerate(bbox)],
        "confidence": conf,
        "is_staff": is_staff,
        "frame_idx": frame_idx,
    }


def read_events(path: str) -> list[dict]:
    events = []
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
    except FileNotFoundError:
        pass
    return events


# ── ZoneMapper Tests ──────────────────────────────────────────────────────────

class TestZoneMapper:
    def test_point_inside_skincare(self, floor_zone_mapper):
        # Foot-point at (400, 900) -> inside SKINCARE polygon (left half)
        bbox = [300.0, 200.0, 500.0, 900.0]
        zone = floor_zone_mapper.map_bbox_to_zone(bbox)
        assert zone == "SKINCARE"

    def test_point_inside_makeup(self, floor_zone_mapper):
        # Foot-point at (1400, 900) -> inside MAKEUP polygon (right half)
        bbox = [1300.0, 200.0, 1500.0, 900.0]
        zone = floor_zone_mapper.map_bbox_to_zone(bbox)
        assert zone == "MAKEUP"

    def test_entry_direction_entry(self, entry_zone_mapper):
        # Starts outside (<450) and crosses deep inside (>700) with sufficient frames
        history = [
            [900.0, 100.0, 1000.0, 300.0],
            [900.0, 100.0, 1000.0, 350.0],
            [900.0, 100.0, 1000.0, 400.0],
            [900.0, 100.0, 1000.0, 500.0],
            [900.0, 100.0, 1000.0, 750.0],
            [900.0, 100.0, 1000.0, 800.0]
        ]
        direction = entry_zone_mapper.detect_entry_point(history)
        assert direction == "ENTRY"

    def test_entry_direction_exit(self, entry_zone_mapper):
        # Starts inside (>700) and crosses outside (<450) with sufficient frames
        history = [
            [900.0, 100.0, 1000.0, 800.0],
            [900.0, 100.0, 1000.0, 750.0],
            [900.0, 100.0, 1000.0, 600.0],
            [900.0, 100.0, 1000.0, 500.0],
            [900.0, 100.0, 1000.0, 400.0],
            [900.0, 100.0, 1000.0, 300.0]
        ]
        direction = entry_zone_mapper.detect_entry_point(history)
        assert direction == "EXIT"

    def test_entry_direction_insufficient_history(self, entry_zone_mapper):
        # Less than required minimum sequence frames -> should return None
        history = [[900.0, 100.0, 1000.0, 300.0], [900.0, 100.0, 1000.0, 800.0]]
        assert entry_zone_mapper.detect_entry_point(history) is None

    def test_billing_in_zone(self, billing_zone_mapper):
        bbox = [500.0, 200.0, 700.0, 800.0]
        assert billing_zone_mapper.is_in_billing_zone(bbox) is True

    def test_sku_zone_lookup(self, floor_zone_mapper):
        assert floor_zone_mapper.get_sku_zone("SKINCARE") == "SKINCARE"
        assert floor_zone_mapper.get_sku_zone("UNKNOWN") == "UNKNOWN"


# ── PersonTracker Staff Detection Tests ───────────────────────────────────────

class TestStaffDetection:
    def _make_tracker(self) -> PersonTracker:
        with patch("tracker.YOLO") as mock_yolo:
            mock_yolo.return_value = MagicMock()
            t = PersonTracker(device="cpu", fps=15.0)
        return t

    def test_dark_torso_score_is_high_for_black_frame(self):
        tracker = self._make_tracker()
        black_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        bbox = np.array([700.0, 100.0, 900.0, 900.0])
        score = tracker._torso_dark_score(black_frame, bbox)
        assert score > 0.8

    def test_dark_torso_score_is_low_for_white_frame(self):
        tracker = self._make_tracker()
        white_frame = np.full((1080, 1920, 3), 255, dtype=np.uint8)
        bbox = np.array([700.0, 100.0, 900.0, 900.0])
        score = tracker._torso_dark_score(white_frame, bbox)
        assert score < 0.2

    def test_staff_not_locked_before_threshold(self):
        tracker = self._make_tracker()
        state = tracker._get_or_create_state(1, 0)
        state.frame_count = 5  # below warm-up frame constraints
        black_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        tracker._classify_staff(black_frame, np.array([700.0, 100.0, 900.0, 900.0]), state, 5)
        assert state.is_staff is None

    def test_staff_locked_after_threshold_with_dark_clothing(self):
        tracker = self._make_tracker()
        state = tracker._get_or_create_state(2, 0)
        state.frame_count = 50  # Exceeds lock configuration bounds (45)
        state.dark_score_sum = 45.0  # avg dark score = 45/50 = 0.90
        state.dark_score_n = 50

        black_frame = np.zeros((1080, 1920, 3), dtype=np.uint8)
        tracker._classify_staff(black_frame, np.array([700.0, 100.0, 900.0, 900.0]), state, 50)
        assert state.is_staff is True


# ── EventEmitter Tests ────────────────────────────────────────────────────────

class TestEventEmitter:
    def test_entry_event_emitted_on_new_track(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_ENTRY_01")
        det = make_detection(track_id=1, bbox=[900.0, 100.0, 1100.0, 800.0], frame_idx=50)
        
        # Fast-forward frame sequence to allow event extraction loop to trigger execution
        emitter.process([det], frame_idx=50)
        emitter.process([det], frame_idx=100)
        emitter.flush_open_sessions(105)

        events = read_events(emitter.out_path)
        entry_events = [e for e in events if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 1
        assert entry_events[0]["visitor_id"].startswith("VIS_")
        assert entry_events[0]["is_staff"] is False

    def test_event_id_is_unique(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_ENTRY_01")
        for i in range(5):
            det = make_detection(track_id=i + 1, bbox=[900.0, 100.0, 1100.0, 800.0], frame_idx=i + 50)
            emitter.process([det], frame_idx=i + 50)
        emitter.flush_open_sessions(150)

        events = read_events(emitter.out_path)
        event_ids = [e["event_id"] for e in events]
        assert len(event_ids) == len(set(event_ids))

    def test_staff_events_not_counted_as_customers(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_ENTRY_01")
        det = make_detection(track_id=99, bbox=[900.0, 100.0, 1100.0, 800.0], is_staff=True, frame_idx=50)
        emitter.process([det], frame_idx=50)
        emitter.flush_open_sessions(110)

        events = read_events(emitter.out_path)
        assert all(e["is_staff"] is True for e in events)
        assert emitter._entry_count == 0

    def test_zone_enter_exit_on_floor_cam(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_FLOOR_01")

        det1 = make_detection(track_id=1, bbox=[300.0, 200.0, 500.0, 900.0], frame_idx=50)
        emitter.process([det1], frame_idx=50)

        det2 = make_detection(track_id=1, bbox=[1300.0, 200.0, 1500.0, 900.0], frame_idx=110)
        emitter.process([det2], frame_idx=110)

        emitter.flush_open_sessions(120)
        events = read_events(emitter.out_path)

        types = [e["event_type"] for e in events]
        assert "ZONE_ENTER" in types
        assert "ZONE_EXIT" in types

    def test_zone_dwell_emitted_after_30s(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_FLOOR_01")
        fps = 15.0
        # Add an explicit buffer (+60 frames) to cross the 30-second interval limit comfortably
        dwell_frames = int(30 * fps) + 50 + 60

        for i in range(50, dwell_frames):
            det = make_detection(track_id=1, bbox=[300.0, 200.0, 500.0, 900.0], frame_idx=i)
            emitter.process([det], frame_idx=i)

        emitter.flush_open_sessions(dwell_frames + 1)
        events = read_events(emitter.out_path)

        dwell_events = [e for e in events if e["event_type"] == "ZONE_DWELL"]
        assert len(dwell_events) >= 1

    def test_empty_store_no_crash(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_ENTRY_01")
        for i in range(100):
            emitter.process([], frame_idx=i)
        emitter.flush_open_sessions(100)

        events = read_events(emitter.out_path)
        assert events == []

    def test_group_entry_three_individuals(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_ENTRY_01")
        dets = [
            make_detection(track_id=1, bbox=[800.0, 100.0, 900.0, 800.0], frame_idx=50),
            make_detection(track_id=2, bbox=[950.0, 100.0, 1050.0, 800.0], frame_idx=50),
            make_detection(track_id=3, bbox=[1100.0, 100.0, 1200.0, 800.0], frame_idx=50),
        ]
        emitter.process(dets, frame_idx=50)
        emitter.process(dets, frame_idx=100)
        emitter.flush_open_sessions(110)

        events = read_events(emitter.out_path)
        entry_events = [e for e in events if e["event_type"] == "ENTRY"]
        assert len(entry_events) == 3

    def test_timestamp_derived_from_frame_and_fps(self, tmp_path):
        emitter = make_emitter(tmp_path, "CAM_ENTRY_01")
        # Frame 150 at 15fps = 10s offset from 20:00:00 -> 20:00:10
        det = make_detection(track_id=1, bbox=[900.0, 100.0, 1100.0, 800.0], frame_idx=150)
        emitter.process([det], frame_idx=150)
        emitter.process([det], frame_idx=200)
        emitter.flush_open_sessions(210)

        events = read_events(emitter.out_path)
        assert len(events) > 0
        assert events[0]["timestamp"] == "2026-04-10T20:00:10Z"

    def test_schema_compliance(self, tmp_path):
        required_fields = {
            "event_id", "store_id", "camera_id", "visitor_id",
            "event_type", "timestamp", "zone_id", "dwell_ms",
            "is_staff", "confidence", "metadata",
        }
        emitter = make_emitter(tmp_path, "CAM_FLOOR_01")
        det = make_detection(track_id=1, bbox=[300.0, 200.0, 500.0, 900.0], frame_idx=50)
        emitter.process([det], frame_idx=50)
        emitter.process([det], frame_idx=100)
        emitter.flush_open_sessions(105)

        events = read_events(emitter.out_path)
        for event in events:
            assert not (required_fields - set(event.keys()))



#  PROMPT:
# "Write pytest tests for a retail CCTV detection pipeline with these components:
#  1. ZoneMapper: maps bounding box coordinates to zone names using polygon intersection.
#     Test: point inside polygon, point outside, foot-point calculation, entry direction detection.
#  2. PersonTracker: classifies staff by dark clothing color (HSV torso analysis) + presence ratio.
#     Test: dark torso returns high dark_score, light torso returns low score, staff locked after
#     STAFF_LOCK_FRAMES, short-lived track not classified as staff.
#  3. EventEmitter: emits ENTRY/EXIT/ZONE_ENTER/ZONE_EXIT/ZONE_DWELL/REENTRY events.
#     Test: new track → ENTRY emitted on entry cam, zone transition → ZONE_ENTER+ZONE_EXIT,
#     dwell interval → ZONE_DWELL, track reappearing → REENTRY, staff events → not emitted,
#     empty frame list → no crash, all-staff clip → 0 customer events.
#  Use pytest fixtures, tmp_path for output files, mock cv2/YOLO where heavy."
#
# CHANGES MADE:
#  - Removed mocking of YOLOv8 internals (too brittle); instead test tracker._torso_dark_score
#    directly with synthetic BGR frames — faster and more precise.
#  - Added edge-case: empty store (zero detections every frame) → flush produces no ENTRY events.
#  - Added edge-case: all-staff clip → no customer events in output.
#  - Replaced AI-generated fixture for layout (used hardcoded minimal dict instead of file load).
#  - Added group-entry test: 3 simultaneous detections on entry cam → 3 ENTRY events, not 1.
#  - Timestamp derivation test added (was missing from AI output).