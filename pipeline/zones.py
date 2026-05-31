from __future__ import annotations
import json
import cv2
import numpy as np
from typing import Optional

class ZoneMapper:
    """Maps a person BB to zone_id, expenses, entry exit"""
    def __init__(self, layout: dict, camera_id: str):
        self.layout = layout
        self.camera_id = camera_id
        self.camera_type = self._resolve_camera_type(layout, camera_id)
        # getting zones to the camera
        self.zone_polygons: list[dict] = []
        for z in layout.get("zones", []):
            if camera_id in z.get("cameras", []):
                poly = z.get("polygon")
                if poly:
                    self.zone_polygons.append({
                        "zone_id": z["zone_id"],
                        "sku_zone": z.get("sku_zone", z["zone_id"]),
                        "poly_np": np.array(poly, dtype=np.float32),
                    })
        
        ## entry line config
        self.entry_cfg = layout.get("entry_zone", {})
        self.billing_cfg = layout.get("billing_zone", {})

    def map_bbox_to_zone(self, bbox: list[float]) -> Optional[str]:
        """Heuristic approach to map bounding box to zone_id"""
        x1, y1, x2, y2 = bbox
        foot_x = (x1 + x2) / 2.0
        foot_y = float(y2)
        for zdef in self.zone_polygons:
            if self._point_in_polygon(foot_x, foot_y, zdef["poly_np"]):
                return zdef["zone_id"]
        return self._heuristic_zone(foot_x, foot_y)
    
    def get_sku_zone(self, zone_id: Optional[str])-> Optional[str]:
        for zdef in self.zone_polygons:
            if zdef["zone_id"] == zone_id:
                return zdef["sku_zone"]
        return zone_id
    
    def detect_entry_point(self, bbox_history: list[list[float]]) -> Optional[str]:
        if self.camera_type != "entry":
            return None
        if len(bbox_history) < 5:
            return None

        outer_y = self.entry_cfg.get("line_y_outer", 450)
        inner_y = self.entry_cfg.get("line_y_inner", 700)

        foot_ys = [self._foot_y(b) for b in bbox_history]
        
        # Check tracking history sequence
        start_y = foot_ys[0]
        end_y = foot_ys[-1]

        # ENTRY: Started outside (above outer line) and ended deep inside (below inner line)
        if start_y < outer_y and end_y > inner_y:
            return "ENTRY"
            
        # EXIT: Started deep inside (below inner line) and ended outside (above outer line)
        if start_y > inner_y and end_y < outer_y:
            return "EXIT"

        return None
    
    def is_in_billing_zone(self, bbox_xyxy: list[float]) -> bool:
        """Returns True if the foot-point is inside the billing polygon."""
        poly = self.billing_cfg.get("polygon")
        if not poly:
            return self.camera_type == "billing"
        
        x1, y1, x2, y2 = bbox_xyxy
        foot_x = (x1 + x2) / 2.0
        foot_y = float(y2)
        poly_np = np.array(poly, dtype=np.float32)
        return self._point_in_polygon(foot_x, foot_y, poly_np)
        
    @staticmethod
    def _resolve_camera_type(layout: dict, camera_id: str) -> str:
        cams = layout.get("cameras", {})
        if isinstance(cams, dict):
            return cams.get(camera_id, {}).get("type", _infer_type_from_id(camera_id))
        return _infer_type_from_id(camera_id)
 
    def _heuristic_zone(self, foot_x: float, foot_y: float) -> Optional[str]:
        if self.camera_type == "entry":
            # If they are above the outer line, they are on the street outside.
            # Returning None prevents the pipeline from emitting events for them.
            outer_y = self.entry_cfg.get("line_y_outer", 450)
            if foot_y < outer_y:
                return None
            return "ENTRY_ZONE"
            
        if self.camera_type == "billing":
            return "BILLING"
            
        frame_w = self.layout.get("cameras", {}).get(self.camera_id, {}).get("frame_width", 1920)
        if foot_x < frame_w * 0.33: return "ZONE_LEFT"
        if foot_x < frame_w * 0.66: return "ZONE_CENTRE"
        return "ZONE_RIGHT"
 
    @staticmethod
    def _foot_y(bbox: list[float]) -> float:
        return float(bbox[3])
    
    @staticmethod
    def _point_in_polygon(x: float, y: float, poly: np.ndarray) -> bool:
        """Ray-casting algorithm."""
        n = len(poly)
        inside = False
        px, py = float(x), float(y)
        j = n - 1
        for i in range(n):
            xi, yi = float(poly[i][0]), float(poly[i][1])
            xj, yj = float(poly[j][0]), float(poly[j][1])
            if ((yi > py) != (yj > py)) and (px < (xj - xi) * (py - yi) / (yj - yi + 1e-9) + xi):
                inside = not inside
            j = i
        return inside
 

def _infer_type_from_id(camera_id: str) -> str:
    cid = camera_id.upper()
    if "ENTRY" in cid:
        return "entry"
    if "BILLING" in cid or "BILL" in cid:
        return "billing"
    return "floor"