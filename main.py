# import cv2

# points = []

# def click_event(event, x, y, flags, params):
#     if event == cv2.EVENT_LBUTTONDOWN:
#         print(f"[{x}, {y}],")
#         points.append([x, y])
#         cv2.circle(img, (x, y), 5, (0, 0, 255), -1)
#         coord_text = f"({x}, {y})"
#         text_position = (x + 10, y - 10) 
        
#         cv2.putText(img, coord_text, text_position, cv2.FONT_HERSHEY_SIMPLEX, 
#                     0.45, (0, 255, 255), 1, cv2.LINE_AA)
        
#         # 3. Draw a connecting line if a previous point exists
#         if len(points) > 1:
#             cv2.line(img, tuple(points[-2]), tuple(points[-1]), (255, 0, 0), 2)
            
#         cv2.imshow("Coordinate Extractor", img)

# if __name__ == "__main__":
#     img_path = "image.png" 
#     img = cv2.imread(img_path)
    
#     if img is None:
#         print(f"Error: Unable to load image at '{img_path}'. Verify the filename.")
#         exit(1)
    
#     img = cv2.resize(img, (1920, 1080))
    
#     cv2.namedWindow("Coordinate Extractor")
#     cv2.setMouseCallback("Coordinate Extractor", click_event)
    
#     cv2.imshow("Coordinate Extractor", img)
#     cv2.waitKey(0)
#     cv2.destroyAllWindows()

"""
main.py — FastAPI application entrypoint.

Routes:
  POST /events/ingest
  GET  /stores/{store_id}/metrics
  GET  /stores/{store_id}/funnel
  GET  /stores/{store_id}/heatmap
  GET  /stores/{store_id}/anomalies
  GET  /health

Production concerns addressed here:
  - Lifespan: creates tables on startup, closes engine on shutdown.
  - DB unavailable → 503 with structured body (no raw stack traces).
  - Graceful degradation: each route catches DB errors independently.
  - Idempotency on ingest: handled in ingestion.py via ON CONFLICT DO NOTHING.
  - CORS: open for dev; restrict origins via ALLOWED_ORIGINS env var in prod.
"""

from __future__ import annotations

import logging
import os
from contextlib import asynccontextmanager
from typing import Annotated

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy.exc import OperationalError, SQLAlchemyError
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import create_tables, engine, get_db
from app.ingestion import ingest_batch
from app.metrics import get_store_metrics
from app.funnel import get_store_funnel
from app.heatmap import get_store_heatmap
from app.anomalies import get_store_anomalies
from app.health import get_health
from app.logging_middleware import StructuredLoggingMiddleware, set_event_count
from app.models import (AnomaliesResponse, FunnelResponse, HeatmapResponse, HealthResponse, IngestRequest,
                    IngestResponse, MetricsResponse)

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger.info('{"event": "startup", "msg": "Creating DB tables if absent"}')
    await create_tables()
    logger.info('{"event": "startup", "msg": "Ready"}')
    yield
    await engine.dispose()
    logger.info('{"event": "shutdown", "msg": "Engine disposed"}')


app = FastAPI(
    title="Store Intelligence API",
    version="1.0.0",
    description="Retail CCTV analytics",
    lifespan=lifespan,
)

app.add_middleware(StructuredLoggingMiddleware)

allowed_origins = os.getenv("ALLOWED_ORIGINS", "*").split(",")
app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

## handling error
@app.exception_handler(OperationalError)
async def db_operational_error(request: Request, exc: OperationalError):
    logger.error('{"event": "db_error", "detail": "%s"}', str(exc)[:200])
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_unavailable",
            "detail": "The database is temporarily unavailable. Please retry.",
        },
    )


@app.exception_handler(SQLAlchemyError)
async def db_general_error(request: Request, exc: SQLAlchemyError):
    logger.error('{"event": "db_error", "detail": "%s"}', str(exc)[:200])
    return JSONResponse(
        status_code=503,
        content={
            "error": "database_error",
            "detail": "A database error occurred.",
        },
    )


DB = Annotated[AsyncSession, Depends(get_db)]

## routes
@app.post("/events/ingest", response_model=IngestResponse, summary="Ingest a batch of detection events" )
async def ingest_events(payload: dict, db: DB) -> IngestResponse:
    try:
        result = await ingest_batch(payload, db)
        set_event_count(result.accepted)
        return result
    except (OperationalError, SQLAlchemyError):
        raise
    except Exception as exc:
        logger.exception("Unexpected ingest error")
        raise HTTPException(status_code=500, detail={"error": "ingest_failed", "detail": str(exc)})


@app.get("/stores/{store_id}/metrics", response_model=MetricsResponse, summary="Real-time store metrics")
async def store_metrics(store_id: str, db: DB, window_hours: int = Query(default=24, ge=1, le=168, description="Look-back window in hours") ) -> MetricsResponse:
    try:
        return await get_store_metrics(store_id, db, window_hours=window_hours)
    except (OperationalError, SQLAlchemyError):
        raise
    except Exception as exc:
        logger.exception("metrics error for %s", store_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/stores/{store_id}/funnel", response_model=FunnelResponse, summary="Conversion funnel: Entry → Zone → Billing → Purchase" )
async def store_funnel(store_id: str, db: DB, window_hours: int = Query(default=24, ge=1, le=168) ) -> FunnelResponse:
    try:
        return await get_store_funnel(store_id, db, window_hours=window_hours)
    except (OperationalError, SQLAlchemyError):
        raise
    except Exception as exc:
        logger.exception("funnel error for %s", store_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/stores/{store_id}/heatmap", response_model=HeatmapResponse, summary="Zone visit frequency + avg dwell, normalised 0–100" )
async def store_heatmap(store_id: str, db: DB, window_hours: int = Query(default=24, ge=1, le=168) ) -> HeatmapResponse:
    try:
        return await get_store_heatmap(store_id, db, window_hours=window_hours)
    except (OperationalError, SQLAlchemyError):
        raise
    except Exception as exc:
        logger.exception("heatmap error for %s", store_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/stores/{store_id}/anomalies", response_model=AnomaliesResponse, summary="Active operational anomalies" )
async def store_anomalies(store_id: str, db: DB) -> AnomaliesResponse:
    try:
        return await get_store_anomalies(store_id, db)
    except (OperationalError, SQLAlchemyError):
        raise
    except Exception as exc:
        logger.exception("anomalies error for %s", store_id)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/health", response_model=HealthResponse, summary="Service health + per-store feed status" )
async def health_check(db: DB) -> HealthResponse:
    # Health must never 500 — catch everything
    try:
        return await get_health(db)
    except Exception as exc:
        logger.error("health check error: %s", exc)
        from datetime import datetime, timezone
        return HealthResponse(
            status="degraded",
            db_connected=False,
            stores=[],
            checked_at=datetime.now(tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        )