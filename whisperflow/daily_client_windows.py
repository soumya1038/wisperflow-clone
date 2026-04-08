"""
WhisperFlow Daily Client (Windows)

Usage:
    python -m whisperflow.daily_client_windows

Behavior:
    - Press F8 once to start recording
    - Press F8 again to stop recording and transcribe
    - Transcript is pasted into the active app via Ctrl+V

Requirements:
    - WhisperFlow server running on http://127.0.0.1:8181
    - PyAudio + websocket-client installed
"""

from __future__ import annotations

import ctypes
import json
import os
import threading
import time
import traceback
from ctypes import wintypes
from typing import List, Optional

import httpx
import pyaudio
import websocket


# --- WhisperFlow / audio ---
SERVER_BASE_URL = os.getenv("WHISPERFLOW_SERVER_BASE_URL", "http://127.0.0.1:8181").strip().rstrip("/")
SERVER_HEALTH_URL = f"{SERVER_BASE_URL}/health"
SERVER_WS_URL = os.getenv("WHISPERFLOW_SERVER_WS_URL", "").strip() or (
    SERVER_BASE_URL.replace("http://", "ws://").replace("https://", "wss://") + "/v1/ws"
)
API_KEY = (os.getenv("WHISPERFLOW_API_KEY") or "").strip()
SAMPLE_RATE = 16000
CHANNELS = 1
CHUNK_SIZE = 1024
TRAILING_SPACE = True

# --- hotkey (F8) ---
HOTKEY_ID = 0xA11
VK_F8 = 0x77
WM_HOTKEY = 0x0312
MOD_NOREPEAT = 0x4000

# --- winapi ---
CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL = 0x11
VK_V = 0x56

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

# WinAPI signatures (critical on 64-bit Windows to avoid pointer truncation).
user32.OpenClipboard.argtypes = [wintypes.HWND]
user32.OpenClipboard.restype = wintypes.BOOL
user32.CloseClipboard.argtypes = []
user32.CloseClipboard.restype = wintypes.BOOL
user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
user32.IsClipboardFormatAvailable.restype = wintypes.BOOL
user32.GetClipboardData.argtypes = [wintypes.UINT]
user32.GetClipboardData.restype = wintypes.HANDLE
user32.EmptyClipboard.argtypes = []
user32.EmptyClipboard.restype = wintypes.BOOL
user32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
user32.SetClipboardData.restype = wintypes.HANDLE

kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
kernel32.GlobalAlloc.restype = wintypes.HGLOBAL
kernel32.GlobalFree.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalFree.restype = wintypes.HGLOBAL
kernel32.GlobalLock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalLock.restype = ctypes.c_void_p
kernel32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
kernel32.GlobalUnlock.restype = wintypes.BOOL


def _beep_ok():
    user32.MessageBeep(0x00000040)


def _beep_warn():
    user32.MessageBeep(0x00000030)


def _get_clipboard_text() -> str:
    """Return current Unicode clipboard text, best-effort."""
    if not user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
        return ""

    for _ in range(8):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return ""

    text = ""
    try:
        handle = user32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return ""

        locked = kernel32.GlobalLock(handle)
        if not locked:
            return ""

        try:
            try:
                text = ctypes.wstring_at(locked)
            except OSError:
                # Clipboard can change between lock/read; keep non-fatal.
                text = ""
        finally:
            kernel32.GlobalUnlock(handle)
    finally:
        user32.CloseClipboard()
    return text


def _set_clipboard_text(text: str) -> bool:
    """Set Unicode text to clipboard."""
    for _ in range(8):
        if user32.OpenClipboard(None):
            break
        time.sleep(0.01)
    else:
        return False

    try:
        user32.EmptyClipboard()
        size = (len(text) + 1) * ctypes.sizeof(ctypes.c_wchar)
        hglobal = kernel32.GlobalAlloc(GMEM_MOVEABLE, size)
        if not hglobal:
            return False
        locked = kernel32.GlobalLock(hglobal)
        if not locked:
            kernel32.GlobalFree(hglobal)
            return False
        try:
            buffer = ctypes.create_unicode_buffer(text)
            ctypes.memmove(locked, ctypes.addressof(buffer), size)
        finally:
            kernel32.GlobalUnlock(hglobal)
        if not user32.SetClipboardData(CF_UNICODETEXT, hglobal):
            kernel32.GlobalFree(hglobal)
            return False
        return True
    finally:
        user32.CloseClipboard()


def _press_ctrl_v():
    user32.keybd_event(VK_CONTROL, 0, 0, 0)
    user32.keybd_event(VK_V, 0, 0, 0)
    user32.keybd_event(VK_V, 0, KEYEVENTF_KEYUP, 0)
    user32.keybd_event(VK_CONTROL, 0, KEYEVENTF_KEYUP, 0)


def _paste_text(text: str):
    """Paste text into focused app and restore previous clipboard content."""
    original = ""
    try:
        original = _get_clipboard_text()
    except Exception:
        original = ""

    if not _set_clipboard_text(text):
        return

    time.sleep(0.04)
    _press_ctrl_v()
    time.sleep(0.08)

    try:
        _set_clipboard_text(original)
    except Exception:
        # If restoration fails, we still keep the main paste successful.
        pass


def _transcribe_chunks(
    chunks: List[bytes],
    ws_url: str = SERVER_WS_URL,
    api_key: Optional[str] = None,
) -> str:
    """Send captured chunks to WhisperFlow websocket and collect transcript."""
    headers = [f"X-API-Key: {api_key}"] if api_key else None
    ws = websocket.create_connection(ws_url, timeout=8, header=headers)
    ws.settimeout(0.9)

    final_segments: List[str] = []
    latest_partial = ""
    last_activity = time.time()
    start = time.time()

    try:
        for chunk in chunks:
            ws.send_binary(chunk)

        while time.time() - start < 15:
            try:
                raw = ws.recv()
            except websocket.WebSocketTimeoutException:
                if time.time() - last_activity > 1.1:
                    break
                continue

            if raw is None:
                break
            if isinstance(raw, bytes):
                try:
                    raw = raw.decode("utf-8", errors="ignore")
                except Exception:
                    continue

            try:
                payload = json.loads(raw)
            except Exception:
                continue

            if isinstance(payload, dict) and payload.get("ok") is False:
                err = payload.get("error") or {}
                message = str(err.get("message") or "Transcription service returned an error.")
                raise RuntimeError(message)

            text = ((payload.get("data") or {}).get("text") or "").strip()
            if not text:
                continue

            last_activity = time.time()
            if payload.get("is_partial", True):
                latest_partial = text
            else:
                if not final_segments or final_segments[-1] != text:
                    final_segments.append(text)
                latest_partial = ""
    finally:
        try:
            ws.close()
        except Exception:
            pass

    if final_segments:
        return " ".join(final_segments).strip()
    return latest_partial.strip()


class DailyDictationClient:
    def __init__(self):
        self._lock = threading.Lock()
        self._recording = False
        self._processing = False
        self._audio_chunks: List[bytes] = []
        self._capture_thread: Optional[threading.Thread] = None

    def run(self):
        self._check_server_health()
        self._register_hotkey()
        print("WhisperFlow Daily Client started.")
        print("Hotkey: F8 (press once to record, press again to transcribe + paste)")
        print("Press Ctrl+C in this terminal to quit.")

        msg = wintypes.MSG()
        try:
            while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) != 0:
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    self._on_hotkey()
                user32.TranslateMessage(ctypes.byref(msg))
                user32.DispatchMessageW(ctypes.byref(msg))
        finally:
            self._unregister_hotkey()

    def _check_server_health(self):
        try:
            headers = {"X-API-Key": API_KEY} if API_KEY else None
            response = httpx.get(SERVER_HEALTH_URL, timeout=3, headers=headers)
            response.raise_for_status()
            print(f"[Server] {response.text}")
        except Exception as exc:
            print(
                f"[Server] Not reachable at {SERVER_BASE_URL}.\n"
                "Start server first:\n"
                "  python -m uvicorn whisperflow.fast_server:app --host 0.0.0.0 --port 8181"
            )
            raise RuntimeError("WhisperFlow server is not running.") from exc

    def _register_hotkey(self):
        if not user32.RegisterHotKey(None, HOTKEY_ID, MOD_NOREPEAT, VK_F8):
            raise RuntimeError("Failed to register F8 hotkey. Another app may be using it.")

    def _unregister_hotkey(self):
        user32.UnregisterHotKey(None, HOTKEY_ID)

    def _on_hotkey(self):
        with self._lock:
            if self._processing:
                print("[Busy] Transcription still running...")
                _beep_warn()
                return

            if not self._recording:
                self._start_recording_locked()
                return

            self._recording = False
            capture_thread = self._capture_thread

        if capture_thread:
            capture_thread.join(timeout=2.5)

        with self._lock:
            chunks = list(self._audio_chunks)
            self._audio_chunks = []
            self._processing = True

        if not chunks:
            print("[Record] No audio captured.")
            _beep_warn()
            with self._lock:
                self._processing = False
            return

        print(f"[Record] Stopped. Sending {len(chunks)} chunks...")
        threading.Thread(target=self._transcribe_and_paste, args=(chunks,), daemon=True).start()

    def _start_recording_locked(self):
        self._recording = True
        self._audio_chunks = []
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        print("[Record] Started...")
        _beep_ok()

    def _capture_loop(self):
        pa = pyaudio.PyAudio()
        stream = None
        try:
            stream = pa.open(
                format=pyaudio.paInt16,
                channels=CHANNELS,
                rate=SAMPLE_RATE,
                input=True,
                frames_per_buffer=CHUNK_SIZE,
            )

            while True:
                with self._lock:
                    if not self._recording:
                        break
                data = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                with self._lock:
                    self._audio_chunks.append(data)
        except Exception as exc:
            print(f"[Audio] Capture error: {exc}")
            _beep_warn()
        finally:
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            pa.terminate()

    def _transcribe_and_paste(self, chunks: List[bytes]):
        t0 = time.time()
        try:
            text = _transcribe_chunks(chunks, ws_url=SERVER_WS_URL, api_key=API_KEY or None)
            ms = int((time.time() - t0) * 1000)
            if not text:
                print(f"[Transcribe] No text ({ms}ms).")
                _beep_warn()
                return

            paste_text = f"{text} " if TRAILING_SPACE else text
            _paste_text(paste_text)
            print(f"[Transcribe] {ms}ms -> {text[:120]}")
            _beep_ok()
        except Exception as exc:
            print(f"[Transcribe] Failed: {exc}")
            traceback.print_exc()
            _beep_warn()
        finally:
            with self._lock:
                self._processing = False


def main():
    client = DailyDictationClient()
    client.run()


if __name__ == "__main__":
    main()
