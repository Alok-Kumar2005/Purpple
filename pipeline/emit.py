import json
import uuid
import cv2
import numpy as np
from datetime import datetime, timedelta, timezone
from typing import Optional
from zones  import ZoneMapper


DWELL_INTERVAL = 30  # emit zone dwell every 30 sec
REENTRY_WINDOW = 300  ## session closed within 300s window ( candidat for reentry matching)
REID_SIM_THRES = 0.75  ## cosing sim. above which considered as re entry
QUEUE_DEPTH = 120  ## distance to count as queued

class SessionState:
    def __init__(self, visitor_id: str, track_id: int, first_frame: int):
        self.visitor_id = visitor_id
        self.track_id = track_id
        self.is_open = True
        self.is_staff = False
        self.current_zone: Optional[str] = None
        self.zone_enter_frame: Optional[int] = None
        self.last_dwell_emit_frame: Optional[int] = None
        self.session_seq = 0
        self.first_frame = first_frame
        self.last_seen_frame = first_frame
        self.entry_emitted = False
        self.exit_emitted = False
        self.in_billing = False
        self.billing_entry_frame: Optional[int] = None
        
        # Buffer to hold early events until staff status is locked
        self.event_buffer: list[dict] = []

    def next_seq(self) -> int:
        self.session_seq += 1
        return self.session_seq


class EventEmitter:
    def __init__(self, store_id: str, camera_id: str, clip_start: datetime, fps: float, zone_mapper: ZoneMapper, out_path: str):
        self.store_id = store_id
        self.camera_id = camera_id
        self.clip_start = clip_start
        self.fps = fps
        self.zone_mapper = zone_mapper
        self.out_path = out_path
        self.open_sessions: dict[int, SessionState] = {}
        self.closed_sessions: list[SessionState] = []
        self._prev_frame_ids: set[int] = set()
        self._track_to_visitor: dict[int, str] = {}
        self._out = open(out_path, "w", encoding="utf-8")
        self._event_count = 0
        self._entry_count = 0
        self._exit_count = 0

    def process(self, detections: list[dict], frame_idx: int) -> None:
        current_ids = {d["track_id"] for d in detections}

        # Close sessions for disappeared tracks
        disappeared = self._prev_frame_ids - current_ids
        for tid in disappeared:
            self._close_session(tid, frame_idx)

        # Process active tracks
        for det in detections:
            tid = det["track_id"]
            is_staff = det["is_staff"]
            conf = det["confidence"]
            bbox = det["bbox_xyxy"]

            if tid not in self.open_sessions:
                self._open_session(tid, frame_idx, det)
            
            session = self.open_sessions[tid]
            session.last_seen_frame = frame_idx

            # Update staff classification state as soon as it flips to True
            if is_staff:
                session.is_staff = True

            # Process spatial movements
            self._update_zone(session, bbox, frame_idx, conf)
            self._update_billing(session, bbox, frame_idx, conf)

            # Flush the buffer once we pass the warm-up window (e.g., frame_count >= 45)
            # This ensures early events get stamped with the correct identity!
            if len(session.event_buffer) > 0 and (frame_idx - session.first_frame) >= 45:
                for buffered_event in session.event_buffer:
                    buffered_event["is_staff"] = session.is_staff
                    self._write_to_file(buffered_event)
                session.event_buffer.clear()

        self._prev_frame_ids = current_ids
 
    def flush_open_sessions(self, final_frame_idx: int) -> None:
        for tid in list(self.open_sessions.keys()):
            self._close_session(tid, final_frame_idx, clip_end=True)
        self._out.close()
 
    def summary(self) -> str:
        return (
            f"total_events={self._event_count} "
            f"entries={self._entry_count} "
            f"exits={self._exit_count}"
        )
    
    def _open_session(self, track_id: int, frame_idx: int, det: dict) -> None:
        visitor_id, is_reentry = self._resolve_visitor_id(track_id, det)
        session = SessionState(visitor_id, track_id, frame_idx)
        session.is_staff = det["is_staff"]
        self.open_sessions[track_id] = session

        if is_reentry:
            self._emit("REENTRY", session, frame_idx, det["confidence"], det["bbox_xyxy"])
        else:
            if self.zone_mapper.camera_type == "entry":
                self._emit("ENTRY", session, frame_idx, det["confidence"], det["bbox_xyxy"])
                session.entry_emitted = True
                if not session.is_staff:
                    self._entry_count += 1

    def _close_session(self, track_id: int, frame_idx: int, clip_end: bool = False) -> None:
        session = self.open_sessions.pop(track_id, None)
        if session is None:
            return

        if session.current_zone is not None:
            self._emit_zone_exit(session, frame_idx)

        if not session.exit_emitted:
            if self.zone_mapper.camera_type == "entry" and session.entry_emitted:
                self._emit("EXIT", session, frame_idx, 0.9 if not clip_end else 0.6, None)
                session.exit_emitted = True
                if not session.is_staff:
                    self._exit_count += 1

        # Flush any unhandled short tracks remaining in the buffer
        if len(session.event_buffer) > 0:
            for buffered_event in session.event_buffer:
                buffered_event["is_staff"] = session.is_staff
                self._write_to_file(buffered_event)
            session.event_buffer.clear()

        session.is_open = False
        self.closed_sessions.append(session)

    def _update_zone(self, session: SessionState, bbox: list[float], frame_idx: int, conf: float)->None:
        new_zone = self.zone_mapper.map_bbox_to_zone(bbox= bbox)
        if new_zone != session.current_zone:
            if session.current_zone is not None:
                self._emit_zone_exit(session, frame_idx, conf=conf)
 
            if new_zone is not None:
                session.current_zone = new_zone
                session.zone_enter_frame = frame_idx
                session.last_dwell_emit_frame = frame_idx
 
                self._emit(
                    event_type="ZONE_ENTER",
                    session=session,
                    frame_idx=frame_idx,
                    confidence=conf,
                    bbox=bbox,
                    zone_id=new_zone,
                )
            else:
                session.current_zone = None
                session.zone_enter_frame = None
        else:
            # Same zone — check for ZONE_DWELL trigger
            if session.current_zone is not None and session.last_dwell_emit_frame is not None:
                frames_since_dwell = frame_idx - session.last_dwell_emit_frame
                if frames_since_dwell >= int(DWELL_INTERVAL * self.fps):
                    dwell_ms = int(frames_since_dwell / self.fps * 1000)
                    self._emit(
                        event_type="ZONE_DWELL",
                        session=session,
                        frame_idx=frame_idx,
                        confidence=conf,
                        bbox=bbox,
                        zone_id=session.current_zone,
                        dwell_ms=dwell_ms,
                    )
                    session.last_dwell_emit_frame = frame_idx

    def _emit_zone_exit(self, session: SessionState, frame_idx: int, conf: float = 0.9 ) -> None:
        if session.current_zone is None:
            return
        dwell_ms = 0
        if session.zone_enter_frame is not None:
            dwell_ms = int((frame_idx - session.zone_enter_frame) / self.fps * 1000)
 
        self._emit(
            event_type="ZONE_EXIT",
            session=session,
            frame_idx=frame_idx,
            confidence=conf,
            bbox=None,
            zone_id=session.current_zone,
            dwell_ms=dwell_ms,
        )
        session.current_zone = None
        session.zone_enter_frame = None
        session.last_dwell_emit_frame = None

    def _update_billing(self, session: SessionState, bbox: list[float], frame_idx: int, conf: float) -> None:
        """Track billing zone entry/abandon on the billing camera."""
        if self.zone_mapper.camera_type != "billing":
            return
 
        in_billing = self.zone_mapper.is_in_billing_zone(bbox)
 
        if in_billing and not session.in_billing:
            # Just entered billing zone
            queue_depth = self._estimate_queue_depth(frame_idx)
            if queue_depth > 0:
                self._emit(
                    event_type="BILLING_QUEUE_JOIN",
                    session=session,
                    frame_idx=frame_idx,
                    confidence=conf,
                    bbox=bbox,
                    zone_id="BILLING",
                    metadata={"queue_depth": queue_depth},
                )
            session.in_billing = True
            session.billing_entry_frame = frame_idx
 
        elif not in_billing and session.in_billing:
            # Left billing zone — BILLING_QUEUE_ABANDON (POS correlation happens in API)
            self._emit(
                event_type="BILLING_QUEUE_ABANDON",
                session=session,
                frame_idx=frame_idx,
                confidence=conf,
                bbox=None,
                zone_id="BILLING",
            )
            session.in_billing = False
            session.billing_entry_frame = None

    def _estimate_queue_depth(self, frame_idx: int) -> int:
        """calcaulte queue depth"""
        count = 0
        for sess in self.open_sessions.values():
            if sess.in_billing:
                count += 1
        return count
    
    def _resolve_visitor_id(self, track_id: int, det: dict) -> tuple[str, bool]:
        if track_id in self._track_to_visitor:
            return self._track_to_visitor[track_id], True   # ← is_reentry=True
        frame_idx = det["frame_idx"]
        cutoff = frame_idx - int(REENTRY_WINDOW * self.fps)
        for sess in reversed(self.closed_sessions):
            if sess.last_seen_frame >= cutoff:
                self._track_to_visitor[track_id] = sess.visitor_id
                return sess.visitor_id, True   # ← REENTRY, not ENTRY
 
        # New visitor
        visitor_id = f"VIS_{uuid.uuid4().hex[:8]}"
        self._track_to_visitor[track_id] = visitor_id
        return visitor_id, False
    
    def _emit(self, event_type: str, session: SessionState, frame_idx: int, confidence: float, 
              bbox: Optional[list[float]], zone_id: Optional[str] = None, dwell_ms: int = 0, metadata: Optional[dict] = None) -> None:
        ts = self._frame_to_timestamp(frame_idx)
        sku_zone = self.zone_mapper.get_sku_zone(zone_id) if zone_id else None

        base_meta = {
            "queue_depth": None,
            "sku_zone": sku_zone,
            "session_seq": session.next_seq(),
        }
        if metadata:
            base_meta.update(metadata)

        event = {
            "event_id": str(uuid.uuid4()),
            "store_id": self.store_id,
            "camera_id": self.camera_id,
            "visitor_id": session.visitor_id,
            "event_type": event_type,
            "timestamp": ts,
            "zone_id": zone_id,
            "dwell_ms": dwell_ms,
            "is_staff": session.is_staff, # Read dynamically from session state
            "confidence": round(confidence, 3),
            "metadata": base_meta,
        }

        # Buffer events if we are still inside the warm-up sequence window
        if (frame_idx - session.first_frame) < 45:
            session.event_buffer.append(event)
        else:
            self._write_to_file(event)
 
    def _frame_to_timestamp(self, frame_idx: int) -> str:
        offset_s = frame_idx / self.fps
        ts = self.clip_start + timedelta(seconds=offset_s)
        return ts.strftime("%Y-%m-%dT%H:%M:%SZ")
    
    def _write_to_file(self, event: dict) -> None:
        self._out.write(json.dumps(event) + "\n")
        self._out.flush()
        self._event_count += 1