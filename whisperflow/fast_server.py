"""FastAPI app for WhisperFlow transcription service."""

import logging
import os
import secrets
import threading
import time
from typing import List, Optional

from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    Header,
    HTTPException,
    Query,
    Request,
    UploadFile,
    WebSocket,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from starlette.websockets import WebSocketDisconnect

import whisperflow.streaming as st
import whisperflow.transcriber as ts
from whisperflow import __version__

logger = logging.getLogger(__name__)

SERVICE_NAME = "whisper-flow"
DEFAULT_MODEL_NAME = os.getenv("WHISPERFLOW_MODEL_NAME", "tiny.en").strip() or "tiny.en"
MAX_AUDIO_BYTES = int(os.getenv("WHISPERFLOW_MAX_AUDIO_BYTES", str(10 * 1024 * 1024)))
API_KEY = (os.getenv("WHISPERFLOW_API_KEY") or "").strip()
AUTH_REQUIRED = bool(API_KEY)
WARM_ON_START = (os.getenv("WHISPERFLOW_WARM_ON_START", "false").strip().lower() in {"1", "true", "yes"})
MAX_ACTIVE_WS_SESSIONS = max(1, int(os.getenv("WHISPERFLOW_MAX_ACTIVE_WS_SESSIONS", "25")))
ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("WHISPERFLOW_ALLOWED_ORIGINS", "*").split(",")
    if origin.strip()
] or ["*"]

BOOT_TIME = time.time()
sessions = {}

_warm_lock = threading.Lock()
_warm_in_progress = False
_warm_last_error = ""
_warm_started_at = 0.0
_warm_finished_at = 0.0


app = FastAPI(
    title="WhisperFlow API",
    version=__version__,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=ALLOWED_ORIGINS,
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup_hook() -> None:
    if WARM_ON_START:
        _start_background_warmup()
    logger.info(
        "WhisperFlow server started: version=%s model=%s auth=%s warm_on_start=%s max_ws_sessions=%s",
        __version__,
        DEFAULT_MODEL_NAME,
        AUTH_REQUIRED,
        WARM_ON_START,
        MAX_ACTIVE_WS_SESSIONS,
    )


def _error_payload(code: str, message: str, details: Optional[dict] = None) -> dict:
    payload = {
        "ok": False,
        "error": {
            "code": code,
            "message": message,
        },
    }
    if details:
        payload["error"]["details"] = details
    return payload


def _validate_api_key(provided_key: Optional[str]) -> bool:
    if not AUTH_REQUIRED:
        return True
    if not provided_key:
        return False
    return secrets.compare_digest(provided_key, API_KEY)


def require_api_key(x_api_key: Optional[str] = Header(default=None, alias="X-API-Key")) -> None:
    """Dependency to protect HTTP routes with API key."""
    if _validate_api_key(x_api_key):
        return
    raise HTTPException(
        status_code=401,
        detail=_error_payload("unauthorized", "Missing or invalid API key."),
    )


async def _authorize_websocket(websocket: WebSocket) -> bool:
    """Validate websocket api key from header or query parameter."""
    if not AUTH_REQUIRED:
        return True
    provided = websocket.headers.get("x-api-key") or websocket.query_params.get("api_key")
    if _validate_api_key(provided):
        return True
    await websocket.accept()
    await websocket.send_json(_error_payload("unauthorized", "Missing or invalid API key."))
    await websocket.close(code=1008)
    return False


def _warm_model_sync() -> None:
    """Load model into memory. Used by wake route and background warmup."""
    global _warm_in_progress, _warm_last_error, _warm_started_at, _warm_finished_at
    with _warm_lock:
        if _warm_in_progress:
            return
        _warm_in_progress = True
        _warm_last_error = ""
        _warm_started_at = time.time()

    try:
        ts.preload_model(DEFAULT_MODEL_NAME)
    except Exception as exc:  # pragma: no cover
        _warm_last_error = str(exc)
        logger.exception("Model warmup failed: %s", exc)
    finally:
        _warm_finished_at = time.time()
        _warm_in_progress = False


def _start_background_warmup() -> bool:
    """Start warmup thread if not already warming or loaded."""
    if ts.is_model_loaded(DEFAULT_MODEL_NAME) or _warm_in_progress:
        return False
    threading.Thread(target=_warm_model_sync, daemon=True).start()
    return True


@app.exception_handler(HTTPException)
async def _http_exception_handler(_request: Request, exc: HTTPException):
    detail = exc.detail
    if isinstance(detail, dict) and "error" in detail:
        payload = detail
    else:
        payload = _error_payload("http_error", str(detail))
    return JSONResponse(status_code=exc.status_code, content=payload)


@app.exception_handler(Exception)
async def _unhandled_exception_handler(_request: Request, exc: Exception):
    logger.exception("Unhandled server error: %s", exc)
    return JSONResponse(
        status_code=500,
        content=_error_payload("internal_error", "Unexpected server error.", {"hint": "Check server logs."}),
    )


@app.get("/health", response_model=str)
def health():
    """Simple health text endpoint for backward compatibility."""
    return f"Whisper Flow V{__version__}"


@app.get("/v1/health", response_model=dict)
def health_v1():
    """Structured health endpoint for apps."""
    return {
        "ok": True,
        "service": SERVICE_NAME,
        "version": __version__,
        "auth_required": AUTH_REQUIRED,
        "model": {
            "default": DEFAULT_MODEL_NAME,
            "loaded": ts.is_model_loaded(DEFAULT_MODEL_NAME),
            "warming": _warm_in_progress,
            "last_error": _warm_last_error or None,
        },
        "uptime_s": round(time.time() - BOOT_TIME, 3),
    }


@app.get("/v1/wake", response_model=dict, dependencies=[Depends(require_api_key)])
@app.post("/v1/wake", response_model=dict, dependencies=[Depends(require_api_key)])
def wake_service(wait: bool = Query(default=False, description="When true, block until model is loaded.")):
    """
    Wake endpoint for free-tier cold starts.
    Call this from client app on load to pre-warm model.
    """
    started = False
    begin = time.time()

    if wait:
        _warm_model_sync()
    else:
        started = _start_background_warmup()

    return {
        "ok": True,
        "service": SERVICE_NAME,
        "model": {
            "default": DEFAULT_MODEL_NAME,
            "loaded": ts.is_model_loaded(DEFAULT_MODEL_NAME),
            "warming": _warm_in_progress,
            "warmup_started": started,
            "last_error": _warm_last_error or None,
        },
        "timing": {
            "elapsed_ms": int((time.time() - begin) * 1000),
            "warm_started_at": _warm_started_at or None,
            "warm_finished_at": _warm_finished_at or None,
        },
    }


def _transcribe_pcm_impl(model_name: str, files: List[UploadFile]) -> dict:
    if not files:
        raise HTTPException(
            status_code=400,
            detail=_error_payload("bad_request", "No audio file uploaded."),
        )

    content = files[0].file.read()
    if not content:
        raise HTTPException(
            status_code=400,
            detail=_error_payload("empty_audio", "Uploaded audio is empty."),
        )
    if len(content) > MAX_AUDIO_BYTES:
        raise HTTPException(
            status_code=413,
            detail=_error_payload(
                "audio_too_large",
                "Audio payload exceeds configured limit.",
                {"max_bytes": MAX_AUDIO_BYTES, "received_bytes": len(content)},
            ),
        )

    selected_model = (model_name or DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
    try:
        model = ts.get_model(selected_model)
        result = ts.transcribe_pcm_chunks(model, [content])
    except HTTPException:
        raise
    except Exception as exc:
        logger.exception("Transcription failed: %s", exc)
        raise HTTPException(
            status_code=500,
            detail=_error_payload(
                "transcription_failed",
                "Transcription failed.",
                {"reason": str(exc)},
            ),
        ) from exc

    return {
        "ok": True,
        "model": selected_model,
        "result": result,
    }


@app.post("/transcribe_pcm_chunk", response_model=dict, dependencies=[Depends(require_api_key)])
def transcribe_pcm_chunk(model_name: str = Form(DEFAULT_MODEL_NAME), files: List[UploadFile] = File(...)):
    """Backward-compatible route with protected access when API key is set."""
    payload = _transcribe_pcm_impl(model_name, files)
    # Backward compatibility: old clients expect plain whisper result dict.
    return payload["result"]


@app.post("/v1/transcribe/pcm", response_model=dict, dependencies=[Depends(require_api_key)])
def transcribe_pcm_chunk_v1(model_name: str = Form(DEFAULT_MODEL_NAME), files: List[UploadFile] = File(...)):
    """Versioned route with structured response + robust errors."""
    return _transcribe_pcm_impl(model_name, files)


async def _run_ws_session(websocket: WebSocket) -> None:
    """Shared websocket session handler for /ws and /v1/ws."""
    model_name = (websocket.query_params.get("model_name") or DEFAULT_MODEL_NAME).strip() or DEFAULT_MODEL_NAME
    if len(sessions) >= MAX_ACTIVE_WS_SESSIONS:
        await websocket.accept()
        await websocket.send_json(
            _error_payload(
                "server_busy",
                "Too many active transcription sessions. Please retry shortly.",
                {"max_active_sessions": MAX_ACTIVE_WS_SESSIONS},
            )
        )
        await websocket.close(code=1013)
        return

    model = ts.get_model(model_name)
    session = None

    async def transcribe_async(chunks: list):
        return await ts.transcribe_pcm_chunks_async(model, chunks)

    async def send_back_async(data: dict):
        await websocket.send_json(data)

    try:
        await websocket.accept()
        session = st.TranscribeSession(transcribe_async, send_back_async)
        sessions[str(session.id)] = session

        while True:
            data = await websocket.receive_bytes()
            if len(data) > MAX_AUDIO_BYTES:
                await websocket.send_json(
                    _error_payload(
                        "audio_too_large",
                        "Chunk exceeds configured size limit.",
                        {"max_bytes": MAX_AUDIO_BYTES, "received_bytes": len(data)},
                    )
                )
                continue
            session.add_chunk(data)
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover
        logger.exception("WebSocket session failed: %s", exc)
        if websocket.client_state.name != "DISCONNECTED":
            await websocket.send_json(
                _error_payload("ws_session_error", "WebSocket transcription failed.", {"reason": str(exc)})
            )
            await websocket.close(code=1011)
    finally:
        if session:
            try:
                await session.stop()
            finally:
                sessions.pop(str(session.id), None)


@app.websocket("/ws")
async def websocket_endpoint(websocket: WebSocket):
    """Backward-compatible WS route."""
    if not await _authorize_websocket(websocket):
        return
    await _run_ws_session(websocket)


@app.websocket("/v1/ws")
async def websocket_endpoint_v1(websocket: WebSocket):
    """Versioned WS route with API key support."""
    if not await _authorize_websocket(websocket):
        return
    await _run_ws_session(websocket)
