import json
import logging
import time
import uuid
from contextvars import ContextVar
 
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response
 
logger = logging.getLogger("store_intelligence.access")

_trace_id_var: ContextVar[str] = ContextVar("trace_id", default="")
_event_count_var: ContextVar[int] = ContextVar("event_count", default=0)
 
 
def get_trace_id() -> str:
    return _trace_id_var.get()
 
 
def set_event_count(n: int) -> None:
    _event_count_var.set(n)
 
 
class StructuredLoggingMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next) -> Response:
        trace_id = str(uuid.uuid4())
        _trace_id_var.set(trace_id)
        _event_count_var.set(0)
 
        # Extract store_id from path if present (/stores/{id}/...)
        path_parts = request.url.path.strip("/").split("/")
        store_id = None
        if len(path_parts) >= 2 and path_parts[0] == "stores":
            store_id = path_parts[1]
 
        t0 = time.perf_counter()
        response = await call_next(request)
        latency_ms = round((time.perf_counter() - t0) * 1000, 1)
 
        record = {
            "trace_id":    trace_id,
            "method":      request.method,
            "endpoint":    request.url.path,
            "store_id":    store_id,
            "status_code": response.status_code,
            "latency_ms":  latency_ms,
        }
 
        event_count = _event_count_var.get()
        if event_count > 0:
            record["event_count"] = event_count
 
        logger.info(json.dumps(record))
 
        # Propagate trace_id to client for correlation
        response.headers["X-Trace-Id"] = trace_id
        return response
 