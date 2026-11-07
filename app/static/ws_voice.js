/* AIS Phase 2 voice browser — WebSocket (TCP) client.
 *
 * Same page contract as the WebRTC client (app/static/voice.js) but the
 * audio path is a plain WebSocket instead of RTCPeerConnection, because
 * this VM blocks UDP entirely and WebRTC cannot stream audio here.
 *
 * Wire protocol (agreed with the server, app/server.py /ws/voice):
 *   Client -> server: BINARY raw PCM16 mono 16 kHz (Int16 little-endian).
 *   Server -> client: BINARY raw PCM16 mono 24 kHz (Int16 little-endian).
 *   Either side may send TEXT JSON; unknown message types are ignored.
 *   Client sends {"type":"end"} when Stop is pressed.
 *
 * Mic capture: getUserMedia (audio only) -> AudioContext -> resample to
 * 16 kHz mono. AudioWorklet (via a Blob URL, no extra file) is tried
 * first; ScriptProcessorNode is the fallback for older Chrome/Android.
 * Resampling is mandatory: phone mics usually run at 48 kHz.
 *
 * Playback: incoming 24 kHz PCM16 chunks are converted to Float32 and
 * scheduled back-to-back with AudioBufferSourceNode on one shared
 * AudioContext, so playback is continuous instead of choppy per-chunk
 * starts.
 *
 * Transcript is NOT read from the socket: the page polls
 * GET /api/transcript every 2 seconds (server-side transcript, text
 * only). The same tick polls GET /api/status to show the brain mode and,
 * in muse-operator mode, a "waiting for operator" status while a turn
 * is queued. No audio is recorded or uploaded anywhere. No npm build
 * step — vanilla JS only.
 */
(function () {
  "use strict";

  var IN_RATE = 16000;   // mic -> server: PCM16 mono 16 kHz
  var OUT_RATE = 24000;  // server -> mic: PCM16 mono 24 kHz

  var statusEl = document.getElementById("status");
  var brainModeEl = document.getElementById("brain-mode");
  var errorEl = document.getElementById("error");
  var startBtn = document.getElementById("mic-start");
  var stopBtn = document.getElementById("mic-stop");
  var hintEl = document.getElementById("hint");
  var transcriptEl = document.getElementById("transcript");

  var ws = null;
  var micStream = null;
  var audioCtx = null;
  var sourceNode = null;
  var captureNode = null;
  var workletBlobUrl = null;
  var pollTimer = null;
  var seenCount = 0;
  var currentState = "idle";
  var brainMode = "anthropic";
  var pendingCount = 0;
  var savedStatusText = null; // connection status, restored once no turn is pending
  var stopping = false;       // true while Stop is tearing down on purpose
  var nextPlayTime = 0;       // scheduled end of the playback queue

  function setStatus(state, note) {
    currentState = state;
    statusEl.textContent = "Status: " + state + (note ? " — " + note : "");
  }

  function setButtons(running) {
    startBtn.disabled = running;
    stopBtn.disabled = !running;
  }

  function showError(message) {
    errorEl.textContent = message;
    errorEl.style.display = "block";
  }

  function clearError() {
    errorEl.textContent = "";
    errorEl.style.display = "none";
  }

  function wsUrl() {
    var scheme = window.location.protocol === "https:" ? "wss://" : "ws://";
    return scheme + window.location.host + "/ws/voice";
  }

  /* ---------- resampling (shared by both capture paths) ---------- */

  // Linear-interpolation resample of one Float32 mono block to Int16 at
  // IN_RATE. `state` persists across blocks ({buf: [], readPos: 0}).
  function resampleBlock(input, inputRate, state) {
    var ratio = inputRate / IN_RATE;
    var i;
    for (i = 0; i < input.length; i++) state.buf.push(input[i]);
    var out = new Int16Array(Math.ceil(state.buf.length / ratio) + 1);
    var n = 0;
    while (state.readPos + 1 < state.buf.length) {
      var idx = Math.floor(state.readPos);
      var frac = state.readPos - idx;
      var s = state.buf[idx] * (1 - frac) + state.buf[idx + 1] * frac;
      var v = Math.round(s * 32767);
      if (v > 32767) v = 32767;
      if (v < -32768) v = -32768;
      out[n++] = v;
      state.readPos += ratio;
    }
    var drop = Math.floor(state.readPos);
    if (drop > 0) {
      state.buf.splice(0, drop);
      state.readPos -= drop;
    }
    return out.subarray(0, n);
  }

  // AudioWorklet processor source (same resample logic, runs off the
  // main thread). Output is silence — the node is only a capture tap;
  // its output is routed to a zero-gain node so the graph stays pulled.
  var WORKLET_SOURCE = [
    "class Pcm16Resampler extends AudioWorkletProcessor {",
    "  constructor() {",
    "    super();",
    "    this.ratio = sampleRate / 16000;",
    "    this.buf = [];",
    "    this.readPos = 0;",
    "  }",
    "  process(inputs, outputs) {",
    "    var ch = inputs[0] && inputs[0][0];",
    "    if (ch) {",
    "      for (var i = 0; i < ch.length; i++) this.buf.push(ch[i]);",
    "    }",
    "    var out = outputs[0] && outputs[0][0];",
    "    if (out) { for (var k = 0; k < out.length; k++) out[k] = 0; }",
    "    var pcm = new Int16Array(Math.ceil(this.buf.length / this.ratio) + 1);",
    "    var n = 0;",
    "    while (this.readPos + 1 < this.buf.length) {",
    "      var idx = Math.floor(this.readPos);",
    "      var frac = this.readPos - idx;",
    "      var s = this.buf[idx] * (1 - frac) + this.buf[idx + 1] * frac;",
    "      var v = Math.round(s * 32767);",
    "      if (v > 32767) v = 32767;",
    "      if (v < -32768) v = -32768;",
    "      pcm[n++] = v;",
    "      this.readPos += this.ratio;",
    "    }",
    "    var drop = Math.floor(this.readPos);",
    "    if (drop > 0) { this.buf.splice(0, drop); this.readPos -= drop; }",
    "    if (n > 0) {",
    "      var bytes = pcm.slice(0, n);",
    "      this.port.postMessage(bytes.buffer, [bytes.buffer]);",
    "    }",
    "    return true;",
    "  }",
    "}",
    "registerProcessor('pcm16-resampler', Pcm16Resampler);",
    "",
  ].join("\n");

  /* ---------- playback: server PCM16 24 kHz -> scheduled audio ---------- */

  function schedulePlayback(int16) {
    if (!audioCtx || int16.length === 0) return;
    var floatBuf = new Float32Array(int16.length);
    for (var i = 0; i < int16.length; i++) floatBuf[i] = int16[i] / 32768;
    var buf = audioCtx.createBuffer(1, floatBuf.length, OUT_RATE);
    buf.getChannelData(0).set(floatBuf);
    var src = audioCtx.createBufferSource();
    src.buffer = buf;
    src.connect(audioCtx.destination);
    var now = audioCtx.currentTime;
    // If the queue has grown absurdly long (e.g. after a stall), drop
    // the backlog instead of playing minutes of stale audio.
    if (nextPlayTime < now || nextPlayTime - now > 10) nextPlayTime = now + 0.05;
    src.start(nextPlayTime);
    nextPlayTime += buf.duration;
  }

  /* ---------- transcript + status polling (same as WebRTC client) ---------- */

  function renderTranscript(entries) {
    if (!entries.length) return;
    transcriptEl.innerHTML = "";
    entries.forEach(function (entry) {
      var p = document.createElement("p");
      var who = document.createElement("strong");
      who.className = entry.role === "ai" ? "role-ai" : "role-user";
      who.textContent = entry.role === "ai" ? "AISIMA AI: " : "You: ";
      p.appendChild(who);
      p.appendChild(document.createTextNode(entry.text));
      transcriptEl.appendChild(p);
    });
    transcriptEl.scrollTop = transcriptEl.scrollHeight;
  }

  async function pollTranscript() {
    try {
      var res = await fetch("/api/transcript");
      if (!res.ok) return;
      var entries = await res.json();
      if (entries.length !== seenCount) {
        seenCount = entries.length;
        renderTranscript(entries);
      }
    } catch (e) { /* server down: keep last transcript, retry next tick */ }
  }

  // Poll /api/status every 2 seconds (same tick as the transcript poll):
  // show the brain mode, and — only while a session is active and a turn
  // is waiting — show "waiting for operator" instead of the connection
  // status. The connection status text is restored once nothing is pending.
  async function pollStatus() {
    try {
      var res = await fetch("/api/status");
      if (!res.ok) return;
      var st = await res.json();
      brainMode = st.brain_mode || "unknown";
      pendingCount = st.pending_count || 0;
      brainModeEl.textContent = "Brain mode: " + brainMode +
        (brainMode === "muse-operator"
          ? " — replies are queued for a human operator, not instant."
          : "");
      if (brainMode === "muse-operator" && pendingCount > 0 && currentState !== "idle") {
        if (savedStatusText === null) savedStatusText = statusEl.textContent;
        setStatus("waiting for operator", "your message is queued, an operator will reply shortly");
      } else if (savedStatusText !== null) {
        statusEl.textContent = savedStatusText;
        savedStatusText = null;
      }
    } catch (e) { /* server down: keep last status, retry next tick */ }
  }

  function startPolling() {
    if (pollTimer) return;
    pollTimer = setInterval(function () { pollTranscript(); pollStatus(); }, 2000);
    pollTranscript();
    pollStatus();
  }

  /* ---------- capture: mic -> resample -> WebSocket ---------- */

  function sendPcm(buffer) {
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(buffer); } catch (e) { /* socket closing; ignore */ }
    }
  }

  async function startCapture() {
    sourceNode = audioCtx.createMediaStreamSource(micStream);
    // Keep the graph pulled without audible output.
    var sink = audioCtx.createGain();
    sink.gain.value = 0;
    sink.connect(audioCtx.destination);

    var workletOk = false;
    if (audioCtx.audioWorklet) {
      try {
        var blob = new Blob([WORKLET_SOURCE], { type: "application/javascript" });
        workletBlobUrl = URL.createObjectURL(blob);
        await audioCtx.audioWorklet.addModule(workletBlobUrl);
        var node = new AudioWorkletNode(audioCtx, "pcm16-resampler");
        node.port.onmessage = function (event) { sendPcm(event.data); };
        sourceNode.connect(node);
        node.connect(sink);
        captureNode = node;
        workletOk = true;
      } catch (e) {
        // Fall through to ScriptProcessorNode below.
        if (workletBlobUrl) { URL.revokeObjectURL(workletBlobUrl); workletBlobUrl = null; }
      }
    }

    if (!workletOk) {
      // Deprecated but universal fallback (old Chrome / Android WebView).
      var state = { buf: [], readPos: 0 };
      var inputRate = audioCtx.sampleRate;
      var proc = audioCtx.createScriptProcessor(4096, 1, 1);
      proc.onaudioprocess = function (event) {
        var input = event.inputBuffer.getChannelData(0);
        var pcm = resampleBlock(input, inputRate, state);
        if (pcm.length > 0) sendPcm(pcm.buffer.slice(0, pcm.byteLength));
      };
      sourceNode.connect(proc);
      proc.connect(sink);
      captureNode = proc;
    }
  }

  /* ---------- WebSocket lifecycle ---------- */

  function handleServerText(msg) {
    // Tolerant: ignore anything we do not recognise.
    if (!msg || typeof msg !== "object") return;
    if (msg.type === "error" && typeof msg.message === "string") {
      showError("Server: " + msg.message);
    }
    // Known-but-unactionable types (e.g. greeting/status pings) are
    // intentionally ignored; transcript + status come from polling.
  }

  function waitForOpen(socket, timeoutMs) {
    return new Promise(function (resolve, reject) {
      var done = false;
      var timer = setTimeout(function () {
        if (!done) { done = true; reject(new Error("WebSocket connect timed out")); }
      }, timeoutMs);
      socket.addEventListener("open", function onOpen() {
        if (done) return;
        done = true;
        clearTimeout(timer);
        socket.removeEventListener("open", onOpen);
        resolve();
      });
      socket.addEventListener("error", function onError() {
        if (done) return;
        done = true;
        clearTimeout(timer);
        socket.removeEventListener("error", onError);
        reject(new Error("WebSocket failed to connect — is the server running?"));
      });
    });
  }

  async function startMic() {
    clearError();
    setButtons(true);
    stopping = false;
    try {
      // Honest pre-check: never pretend to connect in demo mode.
      setStatus("connecting", "checking server status…");
      var statusRes = await fetch("/api/status");
      if (!statusRes.ok) {
        throw new Error("Server status check failed (HTTP " + statusRes.status + ") — is the server running?");
      }
      var st = await statusRes.json();
      if (st.kill_switch_engaged) {
        throw new Error("Kill switch is engaged — voice is disabled until it is disengaged.");
      }
      if (!st.live_ready) {
        throw new Error("Demo mode — live voice unavailable: " + (st.demo_reason || "unknown reason"));
      }

      setStatus("connecting", "requesting microphone…");
      try {
        micStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true },
          video: false,
        });
      } catch (err) {
        if (err && err.name === "NotAllowedError") {
          throw new Error("Microphone permission denied — allow mic access in the browser and try again.");
        }
        if (err && err.name === "NotFoundError") {
          throw new Error("No microphone found on this device.");
        }
        throw new Error("Could not open the microphone: " + (err && err.message ? err.message : err));
      }

      audioCtx = new (window.AudioContext || window.webkitAudioContext)();
      if (audioCtx.state === "suspended") await audioCtx.resume();

      setStatus("connecting", "opening WebSocket…");
      ws = new WebSocket(wsUrl());
      ws.binaryType = "arraybuffer";
      await waitForOpen(ws, 10000);

      ws.onmessage = function (event) {
        if (typeof event.data === "string") {
          try { handleServerText(JSON.parse(event.data)); } catch (e) { /* ignore bad JSON */ }
        } else if (event.data instanceof ArrayBuffer) {
          schedulePlayback(new Int16Array(event.data));
        }
      };
      ws.onclose = function () {
        if (stopping) return; // expected: our own Stop
        showError("WebSocket closed unexpectedly — the session ended. Press Start mic to reconnect.");
        setStatus("idle", "connection lost");
        teardown();
        setButtons(false);
      };
      ws.onerror = function () {
        // onclose follows with the real teardown; avoid a double message.
        if (!stopping) showError("WebSocket error — connection to the server failed or dropped.");
      };

      await startCapture();
      nextPlayTime = 0;
      setStatus("connected", "speak now — mic streaming PCM16 16 kHz over WebSocket");
      hintEl.textContent = "Mic is live over WebSocket. Press Stop to end the session.";
      startPolling();
    } catch (err) {
      console.error(err);
      showError(err.message);
      setStatus("idle", err.message);
      await teardown();
      setButtons(false);
    }
  }

  async function teardown() {
    if (captureNode) {
      try { captureNode.disconnect(); } catch (e) { /* ignore */ }
      captureNode = null;
    }
    if (sourceNode) {
      try { sourceNode.disconnect(); } catch (e) { /* ignore */ }
      sourceNode = null;
    }
    if (workletBlobUrl) {
      URL.revokeObjectURL(workletBlobUrl);
      workletBlobUrl = null;
    }
    if (audioCtx) {
      try { await audioCtx.close(); } catch (e) { /* ignore */ }
      audioCtx = null;
    }
    if (micStream) {
      micStream.getTracks().forEach(function (t) { t.stop(); });
      micStream = null;
    }
    if (ws) {
      try { ws.close(); } catch (e) { /* ignore */ }
      ws = null;
    }
    nextPlayTime = 0;
  }

  async function stopMic() {
    stopping = true;
    // Tell the server the turn/session is over; the socket close below
    // ends the session server-side as well.
    if (ws && ws.readyState === WebSocket.OPEN) {
      try { ws.send(JSON.stringify({ type: "end" })); } catch (e) { /* ignore */ }
    }
    await teardown();
    // NOTE: deliberately no POST /api/stop here — that endpoint engages
    // the shared kill switch, which would block the next session too.
    setStatus("idle", "session stopped");
    setButtons(false);
    hintEl.textContent = "Session stopped. Press Start mic to talk again.";
    stopping = false;
  }

  startBtn.addEventListener("click", startMic);
  stopBtn.addEventListener("click", stopMic);

  // Show the transcript and brain mode even before any mic session
  // (e.g. after a reload). Also surfaces demo-mode state early via the
  // brain-mode line; the connect-time check still gates Start.
  startPolling();
})();
