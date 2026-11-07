# ais-voice — Phase 2 voice service

Separate service from Phase 1 (`~/workspace/ais-autonomous-sales/`).
Phase 1 is NEVER written to from here: it is only read over HTTP
(`app/phase1.py`, GET `/api/health`, `/api/opt-outs`,
`/api/history/<id>`). Phase 1's dashboard (port 7860) is left running.

## Compliance gates — `app/gates.py`

`check_eligibility(session)` -> `{"allowed", "reasons", "checks"}`.
One failed gate -> no dial. Every check is appended to
`data/audit_log.jsonl` (phone/session, time, allowed, reasons —
never conversation content).

Session: `{type, phone, clinic_timezone, clinic_state,
recording_enabled=False, recording_consent=False}`.
Times are timezone-aware (`zoneinfo`); `now` is injectable for tests.

### Two session types — the distinction

| | `outbound-dial` | `voice-browser` |
|---|---|---|
| What it is | A real telephone call | Voice test in the browser (Langkah 1) |
| Consent (`data/consent.json`) | REQUIRED | not applicable — no call happens |
| Phase 1 opt-out | REQUIRED | not applicable |
| DNC scrub | REQUIRED if a scrub file exists | not applicable |
| Calling hours | REQUIRED | not applicable |
| Dial cap 3x/24h | REQUIRED | not applicable |
| Kill switch | REQUIRED | REQUIRED |
| Recording off | REQUIRED | REQUIRED |

### Gates and reason codes (`outbound-dial`)

- `kill_switch_engaged` — `data/kill_switch.json` says engaged.
- `recording_without_consent` — recording on without explicit
  `recording_consent=True`. Default is recording OFF, so a dial
  without recording consent still passes this gate — but recording
  stays off.
- `missing_phone` / `invalid_timezone` — fail closed.
- `no_consent` — phone not in `data/consent.json` (starts empty: `[]`).
- `opted_out` — phone matches a Phase 1 opt-out (HTTP).
- `phase1_unreachable` — Phase 1 down -> dial refused (fail closed).
- `dnc_national` / `dnc_state` — phone on a loaded scrub list.
- `dnc_scrub_stale` — a loaded scrub file is older than 31 days.
- `outside_calling_hours` — clinic-local time outside 08:00–21:00
  federal, 08:00–20:00 in Florida.
- `dial_cap_exceeded` — already 3 dials to this phone in 24h.

DNC: only REAL scrub files (`data/dnc_national.json`,
`data/dnc_state.json`) are ever loaded. The `*.example.json` files
are documentation only. No scrub file -> check recorded as `"n/a"`;
a real scrub (version <= 31 days) MUST be imported before any real dial.

### Kill switch — `app/killswitch.py`

`is_engaged()` / `engage(note)` / `disengage()` on
`data/kill_switch.json` (`{"engaged": false}` by default). Missing
file = not engaged; corrupt file = ENGAGED (fail closed). The gate
reads the file on every check, so engaging stops the very next dial.

### Phase 1 client — `app/phase1.py`

Read-only httpx client, 3s timeout, `trust_env=False` (localhost
must never go through a proxy). Any failure raises
`Phase1Unavailable`.

Limitation to know: Phase 1's `opt_outs` table is keyed by
`session_id`, not phone. `is_opted_out()` matches the phone against
those session_ids after normalization, so a phone only matches when
the opting-out session was keyed by that phone. The dialer must use
the phone number as the Phase 1 session_id when recording opt-outs.

## Data files (all fictional numbers — 555-0100 range, never real)

- `data/consent.json` — `[]` (nobody passes without consent)
- `data/consent.example.json` — 1 fictional consented entry
- `data/dnc_national.example.json`, `data/dnc_state.example.json` — format + example
- `data/dial_log.json` — `[]`
- `data/kill_switch.json` — `{"engaged": false}`
- `data/audit_log.jsonl` — append-only gate audit log

## Run the gate tests

```bash
cd ~/workspace/ais-voice
.venv/bin/python -m pytest tests/test_gates.py tests/test_phase1.py -v
.venv/bin/python -m ruff check app/gates.py app/phase1.py app/killswitch.py tests/test_gates.py tests/test_phase1.py
.venv/bin/python -m ruff format --check app/gates.py app/phase1.py app/killswitch.py tests/test_gates.py tests/test_phase1.py
```

38 tests, all passing (7 Nov 2026). The 10 mixed test numbers from
plan §4 Langkah 2 are `test_ten_mixed_numbers_all_rejected` in
`tests/test_gates.py` — each refused with its correct reason
(no consent, opted out, 22:00 local, Florida 20:30, cap 3x/24h,
kill switch, Phase 1 down, recording without consent, missing
phone, DNC listed) — plus 1 consented number in-hours allowed
(`test_one_consented_number_in_hours_allowed`).

Phase 1 must be running on 127.0.0.1:7860 for the 2 live tests in
`tests/test_phase1.py`; they skip if it is down. Do NOT stop it.

## Voice pipeline — `app/pipeline.py`, `app/prompt.py`, `app/config.py`

Pipecat 1.15.0 service (separate venv from Phase 1, anthropic 1.x here):

- Transport: `SmallWebRTCTransport` (`pipecat.transports.smallwebrtc.transport`) — browser, no Daily API key.
- VAD / turn-taking: `SileroVADAnalyzer` (`pipecat.audio.vad.silero`, model bundled locally, no download) wired into `LLMUserAggregatorParams`; `PipelineWorker` turn tracking on, so user speech interrupts TTS (barge-in).
- STT: `DeepgramSTTService` (`pipecat.services.deepgram.stt`), model `nova-3`.
- LLM: `AnthropicLLMService` (`pipecat.services.anthropic.llm`), model `claude-sonnet-5-5`.
- TTS: `CartesiaTTSService` (`pipecat.services.cartesia.tts`), Sonic 3 family (default model `sonic-3.6`).
- Pipeline: `Pipeline` (`pipecat.pipeline.pipeline`) + `PipelineWorker` (`pipecat.pipeline.worker`; `PipelineTask` is a deprecated alias in 1.15, not used) + `PipelineParams`.
- Prompt: `build_voice_prompt(settings)` adapts the Phase 1 text prompt for voice — short sentences, one question per turn, AI disclosure in the first sentence, price only from settings (`$1,000 one-time`), no invented portfolio/testimonials/guarantees, separate voicemail script (max ~20 s). `start_greeting_text(settings)` returns the opening disclosure line.

### Install

```bash
cd ~/workspace/ais-voice
~/.local/bin/uv sync --python 3.12 --extra dev   # exit 0, writes uv.lock
cp .env.example .env   # then fill in keys (never commit .env)
```

### Live voice needs these keys (all empty by default)

| Key | Service | Notes |
|---|---|---|
| `ANTHROPIC_API_KEY` | Claude Sonnet 5.5 (LLM) | Pay-as-you-go; set a spend limit first |
| `DEEPGRAM_API_KEY` | Deepgram Nova-3 (STT) | Claim the $200 free credit at signup |
| `CARTESIA_API_KEY` | Cartesia Sonic 3 (TTS) | Pay-as-you-go |
| `CARTESIA_VOICE_ID` | Cartesia voice | Pick a voice in the Cartesia dashboard; no default voice is invented |

Without a key, the live factory raises `ValueError: missing API key: ...` instead of calling any API. With fake services (`app/fakes.py`: `FakeSTT`/`FakeTTS`/`FakeLLM`) the pipeline builds offline with no keys and no network.

### Run the pipeline tests

```bash
cd ~/workspace/ais-voice
.venv/bin/python -m pytest tests/test_config.py tests/test_prompt.py tests/test_pipeline.py -v
.venv/bin/python -m ruff check app/config.py app/prompt.py app/pipeline.py app/fakes.py tests/test_config.py tests/test_prompt.py tests/test_pipeline.py
```

16 tests, all passing (7 Nov 2026). Recording is OFF by default (`RECORDING_ENABLED=false`) — CA + FL are all-party consent states.

## Voice browser — `app/server.py` (port 7861)

Browser voice test for Langkah 1 (no telephone call, no dialling, no
audio recording). Page: http://127.0.0.1:7861/

### Run

```bash
cd ~/workspace/ais-voice
./start-server.sh            # uvicorn app.server:app, host 127.0.0.1, port 7861
# stop: pkill -f "uvicorn app.server:app"  (does NOT touch port 7860)
```

The server starts WITHOUT any API key (demo mode). Live voice needs all
four keys in `ais-voice/.env` (see table above).

### What you can test WITHOUT keys (demo mode)

- `GET /` — page loads: Start/Stop mic buttons, AI disclosure, the
  "Simulation — no telephone call" warning, live transcript area.
- `GET /api/health` → `{"status":"ok","mode":"voice-browser","phase":2,"recording":false}`
- `GET /api/status` → `state: idle`, `live_ready: false`,
  `demo_reason: "missing in ais-voice/.env: ..."`, gates
  (`recording_off: true`, `dial_enabled: false`).
- `GET /api/transcript` → `[]` (empty until a session runs).
- `POST /api/start` and `POST /api/offer` → `503` demo (never a fake
  SDP answer, and no orphaned peer connection blocks the next offer).
- `POST /api/stop` → kill switch: stops the session AND engages the
  shared kill switch (`data/kill_switch.json`).

### What needs keys (live mode)

With all four keys set, the page's Start mic does a real SmallWebRTC
handshake (`POST /api/offer` → SDP answer, `PATCH /api/offer` → ICE
candidates), the pipeline runs (Deepgram STT → Sonnet 5.5 → Cartesia
TTS, Silero VAD barge-in), the greeting (AI disclosure) is spoken
first and saved to the transcript, and `GET /api/transcript` shows
completed turns (polled by the page every 2 s).

Verified 7 Nov 2026 with an aiortc stand-in client + fake keys:
offer → 200 answer, remote audio track received, pipeline linked and
started, status `active`, greeting in transcript, stop → `idle`.
NOT verified live (needs real keys + a real browser/mic): spoken TTS
audio, real-speech STT, LLM replies, barge-in, end-to-end latency.

### Endpoints

| Method | Path | Notes |
|---|---|---|
| GET | `/` | Voice page (`app/templates/voice.html`) |
| GET | `/static/voice.js` | Plain RTCPeerConnection client (no npm build) |
| GET | `/api/health` | `ok` / `voice-browser` / phase 2 / `recording: false` |
| GET | `/api/status` | state, `live_ready`, `demo_reason`, gates |
| GET | `/api/transcript` | `[{role, text, created_at}]` from `data/transcript.jsonl` |
| POST | `/api/start` | 200 when live-ready, else 503 demo |
| POST | `/api/stop` | Kill switch (stop session + engage kill switch) |
| POST | `/api/offer` | SmallWebRTC SDP offer → SDP answer |
| PATCH | `/api/offer` | SmallWebRTC ICE candidates |

There is NO recording endpoint by design (`/api/record`, `/api/audio`
→ 404). Transcript is text-only, append-only JSONL.

### Re-enable after a stop

`POST /api/stop` engages the shared kill switch, so the next start is
refused with 503 until it is disengaged:

```bash
cd ~/workspace/ais-voice
.venv/bin/python -c "from app.killswitch import disengage; disengage()"
```

### Known environment quirk (fixed)

The VM's `no_proxy` env contains bracketed IPv6 (`[::1]`), which httpx
(Anthropic SDK) cannot parse — service construction raised `InvalidURL`
before any key was used. `app/server.py` strips bracketed entries at
import and `start-server.sh` strips them too (plain `::1` already
covers the same host). Regression test: `test_sanitize_no_proxy_drops_bracketed_ipv6`.
