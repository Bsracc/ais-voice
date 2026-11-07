# Jalan di PC Lokal (Fedora) — ais-voice

Service suara AISIMA (Phase 2): STT Deepgram → otak LLM lewat Nagoya Router
(Opus 5.5) → TTS Cartesia (suara Austin). Dijalankan di laptop/PC sendiri,
jadi mic browser langsung jalan tanpa tunnel — tidak perlu Render, ngrok,
atau Cloudflare.

## 1. Syarat

- Fedora (atau Linux lain), Python 3.12, `uv`, `git`.
- Pasang uv kalau belum ada: `curl -LsSf https://astral.sh/uv/install.sh | sh`
  lalu buka terminal baru.

## 2. Pasang

```bash
unzip ais-voice-pc.zip -d ais-voice   # atau: git clone https://github.com/Bsracc/ais-voice
cd ais-voice
uv sync
cp .env.example .env
```

## 3. Isi `.env` (jangan pernah commit file ini)

| Isi | Dari mana |
|---|---|
| `DEEPGRAM_API_KEY` | Dashboard Deepgram (sudah punya) |
| `CARTESIA_API_KEY` | Dashboard Cartesia (sudah punya) |
| `CARTESIA_VOICE_ID` | `1fcd23d0-bf12-4896-8f60-4f21ef5c9b98` (Austin - Conversational Guide) |
| `ANTHROPIC_API_KEY` | **Key Nagoya Router** (bukan key Anthropic) |
| `ANTHROPIC_BASE_URL` | **Base URL dari dashboard Nagoya Router** — untuk jalur Anthropic TANPA `/v1` di ujung (SDK menambahkan `/v1/messages` sendiri) |
| `ANTHROPIC_MODEL` | `claude-opus-5.5` |
| `BRAIN_MODE` | `anthropic` |

Nagoya Router: daftar di router.nagoyadigital.com, top up Starter Rp11.000
(50 jt kredit, tidak kedaluwarsa, bayar QRIS), salin key + base URL dari
dashboard. Harga Opus 5.5: Rp6.552 input / Rp32.760 output per 1 jt token.
Kalau Opus tidak ada di katalog saat itu, pakai Sonnet 5.5 yang lebih murah
(`ANTHROPIC_MODEL=claude-sonnet-5.5`, Rp5.242 / Rp26.208 per 1 jt token).

Mode lain: `BRAIN_MODE=muse-operator` = tanpa LLM (setiap giliran bicara
masuk antrean, dibalas manual lewat `scripts/muse_reply.py`). Gratis tapi
lambat — hanya cadangan kalau key router habis.

## 4. Jalan

```bash
./start-server.sh
```

Lalu buka di browser: **http://127.0.0.1:7861/ws**
— izinkan mic, bicara, tunggu balasan suara.

Cek status: http://127.0.0.1:7861/api/status — harus `live_ready: true`
dan `brain_mode: anthropic`.

## 5. Catatan

- Rekaman default MATI. Jangan nyalakan untuk panggilan ke California /
  Florida tanpa pengumuman + persetujuan eksplisit (aturan all-party consent).
- Belum ada dial otomatis (Telnyx): service ini hanya menjawab lewat browser.
  Dial keluar butuh persetujuan tertulis dulu (aturan FCC 24-17) — belum dibangun.
- Kill switch: `POST /api/stop` mematikan sesi suara yang jalan.
- Tes cepat tanpa mic: `python -m pytest -q` (harus lolos semua).
