# WhisperFlow Render Deployment Guide (Free Plan)

This guide deploys `whisperflow.fast_server:app` to Render and prepares it for browser/app integrations.

## 1. What You Deploy

- Service: FastAPI + WebSocket Whisper server
- Entry point: `whisperflow.fast_server:app`
- Health route: `/health`
- Structured routes:
  - `GET /v1/health`
  - `GET|POST /v1/wake`
  - `POST /v1/transcribe/pcm`
  - `WS /v1/ws`

## 2. Important Free Plan Reality

- Render free web services can sleep after inactivity.
- First request after sleep can be slow (cold start).
- Your app should call `/v1/wake` on load to reduce user-visible delay before first dictation.

## 3. Pre-Deploy Checklist

1. Push this project to GitHub.
2. Confirm these files are committed:
   - `render.yaml`
   - `whisperflow/fast_server.py`
   - `whisperflow/transcriber.py`
3. Decide your frontend domain, for example:
   - `https://app.example.com`

## 4. Deploy on Render (Blueprint Method)

1. Log into Render.
2. Click `New` -> `Blueprint`.
3. Select this GitHub repository.
4. Render detects `render.yaml` and proposes a service:
   - `whisperflow-api`
5. Set required environment variables before deploy:
   - `WHISPERFLOW_API_KEY` = a long random key
6. Update origin allowlist:
   - `WHISPERFLOW_ALLOWED_ORIGINS` = your app domain(s), comma separated
   - Example: `https://app.example.com,https://staging.example.com`
7. Click `Apply`.

Render will build with:

```bash
pip install -r requirements.server.txt
```

and run with:

```bash
uvicorn whisperflow.fast_server:app --host 0.0.0.0 --port $PORT
```

If your Render service is configured as a Docker service, this repo's `Dockerfile`
already starts uvicorn with the Render `PORT` variable.

## 5. Verify Deployment

Assume your Render URL is:

`https://whisperflow-api.onrender.com`

1. Health:

```bash
curl https://whisperflow-api.onrender.com/health
```

2. Structured health:

```bash
curl https://whisperflow-api.onrender.com/v1/health
```

3. Wake (protected):

```bash
curl -X POST "https://whisperflow-api.onrender.com/v1/wake?wait=true" \
  -H "X-API-Key: YOUR_API_KEY"
```

Expected: JSON with `"ok": true` and model status.

## 6. Security Configuration (Recommended)

Use these env vars in Render:

- `WHISPERFLOW_API_KEY`: required for protected routes and WS
- `WHISPERFLOW_ALLOWED_ORIGINS`: strict CORS allowlist
- `WHISPERFLOW_MAX_AUDIO_BYTES`: max upload/chunk bytes (default 10MB)
- `WHISPERFLOW_MODEL_NAME`: default model source (use `tiny.en` on free plan)
- `WHISPERFLOW_WARM_ON_START`: keep `false` on free plan to reduce startup memory
- `WHISPERFLOW_DEVICE`: `cpu`
- `WHISPERFLOW_COMPUTE_TYPE`: `int8`
- `WHISPERFLOW_MAX_CONCURRENT_TRANSCRIBES`: recommended `2`
- `WHISPERFLOW_MAX_ACTIVE_WS_SESSIONS`: recommended `25`
- `WHISPERFLOW_MAX_SESSION_QUEUE_CHUNKS`: recommended `64`

## 7. Routes and Auth Matrix

- Public:
  - `GET /health`
  - `GET /v1/health`
- Protected when `WHISPERFLOW_API_KEY` is set:
  - `GET|POST /v1/wake`
  - `POST /transcribe_pcm_chunk`
  - `POST /v1/transcribe/pcm`
  - `WS /ws`
  - `WS /v1/ws`

Auth transport:

- HTTP: `X-API-Key` header
- WebSocket:
  - header `X-API-Key` (non-browser clients), or
  - query param `?api_key=...` (browser-compatible)

## 8. Cold-Start UX Strategy

At your app boot:

1. Call `/v1/wake?wait=false` immediately.
2. Poll `/v1/health` every 2-3 seconds (max ~30 seconds) until `model.loaded=true`.
3. Enable microphone button only when ready, or show `Preparing voice engine...`.

## 9. Troubleshooting

1. `401 unauthorized`:
   - Missing or invalid `X-API-Key`
2. Browser CORS error:
   - `WHISPERFLOW_ALLOWED_ORIGINS` does not include your domain
3. Slow first transcription:
   - cold start + model load; use `/v1/wake` on app load
4. `Ran out of memory (used over 512MB)` on Render free:
   - use `tiny.en`, `cpu`, `int8`
   - keep warm-on-start disabled
   - reduce concurrent transcribes
4. `audio_too_large`:
   - reduce chunk size or increase `WHISPERFLOW_MAX_AUDIO_BYTES`
5. WebSocket closes with code `1008`:
   - auth failed on WS handshake

## 10. Production Hardening Next (Optional)

1. Put a lightweight backend proxy between browser and WhisperFlow API.
2. Keep `WHISPERFLOW_API_KEY` only on your backend, never in public frontend JS.
3. Add retry + circuit-breaker logic in your app integration.
