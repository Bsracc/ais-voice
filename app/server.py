"""AIS Phase 2 voice-browser server (FastAPI, port 7861, host 127.0.0.1).

Browser voice test for Langkah 1 of PHASE2_PLAN.md — NOT a telephone
call. No dialling happens here, no audio is recorded, and the server
starts WITHOUT any API key (demo mode: page + status + transcript work,
live voice stays unavailable until the pipeline + keys exist).

Endpoints:
  GET  /               voice-browser page (Start/Stop mic, live transcript)
  GET  /api/health     {"status":"ok","mode":"voice-browser","phase":2,"recording":false}
  GET  /api/status     pipeline state + live-ready + compliance gates (recording=false)
  GET  /api/transcript transcript entries {role, text, created_at}
  POST /api/start      probe: 200 when live voice is ready, else 503 demo
  POST /api/stop       kill switch: stop session + engage shared kill switch
  POST /api/offer      SmallWebRTC SDP offer  -> SDP answer (pipecat-ai 1.15)
  PATCH /api/offer     SmallWebRTC ICE candidates
  WS   /ws/voice       WebSocket voice session (raw PCM16: 16 kHz in, 24 kHz
                       out) — TCP alternative next to WebRTC, for networks
                       where UDP is blocked. One session at a time across
                       both transports.

WebRTC signalling follows the installed pipecat-ai 1.15 code exactly
(pipecat/transports/smallwebrtc/request_handler.py +
pipecat/runner/run.py::_setup_webrtc_routes): the client POSTs
{"sdp","type","pc_id"?} to /api/offer and PATCHes
{"pc_id","candidates":[{candidate,sdp_mid,sdp_mline_index}]} to the same
URL. The browser client is plain RTCPeerConnection (app/static/voice.js)
— no npm build step — and only opens a data channel + "ping" keepalive
to mirror the reference @pipecat-ai/small-webrtc-transport client.

Pipeline integration (app/pipeline.py, owned by the pipeline side):
  build_pipeline(settings, stt=None, tts=None, llm=None, transport=None)
      -> VoicePipeline(.pipeline, .worker, .context, .transport, ...)
  The server builds a SmallWebRTCTransport from the offer handshake's
  SmallWebRTCConnection, passes it as ``transport``, runs
  ``built.worker`` via PipelineRunner, and queues the greeting
  (start_greeting_text — AI disclosure first) as a TextFrame.
  Transcript comes from polling ``built.context.messages`` (LLMContext):
  user/assistant turns are appended to the store as they complete.
  Live factories raise ValueError without keys, so the server pre-checks
  keys (_live_check) BEFORE creating any peer connection — demo mode
  answers 503 and never returns a fake SDP answer.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException, WebSocket
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app import killswitch
from app.transcript import append_transcript, get_transcript
from app.transcript import configure as configure_transcript


def _sanitize_no_proxy() -> None:
    """Drop bracketed IPv6 entries (e.g. "[::1]") from no_proxy/NO_PROXY.

    The VM's proxy env lists both "::1" and "[::1]". httpx (used by the
    Anthropic SDK) cannot parse a bracketed entry as a URL pattern and
    raises InvalidURL at CLIENT CONSTRUCTION time — i.e. build_pipeline
    fails before any key is used or any network call happens. The plain
    "::1" entry already covers the same host, so dropping the bracketed
    duplicates is semantics-preserving. Runs at import so every entry
    point (start-server.sh, plain uvicorn) gets the fix.
    """
    for name in ("no_proxy", "NO_PROXY"):
        value = os.environ.get(name)
        if not value:
            continue
        parts = [part.strip() for part in value.split(",")]
        cleaned = [p for p in parts if not (p.startswith("[") and p.endswith("]"))]
        if len(cleaned) != len(parts):
            os.environ[name] = ",".join(cleaned)


_sanitize_no_proxy()

try:
    from app.config import Settings
    from app.pipeline import build_pipeline, start_greeting_text

    PIPELINE_AVAILABLE = True
except ImportError:  # app/pipeline.py or app/config.py not built yet.
    Settings = None  # type: ignore[assignment]
    build_pipeline = None  # type: ignore[assignment]
    start_greeting_text = None  # type: ignore[assignment]
    PIPELINE_AVAILABLE = False

try:
    # Built by worker A (do not recreate here): the manual operator queue
    # for muse-operator mode. Until it lands, `operator` is None and the
    # operator endpoints answer 503 — everything else keeps working.
    from app import operator
except ImportError:
    operator = None  # type: ignore[assignment]

# Live voice needs all of these (ais-voice/.env). No key is ever read
# from chat or from Phase 1's .env.
REQUIRED_LIVE_KEYS = (
    "ANTHROPIC_API_KEY",
    "DEEPGRAM_API_KEY",
    "CARTESIA_API_KEY",
    "CARTESIA_VOICE_ID",
)

# muse-operator mode needs STT + TTS only: there is no LLM, so no
# Anthropic key is required for live voice in that mode.
OPERATOR_LIVE_KEYS = (
    "DEEPGRAM_API_KEY",
    "CARTESIA_API_KEY",
    "CARTESIA_VOICE_ID",
)

# Valid BRAIN_MODE values. Anything else is a configuration error.
VALID_BRAIN_MODES = ("anthropic", "muse-operator")

BASE_DIR = Path(__file__).resolve().parents[1]
TRANSCRIPT_PATH = BASE_DIR / "data" / "transcript.jsonl"
OPERATOR_PATH = BASE_DIR / "data" / "operator_pending.json"

# Kept as module attributes so tests can point the store / kill switch at
# temp files instead of the real data/ dir.
transcript_store: Path | None = None
KILL_SWITCH_PATH: Path | None = None

_state: dict = {"state": "idle", "pc_id": None, "transport": None}
_session_task: asyncio.Task | None = None
_watcher_task: asyncio.Task | None = None
_active_worker = None  # pipeline worker of the live session, if any.

_webrtc_handler = None  # SmallWebRTCRequestHandler, created lazily.


def _get_webrtc_handler():
    """Single-connection handler (one browser session at a time)."""
    global _webrtc_handler
    if _webrtc_handler is None:
        from pipecat.transports.smallwebrtc.request_handler import (
            ConnectionMode,
            SmallWebRTCRequestHandler,
        )

        # STUN so a remote browser (phone, via tunnel) can find a path to
        # this VM. Localhost still works; without STUN only host candidates
        # exist and a remote peer cannot reach the VM behind NAT.
        _webrtc_handler = SmallWebRTCRequestHandler(
            ice_servers=["stun:stun.l.google.com:19302"],
            connection_mode=ConnectionMode.SINGLE,
        )
    return _webrtc_handler


def _settings_brain_mode(settings) -> str:
    """BRAIN_MODE from an already-built Settings.

    Reads the BRAIN_MODE field when app/config.py provides it (worker A
    owns that file); until then, falls back to the BRAIN_MODE environment
    variable, defaulting to "anthropic".
    """
    value = getattr(settings, "BRAIN_MODE", None)
    if value is None:
        value = os.environ.get("BRAIN_MODE", "anthropic")
    return str(value).strip().lower() or "anthropic"


def _brain_mode() -> str:
    """Effective BRAIN_MODE, or "unknown" when Settings cannot be built."""
    if not PIPELINE_AVAILABLE:
        return "unknown"
    try:
        settings = Settings()
    except Exception:  # noqa: BLE001 — status must never crash.
        return "unknown"
    return _settings_brain_mode(settings)


def _live_check() -> tuple[bool, str]:
    """Is live voice actually possible? (module + keys present for the
    active BRAIN_MODE: "anthropic" needs all four keys, "muse-operator"
    needs STT + TTS only, no Anthropic key.)"""
    if not PIPELINE_AVAILABLE:
        return False, "app/pipeline.py not built yet"
    try:
        settings = Settings()
    except Exception as exc:  # noqa: BLE001 — settings must never crash status.
        return False, f"settings error: {exc}"
    mode = _settings_brain_mode(settings)
    if mode not in VALID_BRAIN_MODES:
        return False, (
            f"invalid BRAIN_MODE {mode!r} "
            f"(expected 'anthropic' or 'muse-operator')"
        )
    required = REQUIRED_LIVE_KEYS if mode == "anthropic" else OPERATOR_LIVE_KEYS
    missing = [key for key in required if not getattr(settings, key, "")]
    if missing:
        return False, "missing in ais-voice/.env: " + ", ".join(missing)
    return True, ""


def _require_not_killed() -> None:
    """Voice-browser gate: refuse to start when the kill switch is engaged
    (same rule as gates.check_eligibility for type="voice-browser")."""
    if killswitch.is_engaged(KILL_SWITCH_PATH):
        raise HTTPException(
            status_code=503,
            detail="kill switch engaged — run app.killswitch.disengage() to re-enable",
        )


def _require_live() -> None:
    """Refuse live voice in demo mode — before any peer connection exists."""
    ready, reason = _live_check()
    if not ready:
        raise HTTPException(
            status_code=503,
            detail=f"voice pipeline unavailable — demo mode ({reason})",
        )


def reset_state() -> None:
    """Reset session + transcript store (used by tests and on startup)."""
    global _session_task, _watcher_task, _active_worker, transcript_store
    _session_task = None
    _watcher_task = None
    _active_worker = None
    _state["state"] = "idle"
    _state["pc_id"] = None
    _state["transport"] = None
    transcript_store = TRANSCRIPT_PATH
    configure_transcript(TRANSCRIPT_PATH)
    if operator is not None:
        operator.configure(OPERATOR_PATH)


def status_payload() -> dict:
    """Status for /api/status. Gates are computed WITHOUT writing an audit
    entry — /api/status is polled, and gates.check_eligibility() audits
    every call; the voice-browser result is fully determined by the kill
    switch + recording-off anyway (see gates.py: voice-browser branch)."""
    engaged = killswitch.is_engaged(KILL_SWITCH_PATH)
    ready, reason = _live_check()
    try:
        pending_count = len(operator.list_pending()) if operator is not None else 0
    except Exception:  # noqa: BLE001 — status must never crash.
        pending_count = 0
    return {
        "state": _state["state"],
        "mode": "voice-browser",
        "phase": 2,
        "recording": False,
        "pipeline_available": PIPELINE_AVAILABLE,
        "live_ready": ready,
        "demo_reason": reason,
        "brain_mode": _brain_mode(),
        "pending_count": pending_count,
        "kill_switch_engaged": engaged,
        "pc_id": _state["pc_id"],
        "transport": _state.get("transport"),  # "webrtc" | "websocket" | None
        "gates": {
            "kill_switch": not engaged,
            "recording_off": True,
            "dial_enabled": False,
            # Not a phone call: consent / DNC / hours / cap do not apply.
            "consent": "n/a",
            "opt_out": "n/a",
            "dnc": "n/a",
            "calling_hours": "n/a",
            "dial_cap": "n/a",
        },
    }


def _message_text(message) -> tuple[str | None, str]:
    """(role, text) from one LLMContext message (dict or object, str or
    block-list content). Returns ("system", "") for anything unusable."""
    if isinstance(message, dict):
        role = message.get("role")
        content = message.get("content")
    else:
        role = getattr(message, "role", None)
        content = getattr(message, "content", None)
    if isinstance(content, list):
        text = "".join(
            block.get("text", "")
            for block in content
            if isinstance(block, dict) and block.get("type") == "text"
        )
    elif isinstance(content, str):
        text = content
    else:
        text = ""
    return (str(role) if role else None), text.strip()


async def _watch_transcript(
    context, greeting: str, interval: float = 1.0, worker=None, operator_mode: bool = False
) -> None:
    """Poll the pipeline's LLMContext and append completed turns to the
    transcript store. The greeting is appended directly in start_session,
    so context copies of the exact greeting text are skipped here.

    In muse-operator mode (operator_mode=True) there is no LLM: each new
    user turn is also queued as an operator turn (operator.create_turn)
    and, when a worker is given, the holding reply is spoken back via
    TextFrame(operator.HOLDING_TEXT). Assistant entries are appended only
    when the transcript does not already end with the same entry — the
    operator reply endpoint appends directly, and the same text may
    surface again via context; without this dedupe it would appear twice.
    """
    from pipecat.frames.frames import TextFrame

    seen = 0
    while _state["state"] == "active":
        await asyncio.sleep(interval)
        try:
            messages = list(context.messages)
        except Exception:  # noqa: BLE001 — a dead context ends the watch.
            return
        for message in messages[seen:]:
            role, text = _message_text(message)
            if not text or role not in ("user", "assistant"):
                continue
            if text == greeting:
                continue
            if role == "user":
                append_transcript("user", text)
                if operator_mode and operator is not None:
                    operator.create_turn(text)
                    if worker is not None:
                        await worker.queue_frame(
                            TextFrame(text=operator.HOLDING_TEXT)
                        )
            else:
                entries = get_transcript()
                if (
                    entries
                    and entries[-1]["role"] == "ai"
                    and entries[-1]["text"] == text
                ):
                    continue
                append_transcript("ai", text)
        seen = len(messages)


async def _run_session(transport, *, transport_name: str, pc_id=None) -> asyncio.Task:
    """Build the pipeline on any transport and run it as a background task.

    Shared by the WebRTC session (start_session) and the WebSocket session
    (start_ws_session): same greeting (AI disclosure first), same transcript
    watcher, same operator-mode handling. Raises RuntimeError in demo mode
    (no keys) and when another session is already active.
    """
    global _session_task, _watcher_task, _active_worker
    ready, reason = _live_check()
    if not ready:
        raise RuntimeError(f"voice pipeline not live-ready: {reason}")
    if _state["state"] == "active":
        raise RuntimeError("a voice session is already active")
    from pipecat.frames.frames import TextFrame
    from pipecat.pipeline.runner import PipelineRunner

    settings = Settings()
    built = build_pipeline(settings, transport=transport)

    # Greeting first (AI disclosure in the first 5 seconds). The worker's
    # push queue buffers the frame until the pipeline runs; a plain
    # TextFrame flows through STT/aggregators/LLM untouched and is spoken
    # by TTS (verified against installed pipecat 1.15 source).
    greeting = start_greeting_text(settings)
    await built.worker.queue_frame(TextFrame(text=greeting))
    append_transcript("ai", greeting)

    runner = PipelineRunner()
    await runner.add_workers(built.worker)
    _state["state"] = "active"
    _state["pc_id"] = pc_id
    _state["transport"] = transport_name
    _active_worker = built.worker
    _session_task = asyncio.create_task(runner.run())
    _session_task.add_done_callback(_on_session_done)
    _watcher_task = asyncio.create_task(
        _watch_transcript(
            built.context,
            greeting,
            worker=built.worker,
            operator_mode=_brain_mode() == "muse-operator",
        )
    )
    return _session_task


async def start_session(connection) -> asyncio.Task:
    """Start the live voice pipeline for one WebRTC connection.

    Builds SmallWebRTCTransport(connection) + build_pipeline(settings,
    transport=...) from app/pipeline.py and runs built.worker as a
    background task. Raises RuntimeError in demo mode (no keys) — the
    offer endpoint pre-checks this, so the callback should not fail.
    The live check runs here too (before touching `connection`):
    _run_session checks again, but SmallWebRTCTransport's constructor
    already dereferences the connection, so a demo-mode call must fail
    before that with RuntimeError, not AttributeError.
    """
    ready, reason = _live_check()
    if not ready:
        raise RuntimeError(f"voice pipeline not live-ready: {reason}")
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    transport = SmallWebRTCTransport(
        connection,
        TransportParams(
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=24000,
            video_in_enabled=False,
            video_out_enabled=False,
        ),
    )
    return await _run_session(transport, transport_name="webrtc", pc_id=connection.pc_id)


async def start_ws_session(websocket: WebSocket) -> asyncio.Task:
    """Start the live voice pipeline for one WebSocket connection.

    Same pipeline as the WebRTC session, on a FastAPIWebsocketTransport
    with the raw-PCM serializer (app/ws_transport.py): binary PCM16 mono
    16 kHz in, 24 kHz out. The caller must have accepted the websocket
    already. On client disconnect the session task is cancelled, which
    ends the pipeline and frees the single-session slot.
    """
    from pipecat.transports.websocket.fastapi import (
        FastAPIWebsocketParams,
        FastAPIWebsocketTransport,
    )

    from app.ws_transport import RawPCMSerializer

    transport = FastAPIWebsocketTransport(
        websocket,
        FastAPIWebsocketParams(
            serializer=RawPCMSerializer(),
            allowed_origins=[],
            session_timeout=None,
            add_wav_header=False,
            # TransportParams defaults to audio in/out DISABLED: without
            # these flags the transport drops audio in both directions
            # (input push_audio_frame and output handle_audio_frame both
            # no-op). Rates match the pipeline worker (16 kHz in, 24 kHz
            # out) and the wire protocol in app/ws_transport.py.
            audio_in_enabled=True,
            audio_in_sample_rate=16000,
            audio_out_enabled=True,
            audio_out_sample_rate=24000,
        ),
    )

    @transport.event_handler("on_client_disconnected")
    async def on_client_disconnected(transport, client):
        # Cancel only — do NOT await stop_session() here: this handler runs
        # inside the transport's own receive task, and the runner's teardown
        # cancels that task, so awaiting the session task would deadlock
        # until cancel_task's 1s timeout. _on_session_done clears the
        # state/watcher when the cancelled task finishes.
        task = _session_task
        if task is not None and not task.done():
            task.cancel()

    return await _run_session(transport, transport_name="websocket")


def _on_session_done(task: asyncio.Task) -> None:
    """The pipeline ended (client disconnect, error, or stop): the session
    is idle again, and the transcript watcher stops with it."""
    global _watcher_task, _active_worker
    if _session_task is task:
        _state["state"] = "idle"
        _state["pc_id"] = None
        _state["transport"] = None
        _active_worker = None
    watcher, _watcher_task = _watcher_task, None
    if watcher is not None and not watcher.done():
        watcher.cancel()


async def stop_session() -> None:
    """Stop the live voice pipeline/session, if any. Idempotent."""
    global _session_task, _watcher_task, _active_worker
    _state["state"] = "idle"
    _state["pc_id"] = None
    _state["transport"] = None
    _active_worker = None
    watcher, _watcher_task = _watcher_task, None
    if watcher is not None and not watcher.done():
        watcher.cancel()
        try:
            await watcher
        except asyncio.CancelledError:
            pass
    task, _session_task = _session_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


@asynccontextmanager
async def lifespan(app: FastAPI):
    reset_state()
    yield
    await stop_session()
    if _webrtc_handler is not None:
        await _webrtc_handler.close()


app = FastAPI(title="AIS Phase 2 voice browser", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "app" / "static"), name="static")


@app.get("/")
async def index():
    return FileResponse(BASE_DIR / "app" / "templates" / "voice.html")


@app.get("/ws")
async def ws_index():
    """WebSocket-transport voice page (TCP, no WebRTC/UDP)."""
    return FileResponse(BASE_DIR / "app" / "templates" / "ws_voice.html")


@app.get("/api/health")
async def health():
    return {"status": "ok", "mode": "voice-browser", "phase": 2, "recording": False}


@app.get("/api/status")
async def status():
    return status_payload()


@app.get("/api/transcript")
async def transcript():
    return get_transcript()


def _require_operator() -> None:
    """The operator store (app/operator.py, built by worker A) must exist."""
    if operator is None:
        raise HTTPException(
            status_code=503,
            detail="operator store not built yet (app/operator.py)",
        )


@app.get("/api/pending")
async def pending_list():
    """Turns waiting for an operator reply (muse-operator mode)."""
    _require_operator()
    return operator.list_pending()


@app.get("/api/pending/{turn_id}")
async def pending_one(turn_id: str):
    """One operator turn, or 404 when unknown."""
    _require_operator()
    turn = operator.get_turn(turn_id)
    if turn is None:
        raise HTTPException(status_code=404, detail=f"unknown turn: {turn_id}")
    return turn


@app.post("/api/operator/reply")
async def operator_reply(request: dict):
    """Save a manual operator reply for one pending turn.

    Body: {"turn_id": str, "reply": str}. On success the reply is stored,
    appended to the transcript, and — when a voice session is active —
    spoken aloud to the caller via the live pipeline worker. When no
    session is active the reply is still stored (spoken=false).
    Unknown turn -> 404; already answered -> 409; empty reply -> 400.
    This endpoint never touches the gates or the kill switch.
    """
    _require_operator()
    turn_id = request.get("turn_id")
    reply = request.get("reply")
    turn = operator.get_turn(turn_id) if isinstance(turn_id, str) else None
    if turn is None:
        raise HTTPException(status_code=404, detail=f"unknown turn: {turn_id}")
    if not isinstance(reply, str) or not reply.strip():
        raise HTTPException(
            status_code=400, detail="reply must be a non-empty string"
        )
    clean = reply.strip()
    if not operator.save_operator_reply(turn_id, clean):
        raise HTTPException(
            status_code=409, detail=f"turn already answered: {turn_id}"
        )
    append_transcript("ai", clean)
    spoken = False
    if _state["state"] == "active" and _active_worker is not None:
        from pipecat.frames.frames import TextFrame

        await _active_worker.queue_frame(TextFrame(text=clean))
        spoken = True
    return {"status": "answered", "turn_id": turn_id, "spoken": spoken}


@app.post("/api/start")
async def start():
    """Probe the live pipeline. The real start is the WebRTC offer
    (POST /api/offer); this endpoint only reports whether a live session
    is possible at all, without crashing in demo mode."""
    _require_not_killed()
    _require_live()
    return {"status": "ready", "mode": "voice-browser", "phase": 2, "recording": False}


@app.post("/api/stop")
async def stop():
    """Kill switch: stop the voice session AND engage the shared kill
    switch (data/kill_switch.json), so no dial can start afterwards
    until it is disengaged (app.killswitch.disengage)."""
    await stop_session()
    if _webrtc_handler is not None:
        await _webrtc_handler.close()
    killswitch.engage(KILL_SWITCH_PATH, note="stopped from voice-browser /api/stop")
    return {"status": "stopped", "state": "idle", "kill_switch_engaged": True}


@app.post("/api/offer")
async def offer(request: dict):
    """SmallWebRTC offer (see module docstring for the wire format).

    Demo mode answers 503 BEFORE any peer connection is created — never
    a fake SDP answer, and no orphaned connection blocks the next offer.
    """
    _require_not_killed()
    _require_live()
    if _state["state"] == "active" and _state.get("transport") == "websocket":
        # One session at a time: a live WebSocket session owns the pipeline.
        raise HTTPException(
            status_code=503,
            detail="a WebSocket voice session is active",
        )
    from pipecat.transports.smallwebrtc.request_handler import SmallWebRTCRequest

    handler = _get_webrtc_handler()
    webrtc_request = SmallWebRTCRequest.from_dict(dict(request))

    async def webrtc_connection_callback(connection):
        try:
            await start_session(connection)
        except Exception:
            # Never leave an orphaned peer connection behind: in SINGLE
            # mode it would block every later offer with a 400.
            await connection.disconnect()
            raise

    return await handler.handle_web_request(
        request=webrtc_request,
        webrtc_connection_callback=webrtc_connection_callback,
    )


@app.patch("/api/offer")
async def ice_candidate(request: dict):
    """SmallWebRTC ICE candidates for the pc_id from POST /api/offer."""
    _require_live()
    from pipecat.transports.smallwebrtc.request_handler import (
        IceCandidate,
        SmallWebRTCPatchRequest,
    )

    handler = _get_webrtc_handler()
    patch = SmallWebRTCPatchRequest(
        pc_id=request["pc_id"],
        candidates=[IceCandidate(**c) for c in request.get("candidates", [])],
    )
    await handler.handle_patch_request(patch)
    return {"status": "success"}


@app.websocket("/ws/voice")
async def ws_voice(websocket: WebSocket):
    """WebSocket voice session (raw PCM over TCP, no WebRTC/UDP).

    Wire protocol — see app/ws_transport.py: binary PCM16 mono 16 kHz
    in, 24 kHz out; text messages are ignored. Same pipeline, greeting
    and operator-mode watcher as the WebRTC session; one session at a
    time across both transports. Rejections close the socket with 1013
    (try again later): kill switch engaged, demo mode (no keys), or
    another session already active. No fake pipeline is ever built.
    """
    try:
        _require_not_killed()
        _require_live()
    except HTTPException:
        await websocket.close(code=1013)
        return
    if _state["state"] == "active":
        await websocket.close(code=1013)
        return
    await websocket.accept()
    try:
        task = await start_ws_session(websocket)
    except Exception:  # noqa: BLE001 — a failed start must not hang the socket.
        await websocket.close(code=1011)
        return
    try:
        await task
    except asyncio.CancelledError:
        pass


if __name__ == "__main__":
    import uvicorn

    # HOST/PORT come from the environment (HF Spaces sets PORT=7860 and
    # needs 0.0.0.0); local default stays 127.0.0.1:7861.
    uvicorn.run(
        app,
        host=os.environ.get("HOST", "127.0.0.1"),
        port=int(os.environ.get("PORT", "7861")),
    )
