"""FastAPI application entry point."""
import json
import logging
from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse, FileResponse
from fastapi.staticfiles import StaticFiles
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.models.schemas import PlannerInput
from app.agent.planner import run_planner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(title="Perfect Saturday Planner", version="1.0.0")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    return JSONResponse(
        status_code=422,
        content={"detail": exc.errors(), "body": str(exc.body)},
    )


@app.post("/api/plan")
async def create_plan(user_input: PlannerInput):
    """Stream a Saturday plan as Server-Sent Events."""

    async def event_stream():
        try:
            async for event_json in run_planner(user_input):
                yield f"data: {event_json}\n\n"
        except Exception as e:
            logger.error("Planning failed: %s", e, exc_info=True)
            error_event = json.dumps({
                "type": "error",
                "message": f"Planning encountered an error: {str(e)}",
            })
            yield f"data: {error_event}\n\n"
        finally:
            yield "data: [DONE]\n\n"

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
            "Connection": "keep-alive",
        },
    )


@app.get("/api/health")
async def health():
    return {"status": "ok", "service": "Saturday Planner"}


# Serve static files (frontend)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/")
async def serve_frontend():
    return FileResponse("static/index.html")
