/* AIS Phase 2 voice browser — plain RTCPeerConnection client.
 *
 * Signalling follows the installed pipecat-ai 1.15 SmallWebRTC server
 * (pipecat/transports/smallwebrtc/request_handler.py):
 *   POST  /api/offer  {sdp, type:"offer", pc_id?} -> {sdp, type:"answer", pc_id}
 *   PATCH /api/offer  {pc_id, candidates:[{candidate, sdp_mid, sdp_mline_index}]}
 * This mirrors what @pipecat-ai/small-webrtc-transport does, without an
 * npm build step. The client also opens a data channel and sends a "ping"
 * keepalive, as the reference client does (the server treats messages
 * starting with "ping" as keepalive; anything else must be JSON).
 *
 * Transcript is NOT read from the data channel: the page polls
 * GET /api/transcript every 2 seconds (server-side transcript, text only).
 * The same tick polls GET /api/status to show the brain mode and, in
 * muse-operator mode, a "waiting for operator" status while a turn is
 * queued. No audio is recorded or uploaded anywhere.
 */
(function () {
  "use strict";

  var statusEl = document.getElementById("status");
  var brainModeEl = document.getElementById("brain-mode");
  var startBtn = document.getElementById("mic-start");
  var stopBtn = document.getElementById("mic-stop");
  var hintEl = document.getElementById("hint");
  var transcriptEl = document.getElementById("transcript");
  var audioEl = document.getElementById("remote-audio");

  var pc = null;
  var micStream = null;
  var pcId = null;
  var pollTimer = null;
  var pingTimer = null;
  var dataChannel = null;
  var seenCount = 0;
  var currentState = "idle";
  var brainMode = "anthropic";
  var pendingCount = 0;
  var savedStatusText = null; // connection status, restored once no turn is pending

  function setStatus(state, note) {
    currentState = state;
    statusEl.textContent = "Status: " + state + (note ? " — " + note : "");
  }

  function setButtons(running) {
    startBtn.disabled = running;
    stopBtn.disabled = !running;
  }

  async function postOffer(offer) {
    var res = await fetch("/api/offer", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        sdp: offer.sdp,
        type: offer.type,
        pc_id: pcId || undefined,
      }),
    });
    if (!res.ok) {
      var detail = "";
      try { detail = (await res.json()).detail || ""; } catch (e) { /* ignore */ }
      throw new Error("offer failed (" + res.status + "): " + (detail || res.statusText));
    }
    return res.json();
  }

  async function patchCandidates(candidates) {
    if (!pcId || !candidates.length) return;
    await fetch("/api/offer", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        pc_id: pcId,
        candidates: candidates.map(function (c) {
          return {
            candidate: c.candidate,
            sdp_mid: c.sdpMid,
            sdp_mline_index: c.sdpMLineIndex,
          };
        }),
      }),
    });
  }

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

  async function startMic() {
    setButtons(true);
    try {
      setStatus("listening", "requesting microphone…");
      micStream = await navigator.mediaDevices.getUserMedia({ audio: true, video: false });

      pc = new RTCPeerConnection({
        iceServers: [{ urls: "stun:stun.l.google.com:19302" }],
      });
      micStream.getTracks().forEach(function (t) { pc.addTrack(t, micStream); });

      pc.ontrack = function (event) {
        if (event.track.kind === "audio") {
          audioEl.srcObject = event.streams[0];
          setStatus("speaking", "agent audio playing");
        }
      };
      pc.onicecandidate = function (event) {
        if (event.candidate) patchCandidates([event.candidate]);
      };
      pc.onconnectionstatechange = function () {
        if (pc.connectionState === "connected") setStatus("listening", "connected — speak now");
        if (pc.connectionState === "failed" || pc.connectionState === "disconnected") {
          setStatus("idle", "connection " + pc.connectionState);
        }
      };

      // Data channel mirrors the reference client (keepalive only).
      dataChannel = pc.createDataChannel("voice");
      pingTimer = setInterval(function () {
        if (dataChannel && dataChannel.readyState === "open") {
          dataChannel.send("ping " + Date.now());
        }
      }, 5000);

      var offer = await pc.createOffer();
      await pc.setLocalDescription(offer);
      var answer = await postOffer(offer);
      pcId = answer.pc_id;
      await pc.setRemoteDescription(new RTCSessionDescription(answer));
      setStatus("listening", "connected — speak now");

      seenCount = 0;
      pollTimer = setInterval(function () { pollTranscript(); pollStatus(); }, 2000);
      pollTranscript();
      pollStatus();
      hintEl.textContent = "Mic is live. Press Stop to end the session.";
    } catch (err) {
      console.error(err);
      setStatus("idle", err.message);
      hintEl.textContent = "Could not start: " + err.message +
        " (Demo mode without API keys shows this until the pipeline is built.)";
      await stopMic();
      setButtons(false);
    }
  }

  async function stopMic() {
    if (pollTimer) { clearInterval(pollTimer); pollTimer = null; }
    if (pingTimer) { clearInterval(pingTimer); pingTimer = null; }
    if (dataChannel) { try { dataChannel.close(); } catch (e) {} dataChannel = null; }
    if (pc) { try { pc.close(); } catch (e) {} pc = null; }
    if (micStream) { micStream.getTracks().forEach(function (t) { t.stop(); }); micStream = null; }
    pcId = null;
    try { await fetch("/api/stop", { method: "POST" }); } catch (e) { /* ignore */ }
    setStatus("idle", "session stopped");
    setButtons(false);
    hintEl.textContent = "Session stopped. Press Start mic to talk again.";
  }

  startBtn.addEventListener("click", startMic);
  stopBtn.addEventListener("click", stopMic);

  // Show the transcript and brain mode even before any mic session
  // (e.g. after a reload).
  pollTranscript();
  pollStatus();
})();
