import cv2
import numpy as np
from dataclasses import dataclass, field
from typing import Optional, List
from ultralytics import YOLO
from supervision import ByteTrack, Detections as SvDetecions
import supervision as sv

DARK_VALUE_THRES = 130  ## mean HSV ( staff wear full black)
STAFF_LOCK_FRAMES = 45   # more than that frames before to lock as staff
STAFF_PRESESNSE_RATIO = 0.40 ## if more than that present then they are staff
MIN_FRAMES_FOR_COLOR =10 ## min frames a track nees before attempting color samping

@dataclass
class TrackState:
    track_id: int
    is_staff: Optional[bool] = None
    frame_count: int = 0
    dark_score_sum: float = 0.0 
    dark_score_n: int = 0
    first_frame: int = 0
    last_frame: int = 0
    bbox_history: list = field(default_factory=list)


class PersonTracker:
    """ detect and track person, classify visitor or staff"""
    def __init__(self, conf_thres: float =0.35, device: str = "cpu", camera_id: str = "CAM 1", fps: float = 15.0, model_size: str = "yolov8m.pt"):
        self.conf_threshold = conf_thres 
        self.camera_id = camera_id
        self.fps = fps

        print(f"Loading Yolo model -----")
        self.model = YOLO(model_size)
        self.model.to(device)

        self.byte_tracker = ByteTrack(
            track_activation_threshold=conf_thres,
            lost_track_buffer=int(fps * 3),
            minimum_matching_threshold=0.8,
            frame_rate=int(fps),
        )
        self.track_states: dict[int, TrackState] = {}
        self.total_frames_processed: int = 0

    def update(self, frame: np.ndarray, frame_idx: int) ->list[dict]:
        self.total_frames_processed += 1;
        h, w = frame.shape[:2]
        result = self.model(frame, classes = [0], conf = self.conf_threshold, verbose = False)[0]
        ## convert to sv detection
        sv_dets = SvDetecions.from_ultralytics(result)
        if len(sv_dets) == 0:
            sv_dets = SvDetecions.empty()
        ## tracking
        tracked = self.byte_tracker.update_with_detections(sv_dets)
        if len(tracked) == 0:
            return []
        
        output = []
        for i in range(len(tracked)):
            tid = int(tracked.tracker_id[i])
            bbox_xyxy = tracked.xyxy[i]
            conf = float(tracked.confidence[i]) if tracked.confidence is not None else 0.5
            # Update track state
            state = self._get_or_create_state(tid, frame_idx)
            state.frame_count += 1
            state.last_frame = frame_idx
            state.bbox_history.append(bbox_xyxy.tolist())
            if len(state.bbox_history) > 30:
                state.bbox_history.pop(0)
            # Staff classification
            is_staff = self._classify_staff(frame, bbox_xyxy, state, frame_idx)
 
            # Normalise bbox → [0,1] for schema
            x1, y1, x2, y2 = bbox_xyxy
            bbox_norm = [
                float(x1 / w), float(y1 / h),
                float(x2 / w), float(y2 / h),
            ]
 
            output.append({
                "track_id": tid,
                "bbox_xyxy": bbox_xyxy.tolist(),
                "bbox_norm": bbox_norm,
                "confidence": round(conf, 3),
                "is_staff": is_staff,
                "frame_idx": frame_idx,
            })
 
        return output
    
    def _classify_staff(self, frame: np.ndarray, bbox: np.ndarray, state: TrackState, frame_idx: int) -> bool:
        """Classify staff based on stable clothing color analysis."""
        if state.is_staff is not None:
            return state.is_staff
        
        # Collect color samples early on
        if state.frame_count >= MIN_FRAMES_FOR_COLOR:
            dark_score = self._torso_dark_score(frame, bbox)
            state.dark_score_sum += dark_score
            state.dark_score_n += 1

        # Lock classification once we hit our frame threshold
        if state.frame_count >= STAFF_LOCK_FRAMES:
            avg_dark = (
                state.dark_score_sum / state.dark_score_n
                if state.dark_score_n > 0 else 0.0
            )
            # Staff dress in full black outfits; clean color thresholding is highly reliable
            is_staff = (avg_dark > 0.55)
            state.is_staff = is_staff
            return is_staff
            
        return False  # Default to false during warm-up period
    
    def _torso_dark_score(self, frame: np.ndarray, bbox: np.ndarray) -> float:
        """Normalized dark-clothing heuristic robust against retail lighting reflections."""
        x1, y1, x2, y2 = [int(v) for v in bbox]
        h_box = y2 - y1
        torso_y1 = max(0, y1 + h_box // 3)
        torso_y2 = min(frame.shape[0], y1 + 2 * h_box // 3)
        x1 = max(0, x1)
        x2 = min(frame.shape[1], x2)

        if torso_y2 <= torso_y1 or x2 <= x1:
            return 0.0

        torso_roi = frame[torso_y1:torso_y2, x1:x2]
        if torso_roi.size == 0:
            return 0.0
        
        hsv = cv2.cvtColor(torso_roi, cv2.COLOR_BGR2HSV)
        mean_v = float(np.mean(hsv[:, :, 2]))  # Brightness channel
        mean_s = float(np.mean(hsv[:, :, 1]))  # Saturation purity channel

        # CRITICAL FIX: If the clothing is clearly bright/reflective, 
        # it cannot be a black uniform. Return 0.0 immediately.
        if mean_v > 200:
            return 0.0

        # Map scores linearly across the 255 spectrum
        dark_by_value = max(0.0, 1.0 - (mean_v / 255.0))
        dark_by_sat = max(0.0, 1.0 - (mean_s / 255.0))

        return (dark_by_value * 0.6 + dark_by_sat * 0.4)
 
 
    def _get_or_create_state(self, track_id: int, frame_idx: int) -> TrackState:
        if track_id not in self.track_states:
            self.track_states[track_id] = TrackState(
                track_id=track_id,
                first_frame=frame_idx,
                last_frame=frame_idx,
            )
        return self.track_states[track_id]
 
    def get_state(self, track_id: int) -> Optional[TrackState]:
        return self.track_states.get(track_id)