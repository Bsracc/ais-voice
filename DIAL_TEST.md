# Uji Panggilan Twilio Trial — AI menelepon HP Bsr sendiri

Tujuan: membuktikan ujung-ke-ujung bahwa AI bisa menelepon nomor telepon
beneran (bukan browser): Twilio dial → HP berdering → angkat → AI bicara
disclosure dulu → percakapan jalan → transcript tersimpan.

**Yang diuji HANYA nomor Bsr sendiri.** Kredit trial Twilio memang hanya
bisa menelepon nomor yang sudah terverifikasi — jadi tidak ada risiko
kepencet nelpon klinik. Tidak ada panggilan produksi di sini; produksi
nanti pindah ke Telnyx (lebih murah) setelah jalur ini terbukti jalan.

## Batasan trial Twilio (baca dulu)

- **Hanya ke nomor terverifikasi.** Nomor tujuan harus nomor HP Bsr yang
  sudah diverifikasi di console Twilio. Nomor lain ditolak Twilio.
- **75 menit suara gratis, 30 hari.** Setelah itu trial habis; jangan
  pakai trial untuk produksi.
- **Caller ID = nomor trial Twilio** (nomor gratis dari Twilio, bukan
  nomor Bsr). Di HP akan muncul sebagai nomor tak dikenal.
- **Tanpa kartu kredit** untuk daftar trial.
- Biaya AI-nya (Deepgram + Cartesia + Opus via Nagoya) tetap jalan per
  menit dan terpisah dari biaya telepon.

## (a) Daftar Twilio trial + verifikasi nomor — Bsr

1. Buka https://www.twilio.com/try-twilio — daftar akun gratis (email +
   password, tanpa kartu).
2. Verifikasi nomor HP sendiri di console: **Phone Numbers → Verified
   Caller IDs → Add a new Caller ID** — masukkan nomor HP Bsr, terima
   kode SMS/telepon, masukkan kodenya. Nomor ini yang jadi tujuan uji.
3. Salin dari console (**Account → API keys & tokens**):
   - **Account SID** (diawali `AC...`)
   - **Auth Token** (klik View, salin)
4. Salin nomor trial: **Phone Numbers → Manage → Active Numbers** —
   nomor yang Twilio kasih gratis (format E.164, mis. `+1...`). Ini
   jadi caller ID (`TWILIO_FROM_NUMBER`).

## (b) Isi `.env` di laptop

Di folder `ais-voice` di laptop, salin `.env.example` ke `.env` lalu isi:

```ini
TWILIO_ACCOUNT_SID=AC...            # dari langkah (a)3
TWILIO_AUTH_TOKEN=...              # dari langkah (a)3
TWILIO_FROM_NUMBER=+1...           # nomor trial Twilio, dari langkah (a)4
TWILIO_TO_NUMBER=+62...            # nomor HP Bsr sendiri, format E.164
TWILIO_STREAM_URL=wss://<id>.ngrok-free.app/twilio/stream
TWILIO_TEST_MODE=true
TWILIO_TEST_TIMEZONE=Asia/Jakarta
```

Yang lain tetap seperti biasa: `DEEPGRAM_API_KEY`, `CARTESIA_API_KEY`,
`CARTESIA_VOICE_ID` (Austin), `ANTHROPIC_API_KEY` = key Nagoya Router,
`ANTHROPIC_BASE_URL` = base URL Nagoya (tanpa `/v1`),
`ANTHROPIC_MODEL=claude-opus-5.5`, `BRAIN_MODE=anthropic`.

**Consent tertulis (wajib):** tambahkan satu baris ke
`data/consent.json` untuk nomor sendiri (salin bentuknya dari
`data/consent.example.json`, ganti `phone` dengan nomor HP Bsr dan
`timezone` dengan `Asia/Jakarta`). Tanpa baris ini gate menolak dial
dengan `no_consent` — itu memang disengaja.

```json
[{"channel":"self-test","consented_at":"2026-11-07T00:00:00Z","name":"Bsr (self-test)","phone":"+62...","seller":"AISIMA","timezone":"Asia/Jakarta"}]
```

## (c) Jalankan service + ngrok di LAPTOP Bsr

Kenapa laptop, bukan VM ini: Twilio butuh URL `wss://` PUBLIK untuk
stream audio, dan VM ini tidak punya URL publik (semua tunnel dari VM
gagal: Cloudflare TLS EOF, ngrok berbayar di belakang proxy). Di laptop
internet langsung jalan, jadi streaming STT/TTS juga bisa dipakai.

Terminal 1 — service:

```bash
cd ais-voice
uv sync
./start-server.sh
```

Cek: `curl http://127.0.0.1:7861/api/twilio/status` harus
`"configured": true` dan `"test_mode": true`.

Terminal 2 — tunnel publik (ngrok gratis, daftar dulu di ngrok.com
kalau belum):

```bash
ngrok http 7861
```

Salin URL `https://<id>.ngrok-free.app` dari output ngrok, lalu isi
`TWILIO_STREAM_URL=wss://<id>.ngrok-free.app/twilio/stream` di `.env`
dan restart service (Terminal 1: Ctrl+C, `./start-server.sh` lagi).
URL ngrok gratis berubah setiap restart — ulangi langkah ini kalau
ngrok di-restart.

## (d) Jalankan dial

```bash
# 1. Gates dulu, tanpa menelepon:
uv run python scripts/twilio_dial.py --dry-run
# Harus: "GATES PASS". Kalau REFUSED, baca alasannya (mis. no_consent
# = consent.json belum diisi; outside_calling_hours = di luar 08:00–
# 21:00 waktu Asia/Jakarta; dial_cap_exceeded = sudah 3x dial 24 jam).

# 2. Panggilan beneran:
uv run python scripts/twilio_dial.py
# Harus: "CALL PLACED — sid=CA... status=queued".
```

## (e) Yang diharapkan

1. HP Bsr berdering dari nomor trial Twilio (tidak dikenal).
2. Angkat → AI bicara duluan: *"Hi, I am an AI assistant calling on
   behalf of AISIMA. I'm calling about a website redesign for your
   dental clinic. Do you have two minutes?"*
3. Jawab seperti ngobrol biasa (mis. *"How much does it cost?"*) →
   AI jawab `$1,000 one-time`, dst.
4. Tutup telepon → sesi berakhir, transcript tersimpan di
   `data/transcript.jsonl` di laptop; dial tercatat di
   `data/dial_log.json`; audit di `data/audit_log.jsonl`.
5. Ulangi maksimal 3x per 24 jam per nomor (cap gate); mau lebih,
   tunggu 24 jam atau hapus baris dial di `data/dial_log.json` —
   hanya untuk nomor sendiri.

## Kalau gagal

| Gejala | Penyebab paling mungkin |
|---|---|
| `--dry-run` → `twilio_not_configured` | `.env` belum diisi / service belum restart setelah isi `.env` |
| `no_consent` | `data/consent.json` belum ada baris nomor sendiri |
| `outside_calling_hours` | Di luar 08:00–21:00 Asia/Jakarta — coba lagi di jam itu |
| `phase1_unreachable` | `TWILIO_TEST_MODE` tidak `true` — cek `.env` |
| Twilio error `unverified number` | Nomor tujuan belum diverifikasi di console Twilio (a)2 |
| HP berdering tapi sunyi | `TWILIO_STREAM_URL` salah / ngrok mati / service tidak jalan |
| AI tidak menjawab | Cek `api/status` → `live_ready` harus `true` (semua key terisi) |

## Setelah uji lolos

Jalur teleponnya terbukti. Produksi pindah ke Telnyx (Call Control +
nomor lokal $1/bulan, $0,007/menit, tanpa batas trial) dengan gate
yang sama — yang berubah hanya provider dan `TWILIO_TEST_MODE=false`
plus consent tertulis per klinik + scrub DNC asli. Dial ke klinik
TANPA consent tertulis tetap dilarang (FCC 24-17, denda
$500–$1.500/panggilan).
