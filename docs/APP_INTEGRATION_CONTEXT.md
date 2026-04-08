# WhisperFlow Integration Context (Mic Button -> Transcribe -> Paste)

This document explains how to integrate deployed WhisperFlow into another application with:

- mic click to start recording
- mic click to stop recording
- real-time transcription over WebSocket
- paste into selected input/textarea/contenteditable
- popup/flash error handling
- wakeup flow to reduce cold-start delays on Render free

---

## 1. API Endpoints You Should Use

Base URL:

`https://whisperflow-api.onrender.com`

Routes:

- `GET /v1/health` -> readiness, model status
- `POST /v1/wake?wait=false` -> start model warmup
- `POST /v1/transcribe/pcm` -> fallback one-shot transcription upload
- `WS /v1/ws` -> streaming transcription

Auth:

- HTTP: `X-API-Key: <key>`
- WebSocket:
  - non-browser client: `X-API-Key` header
  - browser client: `wss://.../v1/ws?api_key=<key>`

---

## 2. Recommended Security Pattern

Do not expose your master API key directly in frontend code.

Best pattern:

1. Frontend calls your backend.
2. Backend calls WhisperFlow with `X-API-Key`.
3. Backend proxies wake/transcribe requests and signs short-lived WS URLs.

If you must do browser-direct integration for MVP, use CORS allowlist and rotate keys regularly.

---

## 3. App Boot Flow (Wake + Readiness)

On your app load:

1. Call wake in background.
2. Poll health until model loaded.
3. Enable mic button only when ready.

```ts
// TypeScript/JS
const API_BASE = "https://whisperflow-api.onrender.com";
const API_KEY = "<SERVER_SIDE_OR_TEMP_KEY>";

export async function wakeWhisperFlow() {
  await fetch(`${API_BASE}/v1/wake?wait=false`, {
    method: "POST",
    headers: { "X-API-Key": API_KEY },
  });
}

export async function waitUntilReady(timeoutMs = 30000) {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    const res = await fetch(`${API_BASE}/v1/health`);
    const data = await res.json();
    if (data?.model?.loaded) return true;
    await new Promise((r) => setTimeout(r, 2000));
  }
  return false;
}
```

---

## 4. Mic Start/Stop + Streaming Transcription

### 4.1 Core Controller

```ts
type FlashKind = "success" | "error" | "info";

class WhisperMicController {
  private mediaRecorder: MediaRecorder | null = null;
  private stream: MediaStream | null = null;
  private ws: WebSocket | null = null;
  private isRecording = false;
  private latestPartial = "";
  private finalSegments: string[] = [];

  constructor(
    private wsUrl: string, // ex: wss://.../v1/ws?api_key=...
    private onFlash: (message: string, kind: FlashKind) => void,
    private onStatus: (status: string) => void
  ) {}

  async start() {
    if (this.isRecording) return;
    this.onStatus("Starting microphone...");
    this.finalSegments = [];
    this.latestPartial = "";

    try {
      this.stream = await navigator.mediaDevices.getUserMedia({ audio: true });
      this.ws = new WebSocket(this.wsUrl);

      await new Promise<void>((resolve, reject) => {
        if (!this.ws) return reject(new Error("WebSocket init failed."));
        this.ws.onopen = () => resolve();
        this.ws.onerror = () => reject(new Error("WebSocket connection failed."));
      });

      this.ws.onmessage = (event) => {
        try {
          const payload = JSON.parse(event.data);
          if (payload?.ok === false) {
            const msg = payload?.error?.message || "Transcription service error";
            this.onFlash(msg, "error");
            return;
          }
          const text = payload?.data?.text?.trim?.() || "";
          if (!text) return;

          if (payload?.is_partial) {
            this.latestPartial = text;
            this.onStatus(`Listening... ${text}`);
          } else {
            if (!this.finalSegments.length || this.finalSegments[this.finalSegments.length - 1] !== text) {
              this.finalSegments.push(text);
            }
            this.latestPartial = "";
            this.onStatus("Segment finalized");
          }
        } catch {
          this.onFlash("Received invalid transcription payload.", "error");
        }
      };

      this.mediaRecorder = new MediaRecorder(this.stream, { mimeType: "audio/webm;codecs=opus" });
      this.mediaRecorder.ondataavailable = async (evt) => {
        if (!evt.data || evt.data.size === 0 || !this.ws || this.ws.readyState !== WebSocket.OPEN) return;
        const arr = await evt.data.arrayBuffer();
        this.ws.send(arr); // stream chunks directly
      };

      this.mediaRecorder.start(250); // send chunk every 250ms
      this.isRecording = true;
      this.onStatus("Recording...");
      this.onFlash("Recording started", "info");
    } catch (error: any) {
      this.cleanup();
      this.onFlash(error?.message || "Unable to start microphone.", "error");
      throw error;
    }
  }

  stopAndGetTranscript(): string {
    if (!this.isRecording) return "";
    this.isRecording = false;

    try {
      this.mediaRecorder?.stop();
      this.stream?.getTracks()?.forEach((t) => t.stop());
      this.ws?.close();
    } finally {
      const text = (this.finalSegments.join(" ").trim() || this.latestPartial.trim());
      this.cleanup();
      this.onStatus("Idle");
      if (!text) this.onFlash("No speech detected. Try speaking louder.", "error");
      return text;
    }
  }

  private cleanup() {
    this.mediaRecorder = null;
    this.stream = null;
    this.ws = null;
    this.isRecording = false;
  }
}
```

---

## 5. Paste Transcript into Selected Area

Use one function that supports:

- `<input>`
- `<textarea>`
- contenteditable editors

```ts
export function pasteTranscriptAtSelection(text: string, trailingSpace = true) {
  const finalText = trailingSpace ? `${text} ` : text;
  const active = document.activeElement as HTMLElement | null;

  if (!active) throw new Error("No active element to paste into.");

  // input / textarea
  if (active instanceof HTMLInputElement || active instanceof HTMLTextAreaElement) {
    const start = active.selectionStart ?? active.value.length;
    const end = active.selectionEnd ?? active.value.length;
    active.value = active.value.slice(0, start) + finalText + active.value.slice(end);
    const cursor = start + finalText.length;
    active.selectionStart = cursor;
    active.selectionEnd = cursor;
    active.dispatchEvent(new Event("input", { bubbles: true }));
    return;
  }

  // contenteditable
  if (active.isContentEditable) {
    const sel = window.getSelection();
    if (!sel || sel.rangeCount === 0) {
      active.append(document.createTextNode(finalText));
      return;
    }
    const range = sel.getRangeAt(0);
    range.deleteContents();
    const node = document.createTextNode(finalText);
    range.insertNode(node);
    range.setStartAfter(node);
    range.setEndAfter(node);
    sel.removeAllRanges();
    sel.addRange(range);
    return;
  }

  throw new Error("Focused element does not support text insertion.");
}
```

---

## 6. Full Button Behavior (Click Start / Click Stop)

```ts
let controller: WhisperMicController | null = null;

function flash(message: string, kind: "success" | "error" | "info") {
  // Replace with your toast/snackbar/modal
  console.log(`[${kind}] ${message}`);
}

function setStatus(s: string) {
  console.log("status:", s);
}

async function onMicButtonClick() {
  const wsUrl = `wss://whisperflow-api.onrender.com/v1/ws?api_key=${encodeURIComponent("<TEMP_OR_PROXY_TOKEN>")}`;

  if (!controller) {
    controller = new WhisperMicController(wsUrl, flash, setStatus);
  }

  try {
    // Toggle
    if ((controller as any).isRecording) {
      const text = controller.stopAndGetTranscript();
      if (text) {
        pasteTranscriptAtSelection(text, true);
        flash("Transcription pasted", "success");
      }
    } else {
      await controller.start();
    }
  } catch (error: any) {
    flash(error?.message || "Mic flow failed.", "error");
  }
}
```

---

## 7. Error Popup / Flash Message Map

Map server errors to user-facing text:

- `unauthorized` -> `Session expired. Please refresh and try again.`
- `audio_too_large` -> `Audio too large. Please record a shorter segment.`
- `empty_audio` -> `No audio captured. Check your microphone.`
- `transcription_failed` -> `Transcription failed. Please retry.`
- `ws_session_error` -> `Connection dropped during dictation.`

UI behavior:

1. Show immediate popup/snackbar.
2. Reset mic button to idle.
3. Keep typed text unchanged.
4. Offer one-click retry.

---

## 8. Fallback Mode (If WS Fails)

If WebSocket is blocked, use one-shot HTTP upload:

```ts
async function transcribeViaUpload(blob: Blob, apiKey: string) {
  const form = new FormData();
  form.append("model_name", "tiny.en.pt");
  form.append("files", blob, "audio.webm");

  const res = await fetch("https://whisperflow-api.onrender.com/v1/transcribe/pcm", {
    method: "POST",
    headers: { "X-API-Key": apiKey },
    body: form,
  });

  const data = await res.json();
  if (!res.ok || data?.ok === false) {
    throw new Error(data?.error?.message || "Upload transcription failed");
  }
  return data?.result?.text || "";
}
```

---

## 9. Edge Cases to Handle

1. User denies mic permission:
   - Show error popup + guide to browser permission settings
2. Server sleeping/cold:
   - show `Preparing voice engine...` after wake request
3. User clicks stop immediately:
   - return empty transcript gracefully
4. Focus moved before paste:
   - show `Please click target input and try again`
5. Rapid repeated clicks:
   - disable button during transition states
6. Network drop:
   - reconnect WS once; then fallback to upload mode
7. Mobile Safari recorder format differences:
   - detect unsupported mime types and fallback options

---

## 10. Minimal Backend Proxy (Recommended)

If your frontend should not hold WhisperFlow API key, add your own backend route:

```ts
// Node/Express example
app.post("/api/voice/wake", async (req, res) => {
  const r = await fetch("https://whisperflow-api.onrender.com/v1/wake?wait=false", {
    method: "POST",
    headers: { "X-API-Key": process.env.WHISPERFLOW_API_KEY! },
  });
  res.status(r.status).json(await r.json());
});
```

Do similar proxy routes for:

- `/api/voice/transcribe` (HTTP fallback)
- `/api/voice/ws-url` (short-lived signed WS URL or token strategy)

---

## 11. Ready-to-Use Checklist

1. Render deployed and healthy.
2. `WHISPERFLOW_API_KEY` set.
3. CORS origins allow your app domain.
4. App calls `/v1/wake` on load.
5. Mic button toggles start/stop.
6. Transcript inserts at cursor.
7. Popup/snackbar errors mapped and tested.
8. WS fallback to HTTP upload implemented.
