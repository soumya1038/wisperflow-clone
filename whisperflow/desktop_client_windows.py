"""
WhisperFlow Desktop Client (Windows)

Usage:
    python -m whisperflow.desktop_client_windows

What it provides:
    - Starts WhisperFlow server and desktop client together
    - Global hotkey dictation with auto-paste
    - Settings panel (hotkey, host/port, startup behavior)
    - Local transcription history
    - Floating overlay pill with live state
"""

from __future__ import annotations

import ctypes
import json
import queue
import subprocess
import sys
import threading
import time
import traceback
from ctypes import wintypes
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Optional, Tuple

import httpx
import pyaudio
import tkinter as tk
from tkinter import messagebox

from whisperflow import __version__
from whisperflow.daily_client_windows import (
    CHANNELS,
    CHUNK_SIZE,
    SAMPLE_RATE,
    _beep_ok,
    _beep_warn,
    _paste_text,
    _transcribe_chunks,
)


APP_ROOT = Path(__file__).resolve().parents[1]
APP_DATA_DIR = Path.home() / ".whisperflow"
SETTINGS_PATH = APP_DATA_DIR / "desktop_settings.json"
HISTORY_PATH = APP_DATA_DIR / "desktop_history.json"
SERVER_LOG_PATH = APP_DATA_DIR / "desktop_server.log"

WM_HOTKEY = 0x0312
WM_QUIT = 0x0012
HOTKEY_ID = 0xBEEF

MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

DEFAULT_SETTINGS = {
    "host": "127.0.0.1",
    "port": 8181,
    "hotkey": "F8",
    "trailing_space": True,
    "auto_start_server": True,
    "overlay_enabled": True,
}

MAX_HISTORY_ITEMS = 300

UNSAFE_HOTKEYS = {
    "ctrl+c",
    "ctrl+v",
    "ctrl+x",
    "ctrl+z",
    "ctrl+a",
    "ctrl+s",
    "alt+f4",
}

SPECIAL_VK = {
    "space": 0x20,
    "tab": 0x09,
    "enter": 0x0D,
    "esc": 0x1B,
    "escape": 0x1B,
    "capslock": 0x14,
    "backspace": 0x08,
    "delete": 0x2E,
    "insert": 0x2D,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
}

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32
user32.RegisterHotKey.argtypes = [
    wintypes.HWND,
    ctypes.c_int,
    wintypes.UINT,
    wintypes.UINT,
]
user32.RegisterHotKey.restype = wintypes.BOOL
user32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
user32.UnregisterHotKey.restype = wintypes.BOOL
user32.GetMessageW.argtypes = [
    ctypes.POINTER(wintypes.MSG),
    wintypes.HWND,
    wintypes.UINT,
    wintypes.UINT,
]
user32.GetMessageW.restype = ctypes.c_int
user32.PostThreadMessageW.argtypes = [
    wintypes.DWORD,
    wintypes.UINT,
    wintypes.WPARAM,
    wintypes.LPARAM,
]
user32.PostThreadMessageW.restype = wintypes.BOOL
kernel32.GetCurrentThreadId.argtypes = []
kernel32.GetCurrentThreadId.restype = wintypes.DWORD


def _safe_load_json(path: Path, default):
    if not path.exists():
        return default
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _safe_save_json(path: Path, payload) -> None:
    APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _parse_key_token(token: str) -> Tuple[int, str]:
    token_clean = token.strip().lower()
    if not token_clean:
        raise ValueError("Missing key in hotkey.")

    if token_clean in SPECIAL_VK:
        return SPECIAL_VK[token_clean], token_clean.upper()

    if len(token_clean) == 1 and token_clean.isalpha():
        return ord(token_clean.upper()), token_clean.upper()

    if len(token_clean) == 1 and token_clean.isdigit():
        return ord(token_clean), token_clean

    if token_clean.startswith("f") and token_clean[1:].isdigit():
        idx = int(token_clean[1:])
        if 1 <= idx <= 24:
            return 0x70 + idx - 1, f"F{idx}"

    raise ValueError(
        f"Unsupported key '{token}'. Use A-Z, 0-9, F1-F24, Space, Enter, Tab, arrows."
    )


def parse_hotkey(hotkey_text: str) -> Tuple[int, int, str]:
    tokens = [x.strip() for x in hotkey_text.replace("_", "+").split("+") if x.strip()]
    if not tokens:
        raise ValueError("Hotkey cannot be empty.")

    modifiers = 0
    display_parts = []
    for token in tokens[:-1]:
        token_low = token.lower()
        if token_low in ("ctrl", "control"):
            modifiers |= MOD_CONTROL
            display_parts.append("Ctrl")
        elif token_low == "shift":
            modifiers |= MOD_SHIFT
            display_parts.append("Shift")
        elif token_low == "alt":
            modifiers |= MOD_ALT
            display_parts.append("Alt")
        elif token_low in ("win", "windows"):
            modifiers |= MOD_WIN
            display_parts.append("Win")
        else:
            raise ValueError(
                f"Unsupported modifier '{token}'. Use Ctrl, Shift, Alt, Win."
            )

    key_code, key_display = _parse_key_token(tokens[-1])
    display_parts.append(key_display)
    hotkey_display = "+".join(display_parts)
    return modifiers | MOD_NOREPEAT, key_code, hotkey_display


def hotkey_is_unsafe(hotkey_text: str) -> bool:
    try:
        _, _, display = parse_hotkey(hotkey_text)
    except Exception:
        return False
    return display.lower() in UNSAFE_HOTKEYS


class GlobalHotkeyListener:
    def __init__(
        self,
        on_hotkey: Callable[[], None],
        on_error: Callable[[str], None],
    ) -> None:
        self._on_hotkey = on_hotkey
        self._on_error = on_error
        self._thread: Optional[threading.Thread] = None
        self._thread_id: int = 0
        self._running = threading.Event()
        self._registered = False
        self._ready = threading.Event()
        self._start_error = ""
        self._display_hotkey = "F8"

    @property
    def display_hotkey(self) -> str:
        return self._display_hotkey

    def start(self, hotkey_text: str) -> str:
        self.stop()
        modifiers, vk, display = parse_hotkey(hotkey_text)
        self._display_hotkey = display
        self._start_error = ""
        self._ready.clear()
        self._running.set()
        self._thread = threading.Thread(
            target=self._run_loop,
            args=(modifiers, vk),
            daemon=True,
        )
        self._thread.start()
        if not self._ready.wait(timeout=2.0):
            self.stop()
            raise RuntimeError("Timed out while registering global hotkey.")
        if not self._registered:
            self.stop()
            reason = self._start_error or "Unknown registration error."
            raise RuntimeError(reason)
        return display

    def stop(self) -> None:
        self._running.clear()
        if self._thread_id:
            user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=1.5)
        self._thread = None
        self._thread_id = 0
        self._registered = False

    def _run_loop(self, modifiers: int, vk: int) -> None:
        self._thread_id = int(kernel32.GetCurrentThreadId())
        if not user32.RegisterHotKey(None, HOTKEY_ID, modifiers, vk):
            self._start_error = (
                "Failed to register global hotkey. Another app may be using it."
            )
            self._ready.set()
            return

        self._registered = True
        self._ready.set()
        msg = wintypes.MSG()

        try:
            while self._running.is_set():
                status = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
                if status in (0, -1):
                    break
                if msg.message == WM_HOTKEY and msg.wParam == HOTKEY_ID:
                    try:
                        self._on_hotkey()
                    except Exception as exc:
                        self._on_error(f"Hotkey callback failed: {exc}")
        finally:
            user32.UnregisterHotKey(None, HOTKEY_ID)
            self._registered = False


class OverlayPill:
    IDLE_W = 68
    IDLE_H = 18
    IDLE_R = 9
    ACTIVE_W = 220
    ACTIVE_H = 32
    ACTIVE_R = 16
    BOTTOM_MARGIN = 22

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self.visible = True
        self.state = "idle"
        self.detail = "Ready"

        self.window = tk.Toplevel(root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.attributes("-topmost", True)
        self.window.attributes("-alpha", 0.97)

        self.canvas = tk.Canvas(
            self.window,
            width=self.IDLE_W,
            height=self.IDLE_H,
            bg="#000000",
            bd=0,
            highlightthickness=0,
        )
        self.canvas.pack(fill="both", expand=True)

        self.root.after(700, self._position_loop)
        self.set_state("idle", "Ready")

    def set_visible(self, is_visible: bool) -> None:
        self.visible = is_visible
        if not is_visible:
            self.window.withdraw()
        else:
            self.window.deiconify()
            self._render()
            self._position_now()

    def set_state(self, state: str, detail: str) -> None:
        self.state = state
        self.detail = detail
        if self.visible:
            self.window.deiconify()
        self._render()
        self._position_now()

    def _draw_round_rect(self, x1, y1, x2, y2, radius, fill, outline):
        points = [
            x1 + radius,
            y1,
            x2 - radius,
            y1,
            x2,
            y1,
            x2,
            y1 + radius,
            x2,
            y2 - radius,
            x2,
            y2,
            x2 - radius,
            y2,
            x1 + radius,
            y2,
            x1,
            y2,
            x1,
            y2 - radius,
            x1,
            y1 + radius,
            x1,
            y1,
        ]
        self.canvas.create_polygon(
            points,
            smooth=True,
            splinesteps=36,
            fill=fill,
            outline=outline,
            width=1,
        )

    def _draw_icon(self, x: int, y: int, color: str) -> None:
        heights = [6, 10, 14, 10, 8]
        gap = 3
        width = 2
        offset = 0
        for height in heights:
            top = y - (height // 2)
            self.canvas.create_rectangle(
                x + offset,
                top,
                x + offset + width,
                top + height,
                fill=color,
                outline=color,
            )
            offset += width + gap

    def _render(self) -> None:
        palette = {
            "idle": ("#17191E", "#323741", "#F0F3F7", ""),
            "recording": ("#2A1619", "#603039", "#F8CDD4", "Recording"),
            "transcribing": ("#1C2234", "#32466E", "#DBE7FF", "Transcribing"),
            "ready": ("#16231B", "#2F4F3D", "#D7F1E0", "Done"),
            "error": ("#2E1919", "#5D3131", "#FFD6D6", "Error"),
        }
        bg, border, fg, label = palette.get(self.state, palette["idle"])

        if self.state == "idle":
            width, height, radius = self.IDLE_W, self.IDLE_H, self.IDLE_R
        else:
            width, height, radius = self.ACTIVE_W, self.ACTIVE_H, self.ACTIVE_R

        self.canvas.configure(width=width, height=height, bg="#000000")
        self.canvas.delete("all")
        self._draw_round_rect(0, 0, width, height, radius, bg, border)

        if self.state == "idle":
            self._draw_icon((width // 2) - 10, height // 2, fg)
        else:
            self._draw_icon(12, height // 2, fg)
            self.canvas.create_text(
                38,
                height // 2,
                text=f"{label}: {self.detail}",
                fill=fg,
                font=("Segoe UI", 9),
                anchor="w",
            )

    def _position_now(self) -> None:
        width = int(self.canvas.cget("width"))
        height = int(self.canvas.cget("height"))
        screen_w = self.root.winfo_screenwidth()
        screen_h = self.root.winfo_screenheight()
        x_pos = (screen_w - width) // 2
        y_pos = screen_h - height - self.BOTTOM_MARGIN
        self.window.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

    def _position_loop(self) -> None:
        if self.visible:
            self._position_now()
        self.root.after(1100, self._position_loop)


class WhisperFlowDesktopApp:
    def __init__(self, root: tk.Tk):
        APP_DATA_DIR.mkdir(parents=True, exist_ok=True)

        self.root = root
        self.root.title("Voice Flow Desktop")
        self.root.geometry("1480x900")
        self.root.minsize(1200, 760)
        self.root.configure(bg="#ECEBE8")

        self.settings = self._load_settings()
        try:
            _, _, normalized_hotkey = parse_hotkey(str(self.settings.get("hotkey", "F8")))
            self.settings["hotkey"] = normalized_hotkey
        except Exception:
            self.settings["hotkey"] = "F8"
        self.history = self._load_history()
        self.event_queue: queue.Queue = queue.Queue()

        self._lock = threading.Lock()
        self._recording = False
        self._processing = False
        self._audio_chunks = []
        self._capture_thread: Optional[threading.Thread] = None

        self._server_process: Optional[subprocess.Popen] = None
        self._server_log_file = None
        self._server_spawned_here = False
        self._settings_modal: Optional[tk.Toplevel] = None
        self._modal_values = {}

        self.hotkey_listener = GlobalHotkeyListener(
            on_hotkey=self._on_hotkey_from_thread,
            on_error=self._on_hotkey_error_from_thread,
        )

        self.server_state_var = tk.StringVar(value="Server: Checking...")
        self.mode_var = tk.StringVar(value="Mode: Idle")
        self.hotkey_label_var = tk.StringVar(value=f"Hotkey: {self.settings['hotkey']}")
        self.latency_var = tk.StringVar(value="Latency: -")
        self.words_total_var = tk.StringVar(value="0")
        self.words_wpm_var = tk.StringVar(value="0")
        self.words_streak_var = tk.StringVar(value="0")

        self.mic_devices = self._enumerate_microphones()
        self.nav_buttons = {}
        self.current_nav = "home"

        self._build_styles()
        self._build_ui()

        self.overlay = OverlayPill(self.root)
        self.overlay.set_visible(bool(self.settings.get("overlay_enabled", True)))
        self._set_mode("idle", f"Press {self.settings['hotkey']} to dictate")
        self._refresh_history_list()
        self._refresh_stats()

        self._register_hotkey(show_error=False)
        self.root.after(70, self._drain_events)
        self.root.after(1800, self._refresh_server_status)
        self.root.after(200, self._startup_server_if_enabled)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    @property
    def health_url(self) -> str:
        host = self.settings.get("host", "127.0.0.1")
        port = int(self.settings.get("port", 8181))
        return f"http://{host}:{port}/health"

    @property
    def ws_url(self) -> str:
        host = self.settings.get("host", "127.0.0.1")
        port = int(self.settings.get("port", 8181))
        return f"ws://{host}:{port}/ws"

    def _build_styles(self) -> None:
        # Layout is custom drawn with tk widgets for tighter visual control.
        return

    def _build_ui(self) -> None:
        self._build_top_bar()

        body = tk.Frame(self.root, bg="#ECEBE8")
        body.pack(fill="both", expand=True, padx=12, pady=(0, 12))

        self.sidebar = tk.Frame(body, bg="#E8E6E1", width=230)
        self.sidebar.pack(side="left", fill="y", padx=(0, 12))
        self.sidebar.pack_propagate(False)

        self.workspace = tk.Frame(body, bg="#F5F3EF")
        self.workspace.pack(side="left", fill="both", expand=True)

        self._build_sidebar()
        self._build_workspace()

    def _build_top_bar(self) -> None:
        top = tk.Frame(self.root, bg="#E2E0DB", height=48)
        top.pack(fill="x")
        top.pack_propagate(False)

        left = tk.Frame(top, bg="#E2E0DB")
        left.pack(side="left", fill="y", padx=16)
        tk.Label(left, text="[]", font=("Segoe UI", 11), bg="#E2E0DB", fg="#3A3A3A").pack(
            side="left", padx=(0, 18), pady=12
        )
        tk.Label(left, text="@", font=("Segoe UI", 11), bg="#E2E0DB", fg="#3A3A3A").pack(
            side="left", pady=12
        )

        right = tk.Frame(top, bg="#E2E0DB")
        right.pack(side="right", fill="y", padx=14)
        tk.Label(right, text="!", font=("Segoe UI", 11), bg="#E2E0DB", fg="#4A4A4A").pack(
            side="left", padx=(0, 22), pady=12
        )

        btn_min = tk.Label(
            right, text="-", font=("Segoe UI", 16), bg="#E2E0DB", fg="#2F2F2F", cursor="hand2"
        )
        btn_min.pack(side="left", padx=(0, 18), pady=6)
        btn_min.bind("<Button-1>", lambda _e: self.root.iconify())

        btn_max = tk.Label(
            right, text="[]", font=("Segoe UI", 10), bg="#E2E0DB", fg="#2F2F2F", cursor="hand2"
        )
        btn_max.pack(side="left", padx=(0, 18), pady=12)
        btn_max.bind(
            "<Button-1>",
            lambda _e: self.root.state("zoomed" if self.root.state() != "zoomed" else "normal"),
        )

        btn_close = tk.Label(
            right, text="x", font=("Segoe UI", 14), bg="#E2E0DB", fg="#2F2F2F", cursor="hand2"
        )
        btn_close.pack(side="left", pady=8)
        btn_close.bind("<Button-1>", lambda _e: self._on_close())

    def _build_sidebar(self) -> None:
        brand = tk.Frame(self.sidebar, bg="#E8E6E1")
        brand.pack(fill="x", padx=16, pady=(18, 16))

        tk.Label(
            brand,
            text="Flow",
            font=("Segoe UI", 36, "bold"),
            bg="#E8E6E1",
            fg="#292833",
        ).pack(side="left")

        badge = tk.Label(
            brand,
            text="Basic",
            font=("Segoe UI", 10, "bold"),
            bg="#F2EFE9",
            fg="#232323",
            padx=10,
            pady=5,
            bd=1,
            relief="solid",
        )
        badge.pack(side="left", padx=(10, 0), pady=(6, 0))

        nav = tk.Frame(self.sidebar, bg="#E8E6E1")
        nav.pack(fill="x", padx=12)

        nav_items = [
            ("home", "Home"),
            ("settings", "Settings"),
        ]

        for key, label in nav_items:
            btn = tk.Button(
                nav,
                text=label,
                anchor="w",
                bd=0,
                relief="flat",
                font=("Segoe UI", 11),
                bg="#E8E6E1",
                fg="#191919",
                activebackground="#DFDCD5",
                activeforeground="#111111",
                padx=14,
                pady=10,
                command=lambda k=key: self._on_nav_click(k),
            )
            btn.pack(fill="x", pady=3)
            self.nav_buttons[key] = btn

        self._set_active_nav("home")

    def _build_workspace(self) -> None:
        card = tk.Frame(
            self.workspace,
            bg="#F8F6F2",
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground="#DDD9D1",
        )
        card.pack(fill="both", expand=True, padx=8, pady=8)

        tk.Label(
            card,
            text="Welcome back",
            bg="#F8F6F2",
            fg="#171717",
            font=("Segoe UI", 26, "bold"),
        ).pack(anchor="w", padx=32, pady=(24, 16))

        top_row = tk.Frame(card, bg="#F8F6F2")
        top_row.pack(fill="x", padx=32)

        hero = tk.Canvas(
            top_row,
            width=760,
            height=220,
            bg="#0F1018",
            bd=0,
            highlightthickness=0,
        )
        hero.pack(side="left", fill="x", expand=True)
        hero.create_rectangle(0, 0, 760, 220, fill="#0D0E15", outline="#0D0E15")
        hero.create_oval(460, -160, 860, 280, fill="#8D5A2B", outline="#8D5A2B")
        hero.create_oval(530, -100, 960, 320, fill="#2D2120", outline="#2D2120")
        hero.create_rectangle(0, 0, 470, 220, fill="#07080F", outline="#07080F")
        hero.create_text(
            36,
            52,
            text="Make Flow sound like you",
            fill="#F7F4EE",
            font=("Georgia", 30),
            anchor="w",
        )
        hero.create_text(
            36,
            95,
            text="Set up writing style and dictation behavior for each app.",
            fill="#E8E2D8",
            font=("Segoe UI", 13),
            anchor="w",
        )

        self.record_button = tk.Button(
            hero,
            text="Start now",
            command=self._toggle_recording,
            bg="#F2EEE7",
            fg="#161616",
            font=("Segoe UI", 12, "bold"),
            bd=0,
            relief="flat",
            padx=18,
            pady=8,
            activebackground="#EAE4DA",
        )
        hero.create_window(112, 150, window=self.record_button)

        stats = tk.Frame(top_row, bg="#ECE8E1", width=235, bd=1, relief="solid")
        stats.pack(side="left", fill="y", padx=(18, 0))
        stats.pack_propagate(False)
        tk.Label(
            stats, textvariable=self.words_total_var, bg="#ECE8E1", fg="#1E1E1E", font=("Georgia", 34)
        ).pack(anchor="w", padx=20, pady=(28, 2))
        tk.Label(stats, text="total words", bg="#ECE8E1", fg="#282828", font=("Segoe UI", 12)).pack(
            anchor="w", padx=20
        )
        tk.Label(
            stats, textvariable=self.words_wpm_var, bg="#ECE8E1", fg="#1E1E1E", font=("Georgia", 30)
        ).pack(anchor="w", padx=20, pady=(20, 2))
        tk.Label(stats, text="wpm", bg="#ECE8E1", fg="#282828", font=("Segoe UI", 12)).pack(anchor="w", padx=20)
        tk.Label(
            stats, textvariable=self.words_streak_var, bg="#ECE8E1", fg="#1E1E1E", font=("Georgia", 30)
        ).pack(anchor="w", padx=20, pady=(20, 2))
        tk.Label(stats, text="day streak", bg="#ECE8E1", fg="#282828", font=("Segoe UI", 12)).pack(
            anchor="w", padx=20
        )

        status_row = tk.Frame(card, bg="#F8F6F2")
        status_row.pack(fill="x", padx=32, pady=(16, 10))
        self.server_status_label = tk.Label(
            status_row, textvariable=self.server_state_var, bg="#F8F6F2", fg="#2F4556", font=("Segoe UI", 11, "bold")
        )
        self.server_status_label.pack(side="left", padx=(0, 24))
        tk.Label(
            status_row, textvariable=self.mode_var, bg="#F8F6F2", fg="#344755", font=("Segoe UI", 11, "bold")
        ).pack(side="left", padx=(0, 24))
        tk.Label(
            status_row, textvariable=self.hotkey_label_var, bg="#F8F6F2", fg="#344755", font=("Segoe UI", 11, "bold")
        ).pack(side="left", padx=(0, 24))
        tk.Label(
            status_row, textvariable=self.latency_var, bg="#F8F6F2", fg="#344755", font=("Segoe UI", 11, "bold")
        ).pack(side="left")

        tk.Label(
            card,
            text="YESTERDAY",
            bg="#F8F6F2",
            fg="#747067",
            font=("Segoe UI", 12, "bold"),
        ).pack(anchor="w", padx=32, pady=(12, 10))

        self.history_rows_container = tk.Frame(
            card,
            bg="#FFFFFF",
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground="#E0DDD7",
        )
        self.history_rows_container.pack(fill="both", expand=True, padx=32, pady=(0, 18))

        self.preview_text = tk.Text(
            card,
            height=1,
            wrap="word",
            bg="#F8F6F2",
            fg="#2E2E2E",
            relief="flat",
            bd=0,
            highlightthickness=0,
        )
        self.preview_text.pack(fill="x", padx=32, pady=(0, 10))
        self.preview_text.insert("1.0", "")
        self.preview_text.configure(state="disabled")

    def _set_active_nav(self, key: str) -> None:
        self.current_nav = key
        for nav_key, btn in self.nav_buttons.items():
            if nav_key == key:
                btn.configure(bg="#DFDCD5", font=("Segoe UI", 11, "bold"))
            else:
                btn.configure(bg="#E8E6E1", font=("Segoe UI", 11))

    def _on_nav_click(self, key: str) -> None:
        self._set_active_nav(key)
        if key == "home":
            return
        if key == "settings":
            self._open_settings_modal()

    def _enumerate_microphones(self) -> list:
        devices = []
        pa = pyaudio.PyAudio()
        try:
            count = pa.get_device_count()
            for idx in range(count):
                try:
                    info = pa.get_device_info_by_index(idx)
                except Exception:
                    continue
                if int(info.get("maxInputChannels", 0)) > 0:
                    devices.append({"index": idx, "name": str(info.get("name", f"Mic {idx}"))})
        except Exception:
            return []
        finally:
            pa.terminate()
        return devices

    def _selected_mic_name(self) -> str:
        selected_idx = self.settings.get("mic_device_index")
        if selected_idx is None:
            return "Auto-detect"
        for item in self.mic_devices:
            if int(item["index"]) == int(selected_idx):
                return item["name"]
        return "Auto-detect"

    def _event_to_hotkey(self, event: tk.Event) -> Optional[str]:
        modifier_keys = {
            "Shift_L",
            "Shift_R",
            "Control_L",
            "Control_R",
            "Alt_L",
            "Alt_R",
            "Meta_L",
            "Meta_R",
            "Super_L",
            "Super_R",
            "Win_L",
            "Win_R",
        }
        keysym = str(event.keysym or "")
        if not keysym or keysym in modifier_keys:
            return None

        key_low = keysym.lower()
        if key_low in SPECIAL_VK:
            key_name = key_low.upper()
        elif len(keysym) == 1 and keysym.isalnum():
            key_name = keysym.upper()
        elif key_low.startswith("f") and key_low[1:].isdigit():
            fn = int(key_low[1:])
            if fn < 1 or fn > 24:
                return None
            key_name = f"F{fn}"
        else:
            return None

        parts = []
        state = int(getattr(event, "state", 0))
        if state & 0x0004:
            parts.append("Ctrl")
        if state & 0x0001:
            parts.append("Shift")
        if state & 0x0008:
            parts.append("Alt")

        parts.append(key_name)
        return "+".join(parts)

    def _capture_hotkey_dialog(self, parent: tk.Toplevel, current_hotkey: str) -> Optional[str]:
        dialog = tk.Toplevel(parent)
        dialog.title("Set Hotkey")
        dialog.configure(bg="#F5F3EF")
        dialog.transient(parent)
        dialog.grab_set()

        width = 560
        height = 280
        x_pos = parent.winfo_rootx() + max(20, (parent.winfo_width() - width) // 2)
        y_pos = parent.winfo_rooty() + max(20, (parent.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

        tk.Label(
            dialog,
            text="Press your new shortcut",
            bg="#F5F3EF",
            fg="#171717",
            font=("Segoe UI", 16, "bold"),
        ).pack(anchor="w", padx=22, pady=(20, 4))
        tk.Label(
            dialog,
            text="Example: hold Ctrl+Shift and press Space",
            bg="#F5F3EF",
            fg="#4A4742",
            font=("Segoe UI", 11),
        ).pack(anchor="w", padx=22)

        preview_var = tk.StringVar(value=current_hotkey)
        warning_var = tk.StringVar(value="")

        preview = tk.Label(
            dialog,
            textvariable=preview_var,
            bg="#FFFFFF",
            fg="#1E1E1E",
            font=("Segoe UI", 18, "bold"),
            bd=1,
            relief="solid",
            padx=18,
            pady=10,
        )
        preview.pack(fill="x", padx=22, pady=(16, 8))

        tk.Label(
            dialog,
            textvariable=warning_var,
            bg="#F5F3EF",
            fg="#8A2E2E",
            font=("Segoe UI", 10),
            anchor="w",
            justify="left",
        ).pack(fill="x", padx=22)

        result = {"value": None}

        def on_key_press(event: tk.Event) -> str:
            combo = self._event_to_hotkey(event)
            if not combo:
                return "break"
            try:
                _, _, display = parse_hotkey(combo)
            except Exception:
                warning_var.set("Unsupported combination. Try a letter, number, F-key, or Space.")
                return "break"

            preview_var.set(display)
            result["value"] = display
            if hotkey_is_unsafe(display):
                warning_var.set("Warning: this may conflict with common shortcuts like Ctrl+C/Ctrl+V.")
            else:
                warning_var.set("")
            return "break"

        entry = tk.Entry(
            dialog,
            textvariable=preview_var,
            bg="#FFFFFF",
            fg="#1F1F1F",
            relief="solid",
            bd=1,
            font=("Segoe UI", 11),
        )
        entry.pack(fill="x", padx=22, pady=(8, 0))
        entry.bind("<KeyPress>", on_key_press)
        dialog.bind("<KeyPress>", on_key_press)
        entry.focus_set()

        buttons = tk.Frame(dialog, bg="#F5F3EF")
        buttons.pack(fill="x", padx=22, pady=(18, 18))

        def cancel() -> None:
            dialog.destroy()

        def save() -> None:
            candidate = preview_var.get().strip()
            if not candidate:
                messagebox.showerror("Invalid Hotkey", "Hotkey cannot be empty.", parent=dialog)
                return
            try:
                _, _, display = parse_hotkey(candidate)
            except Exception as exc:
                messagebox.showerror("Invalid Hotkey", str(exc), parent=dialog)
                return
            result["value"] = display
            dialog.destroy()

        tk.Button(
            buttons,
            text="Cancel",
            command=cancel,
            bg="#EBE7DE",
            fg="#1E1E1E",
            bd=0,
            relief="flat",
            padx=16,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="right", padx=(8, 0))
        tk.Button(
            buttons,
            text="Save",
            command=save,
            bg="#DCD6CA",
            fg="#111111",
            bd=0,
            relief="flat",
            padx=18,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="right")

        dialog.wait_window()
        return result["value"]

    def _open_settings_modal(self) -> None:
        if self._settings_modal and self._settings_modal.winfo_exists():
            self._settings_modal.lift()
            self._settings_modal.focus_force()
            return

        modal = tk.Toplevel(self.root)
        self._settings_modal = modal
        modal.title("Settings")
        modal.configure(bg="#F3F1EC")
        modal.transient(self.root)
        modal.grab_set()

        width = 920
        height = 600
        x_pos = self.root.winfo_rootx() + max(20, (self.root.winfo_width() - width) // 2)
        y_pos = self.root.winfo_rooty() + max(20, (self.root.winfo_height() - height) // 2)
        modal.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

        left = tk.Frame(modal, bg="#ECE9E3", width=230)
        left.pack(side="left", fill="y")
        left.pack_propagate(False)

        right = tk.Frame(modal, bg="#F8F6F2")
        right.pack(side="left", fill="both", expand=True)

        tk.Label(left, text="SETTINGS", bg="#ECE9E3", fg="#4A4943", font=("Segoe UI", 11, "bold")).pack(
            anchor="w", padx=16, pady=(18, 8)
        )

        tk.Label(
            left,
            text="General",
            bg="#DFDCD5",
            fg="#22221F",
            font=("Segoe UI", 12, "bold"),
            anchor="w",
            padx=16,
            pady=10,
        ).pack(fill="x", padx=10, pady=2)

        tk.Label(left, text=f"Flow v{__version__}", bg="#ECE9E3", fg="#6B655C", font=("Segoe UI", 10)).pack(
            side="bottom", anchor="w", padx=16, pady=14
        )

        modal_hotkey = tk.StringVar(value=str(self.settings.get("hotkey", "F8")))
        modal_mic_name = tk.StringVar(value=self._selected_mic_name())
        modal_language = tk.StringVar(value=str(self.settings.get("language", "English")))
        modal_trailing = tk.BooleanVar(value=bool(self.settings.get("trailing_space", True)))
        modal_auto = tk.BooleanVar(value=bool(self.settings.get("auto_start_server", True)))
        modal_overlay = tk.BooleanVar(value=bool(self.settings.get("overlay_enabled", True)))
        modal_shortcut_display = tk.StringVar(value=f"Hold {modal_hotkey.get()} and speak")
        modal_mic_index_ref = {"value": self.settings.get("mic_device_index")}
        modal_lang_code_ref = {"value": self.settings.get("language_code", "en")}

        tk.Label(right, text="General", bg="#F8F6F2", fg="#171717", font=("Georgia", 36)).pack(
            anchor="w", padx=34, pady=(28, 12)
        )

        panel = tk.Frame(
            right,
            bg="#ECE9E3",
            bd=1,
            relief="solid",
            highlightthickness=1,
            highlightbackground="#DDD9D1",
        )
        panel.pack(fill="x", padx=34, pady=(4, 0))

        def add_row(title: str, subtitle_var: tk.StringVar, on_change):
            row = tk.Frame(panel, bg="#ECE9E3")
            row.pack(fill="x", padx=18, pady=6)

            left_col = tk.Frame(row, bg="#ECE9E3")
            left_col.pack(side="left", fill="x", expand=True)
            tk.Label(left_col, text=title, bg="#ECE9E3", fg="#1D1D1B", font=("Segoe UI", 14, "bold")).pack(
                anchor="w"
            )
            tk.Label(left_col, textvariable=subtitle_var, bg="#ECE9E3", fg="#383630", font=("Segoe UI", 12)).pack(
                anchor="w", pady=(2, 0)
            )

            tk.Button(
                row,
                text="Change",
                command=on_change,
                bg="#E2DED4",
                fg="#1E1E1E",
                bd=0,
                relief="flat",
                font=("Segoe UI", 12, "bold"),
                padx=16,
                pady=8,
                activebackground="#DAD5CB",
            ).pack(side="right")
            tk.Frame(panel, height=1, bg="#DCD8D0").pack(fill="x", padx=14)

        def change_hotkey() -> None:
            value = self._capture_hotkey_dialog(modal, modal_hotkey.get())
            if value is None:
                return
            value = value.strip()
            if not value:
                messagebox.showerror("Invalid Hotkey", "Hotkey cannot be empty.", parent=modal)
                return
            try:
                _, _, display = parse_hotkey(value)
            except Exception as exc:
                messagebox.showerror("Invalid Hotkey", str(exc), parent=modal)
                return
            modal_hotkey.set(display)
            modal_shortcut_display.set(f"Hold {display} and speak")

        def change_microphone() -> None:
            if not self.mic_devices:
                messagebox.showwarning("Microphone", "No input microphone devices found.", parent=modal)
                return

            entries = ["Auto-detect"] + [f"{item['index']}: {item['name']}" for item in self.mic_devices]
            selected = 0
            if modal_mic_index_ref["value"] is not None:
                for idx, item in enumerate(self.mic_devices, start=1):
                    if int(item["index"]) == int(modal_mic_index_ref["value"]):
                        selected = idx
                        break

            picked = self._pick_from_list(modal, "Select Microphone", entries, selected)
            if picked is None:
                return
            if picked == 0:
                modal_mic_index_ref["value"] = None
                modal_mic_name.set("Auto-detect")
            else:
                item = self.mic_devices[picked - 1]
                modal_mic_index_ref["value"] = int(item["index"])
                modal_mic_name.set(item["name"])

        def change_language() -> None:
            entries = ["English", "Bengali (Bangla)", "Hindi"]
            current_idx = 0
            for idx, item in enumerate(entries):
                if item == modal_language.get():
                    current_idx = idx
                    break
            picked = self._pick_from_list(modal, "Select Language", entries, current_idx)
            if picked is None:
                return
            modal_language.set(entries[picked])
            modal_lang_code_ref["value"] = {
                "English": "en",
                "Bengali (Bangla)": "bn",
                "Hindi": "hi",
            }[entries[picked]]

        add_row("Shortcuts", modal_shortcut_display, change_hotkey)
        add_row("Microphone", modal_mic_name, change_microphone)
        add_row("Languages", modal_language, change_language)

        toggles = tk.Frame(right, bg="#F8F6F2")
        toggles.pack(fill="x", padx=34, pady=(18, 0))
        tk.Checkbutton(
            toggles,
            text="Auto-start local server",
            variable=modal_auto,
            bg="#F8F6F2",
            fg="#202020",
            font=("Segoe UI", 11),
            selectcolor="#F8F6F2",
            activebackground="#F8F6F2",
        ).pack(anchor="w", pady=3)
        tk.Checkbutton(
            toggles,
            text="Add trailing space after paste",
            variable=modal_trailing,
            bg="#F8F6F2",
            fg="#202020",
            font=("Segoe UI", 11),
            selectcolor="#F8F6F2",
            activebackground="#F8F6F2",
        ).pack(anchor="w", pady=3)
        tk.Checkbutton(
            toggles,
            text="Show overlay pill",
            variable=modal_overlay,
            bg="#F8F6F2",
            fg="#202020",
            font=("Segoe UI", 11),
            selectcolor="#F8F6F2",
            activebackground="#F8F6F2",
        ).pack(anchor="w", pady=3)

        footer = tk.Frame(right, bg="#F8F6F2")
        footer.pack(fill="x", padx=34, pady=(24, 26))

        def close_modal() -> None:
            self._settings_modal = None
            try:
                modal.grab_release()
            except Exception:
                pass
            modal.destroy()
            self._set_active_nav("home")

        tk.Button(
            footer,
            text="Cancel",
            command=close_modal,
            bg="#EBE7DE",
            fg="#1E1E1E",
            bd=0,
            relief="flat",
            font=("Segoe UI", 11, "bold"),
            padx=18,
            pady=10,
            activebackground="#E1DCCF",
        ).pack(side="right", padx=(8, 0))

        self._modal_values = {
            "hotkey": modal_hotkey,
            "mic_name": modal_mic_name,
            "mic_index": modal_mic_index_ref,
            "language": modal_language,
            "language_code": modal_lang_code_ref,
            "trailing": modal_trailing,
            "auto_start": modal_auto,
            "overlay": modal_overlay,
        }

        tk.Button(
            footer,
            text="Apply",
            command=self._apply_modal_settings,
            bg="#DCD6CA",
            fg="#111111",
            bd=0,
            relief="flat",
            font=("Segoe UI", 11, "bold"),
            padx=22,
            pady=10,
            activebackground="#D1CBBD",
        ).pack(side="right")
        modal.protocol("WM_DELETE_WINDOW", close_modal)

    def _pick_from_list(self, parent: tk.Toplevel, title: str, entries: list, selected: int):
        dialog = tk.Toplevel(parent)
        dialog.title(title)
        dialog.configure(bg="#F5F3EF")
        dialog.transient(parent)
        dialog.grab_set()

        width = 500
        height = 420
        x_pos = parent.winfo_rootx() + max(20, (parent.winfo_width() - width) // 2)
        y_pos = parent.winfo_rooty() + max(20, (parent.winfo_height() - height) // 2)
        dialog.geometry(f"{width}x{height}+{x_pos}+{y_pos}")

        tk.Label(dialog, text=title, bg="#F5F3EF", fg="#1E1E1E", font=("Segoe UI", 14, "bold")).pack(
            anchor="w", padx=18, pady=(16, 8)
        )

        listbox = tk.Listbox(
            dialog,
            bg="#FFFFFF",
            fg="#1F1F1F",
            font=("Segoe UI", 11),
            activestyle="none",
            selectbackground="#D7D2C7",
            relief="solid",
            bd=1,
        )
        listbox.pack(fill="both", expand=True, padx=18, pady=(0, 12))

        for item in entries:
            listbox.insert("end", item)
        if 0 <= selected < len(entries):
            listbox.selection_set(selected)
            listbox.see(selected)

        result = {"index": None}

        def choose() -> None:
            picked = listbox.curselection()
            if not picked:
                return
            result["index"] = int(picked[0])
            dialog.destroy()

        buttons = tk.Frame(dialog, bg="#F5F3EF")
        buttons.pack(fill="x", padx=18, pady=(0, 16))

        tk.Button(
            buttons,
            text="Cancel",
            command=lambda: dialog.destroy(),
            bg="#EBE7DE",
            fg="#1E1E1E",
            bd=0,
            relief="flat",
            padx=14,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="right", padx=(8, 0))

        tk.Button(
            buttons,
            text="Select",
            command=choose,
            bg="#DCD6CA",
            fg="#121212",
            bd=0,
            relief="flat",
            padx=16,
            pady=8,
            font=("Segoe UI", 10, "bold"),
        ).pack(side="right")

        dialog.wait_window()
        return result["index"]

    def _apply_modal_settings(self) -> None:
        if not self._settings_modal or not self._settings_modal.winfo_exists():
            return

        modal = self._settings_modal
        values = self._modal_values
        old_settings = dict(self.settings)

        hotkey = values["hotkey"].get().strip()
        if not hotkey:
            messagebox.showerror("Settings Error", "Hotkey cannot be empty.", parent=modal)
            return

        try:
            _, _, display = parse_hotkey(hotkey)
        except Exception as exc:
            messagebox.showerror("Settings Error", str(exc), parent=modal)
            return

        if hotkey_is_unsafe(display):
            proceed = messagebox.askyesno(
                "Potential Hotkey Conflict",
                (
                    f"'{display}' may conflict with common system shortcuts.\n\n"
                    "Recommended keys: F8/F9/F10 or Ctrl+Shift+Space.\n\n"
                    "Do you still want to use it?"
                ),
                parent=modal,
            )
            if not proceed:
                values["hotkey"].set(old_settings.get("hotkey", "F8"))
                return

        self.settings.update(
            {
                "hotkey": display,
                "trailing_space": bool(values["trailing"].get()),
                "auto_start_server": bool(values["auto_start"].get()),
                "overlay_enabled": bool(values["overlay"].get()),
                "language": values["language"].get(),
                "language_code": values["language_code"]["value"],
                "mic_device_index": values["mic_index"]["value"],
            }
        )

        try:
            display = self.hotkey_listener.start(self.settings["hotkey"])
        except Exception as exc:
            self.settings = old_settings
            try:
                self._register_hotkey(show_error=False)
            except Exception:
                pass
            messagebox.showerror("Settings Error", f"Could not apply hotkey: {exc}", parent=modal)
            return

        self.hotkey_label_var.set(f"Hotkey: {display}")
        self.overlay.set_visible(bool(self.settings.get("overlay_enabled", True)))

        _safe_save_json(SETTINGS_PATH, self.settings)
        self._set_mode("idle", f"Press {display} to dictate")
        self._set_server_state("Server: Settings applied", ok=True)

        try:
            modal.grab_release()
        except Exception:
            pass
        modal.destroy()
        self._settings_modal = None
        self._set_active_nav("home")

    def _refresh_stats(self) -> None:
        total_words = sum(len(str(item.get("text", "")).split()) for item in self.history)
        self.words_total_var.set(f"{total_words:,}")

        if self.history:
            avg_latency = sum(int(item.get("latency_ms", 0)) for item in self.history) / max(1, len(self.history))
            avg_wpm = 120 if avg_latency < 500 else 85
        else:
            avg_wpm = 0
        self.words_wpm_var.set(str(int(avg_wpm)))
        self.words_streak_var.set(str(self._compute_streak_days()))

    def _compute_streak_days(self) -> int:
        if not self.history:
            return 0

        dates = set()
        for item in self.history:
            ts = str(item.get("time", "")).strip()
            if not ts:
                continue
            try:
                dt = datetime.strptime(ts, "%Y-%m-%d %H:%M:%S")
                dates.add(dt.date())
            except Exception:
                continue

        if not dates:
            return 0

        today = datetime.now().date()
        streak = 0
        cursor = today
        while cursor in dates:
            streak += 1
            cursor -= timedelta(days=1)
        return streak

    def _load_settings(self) -> dict:
        data = _safe_load_json(SETTINGS_PATH, DEFAULT_SETTINGS.copy())
        merged = DEFAULT_SETTINGS.copy()
        merged.update(data if isinstance(data, dict) else {})
        return merged

    def _load_history(self) -> list:
        data = _safe_load_json(HISTORY_PATH, [])
        if not isinstance(data, list):
            return []

        fixed = []
        for item in data:
            if not isinstance(item, dict):
                continue
            fixed.append(
                {
                    "time": str(item.get("time", "")),
                    "latency_ms": int(item.get("latency_ms", 0) or 0),
                    "text": str(item.get("text", "")).strip(),
                }
            )
        return fixed

    def _save_history(self) -> None:
        _safe_save_json(HISTORY_PATH, self.history[:MAX_HISTORY_ITEMS])

    def _set_mode(self, mode: str, detail: str) -> None:
        mode_map = {
            "idle": "Idle",
            "recording": "Recording",
            "transcribing": "Transcribing",
            "ready": "Done",
            "error": "Error",
        }
        self.mode_var.set(f"Mode: {mode_map.get(mode, mode.title())}")
        self.overlay.set_state(mode, detail)

    def _startup_server_if_enabled(self) -> None:
        if self.settings.get("auto_start_server", True):
            threading.Thread(target=self._start_server_if_needed, daemon=True).start()

    def _register_hotkey(self, show_error: bool) -> None:
        hotkey = self.settings.get("hotkey", "F8")
        try:
            display = self.hotkey_listener.start(hotkey)
            self.hotkey_label_var.set(f"Hotkey: {display}")
            self._set_mode("idle", f"Press {display} to dictate")
        except Exception as exc:
            self.hotkey_label_var.set("Hotkey: Not registered")
            self._set_mode("error", "Hotkey registration failed")
            _beep_warn()
            if show_error:
                messagebox.showerror("Hotkey Error", str(exc))

    def _on_hotkey_from_thread(self) -> None:
        self.event_queue.put(("hotkey_toggle", None))

    def _on_hotkey_error_from_thread(self, message: str) -> None:
        self.event_queue.put(("error", message))

    def _drain_events(self) -> None:
        while True:
            try:
                kind, payload = self.event_queue.get_nowait()
            except queue.Empty:
                break

            if kind == "hotkey_toggle":
                self._toggle_recording()
            elif kind == "transcription_done":
                self._on_transcription_done(payload)
            elif kind == "transcription_error":
                self._on_transcription_error(payload)
            elif kind == "error":
                self._set_mode("error", str(payload))
                self._set_server_state(f"Server: {payload}", ok=False)
        self.root.after(70, self._drain_events)

    def _start_capture_locked(self) -> None:
        self._recording = True
        self._audio_chunks = []
        self._capture_thread = threading.Thread(target=self._capture_loop, daemon=True)
        self._capture_thread.start()
        self.record_button.configure(text="Recording...")
        self._set_mode("recording", "Listening")
        _beep_ok()

    def _toggle_recording(self) -> None:
        with self._lock:
            if self._processing:
                _beep_warn()
                self._set_mode("error", "Still processing previous audio")
                return
            if not self._recording:
                self._start_capture_locked()
                return
            self._recording = False
            capture_thread = self._capture_thread

        if capture_thread:
            capture_thread.join(timeout=2.5)

        with self._lock:
            chunks = list(self._audio_chunks)
            self._audio_chunks = []
            self._processing = True

        self.record_button.configure(text="Start now")
        if not chunks:
            self._set_mode("error", "No audio captured")
            _beep_warn()
            with self._lock:
                self._processing = False
            return

        self._set_mode("transcribing", "Transcribing audio...")
        threading.Thread(target=self._transcribe_worker, args=(chunks,), daemon=True).start()

    def _force_stop_recording(self) -> None:
        with self._lock:
            if not self._recording:
                return
            self._recording = False
            capture_thread = self._capture_thread
        if capture_thread:
            capture_thread.join(timeout=2.5)
        self.record_button.configure(text="Start now")
        self._set_mode("idle", "Recording stopped")

    def _capture_loop(self) -> None:
        pa = pyaudio.PyAudio()
        stream = None
        preferred_device = self.settings.get("mic_device_index")
        try:
            open_kwargs = {
                "format": pyaudio.paInt16,
                "channels": CHANNELS,
                "rate": SAMPLE_RATE,
                "input": True,
                "frames_per_buffer": CHUNK_SIZE,
            }
            if preferred_device is not None:
                open_kwargs["input_device_index"] = int(preferred_device)
            try:
                stream = pa.open(**open_kwargs)
            except Exception:
                open_kwargs.pop("input_device_index", None)
                stream = pa.open(**open_kwargs)
            while True:
                with self._lock:
                    if not self._recording:
                        break
                chunk = stream.read(CHUNK_SIZE, exception_on_overflow=False)
                with self._lock:
                    self._audio_chunks.append(chunk)
        except Exception as exc:
            self.event_queue.put(("error", f"Capture failed: {exc}"))
            _beep_warn()
        finally:
            try:
                if stream:
                    stream.stop_stream()
                    stream.close()
            except Exception:
                pass
            pa.terminate()

    def _transcribe_worker(self, chunks) -> None:
        started = time.time()
        try:
            if not self._is_server_healthy():
                if not self._start_server_if_needed():
                    raise RuntimeError("Server is offline and auto-start failed.")

            text = _transcribe_chunks(chunks, ws_url=self.ws_url)
            latency = int((time.time() - started) * 1000)
            self.event_queue.put(
                (
                    "transcription_done",
                    {
                        "text": text,
                        "latency_ms": latency,
                    },
                )
            )
        except Exception as exc:
            traceback.print_exc()
            self.event_queue.put(("transcription_error", str(exc)))

    def _on_transcription_done(self, payload: dict) -> None:
        text = (payload.get("text") or "").strip()
        latency = int(payload.get("latency_ms") or 0)
        self.latency_var.set(f"Latency: {latency} ms")

        with self._lock:
            self._processing = False

        if not text:
            self._set_mode("error", "No speech detected")
            _beep_warn()
            return

        paste_text = f"{text} " if self.settings.get("trailing_space", True) else text
        try:
            _paste_text(paste_text)
        except Exception as exc:
            self._set_mode("error", f"Paste failed: {exc}")
            _beep_warn()
            return

        self._set_preview_text(text)
        self._add_history_item(text, latency)
        self._set_mode("ready", "Transcription pasted")
        _beep_ok()
        self.root.after(800, lambda: self._set_mode("idle", f"Press {self.settings['hotkey']} to dictate"))

    def _on_transcription_error(self, message: str) -> None:
        with self._lock:
            self._processing = False
        self._set_mode("error", message)
        _beep_warn()

    def _set_preview_text(self, text: str) -> None:
        self.preview_text.configure(state="normal")
        self.preview_text.delete("1.0", "end")
        self.preview_text.insert("1.0", text)
        self.preview_text.configure(state="disabled")

    def _add_history_item(self, text: str, latency_ms: int) -> None:
        item = {
            "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "latency_ms": latency_ms,
            "text": text,
        }
        self.history.insert(0, item)
        self.history = self.history[:MAX_HISTORY_ITEMS]
        self._save_history()
        self._refresh_history_list()
        self._refresh_stats()

    def _refresh_history_list(self) -> None:
        if not hasattr(self, "history_rows_container"):
            return

        for child in self.history_rows_container.winfo_children():
            child.destroy()

        if not self.history:
            tk.Label(
                self.history_rows_container,
                text="No transcriptions yet. Press your hotkey to start dictation.",
                bg="#FFFFFF",
                fg="#66615A",
                font=("Segoe UI", 12),
                anchor="w",
                padx=16,
                pady=16,
            ).pack(fill="x")
            return

        max_rows = min(16, len(self.history))
        for idx, entry in enumerate(self.history[:max_rows]):
            row = tk.Frame(self.history_rows_container, bg="#FFFFFF")
            row.pack(fill="x")

            time_text = str(entry.get("time", ""))
            try:
                dt = datetime.strptime(time_text, "%Y-%m-%d %H:%M:%S")
                time_label = dt.strftime("%I:%M %p")
            except Exception:
                time_label = time_text[:8] if time_text else "--:--"

            tk.Label(
                row,
                text=time_label,
                width=10,
                anchor="w",
                bg="#FFFFFF",
                fg="#5C5750",
                font=("Segoe UI", 18),
                padx=16,
                pady=12,
            ).pack(side="left", fill="y")

            preview = str(entry.get("text", "")).strip()
            if len(preview) > 190:
                preview = preview[:187].rstrip() + "..."

            tk.Label(
                row,
                text=preview,
                anchor="w",
                justify="left",
                bg="#FFFFFF",
                fg="#1F1F1F",
                font=("Segoe UI", 15),
                wraplength=900,
                padx=12,
                pady=12,
            ).pack(side="left", fill="x", expand=True)

            if idx != max_rows - 1:
                tk.Frame(self.history_rows_container, height=1, bg="#ECE9E3").pack(fill="x")

    def _on_history_select(self, _event=None) -> None:
        # Legacy hook retained for compatibility with older UI paths.
        return

    def _clear_history(self) -> None:
        if not messagebox.askyesno("Clear History", "Delete all stored transcription history?"):
            return
        self.history = []
        self._save_history()
        self._refresh_history_list()
        self._refresh_stats()

    def _is_server_healthy(self, timeout_s: float = 1.0) -> bool:
        try:
            response = httpx.get(self.health_url, timeout=timeout_s)
            return response.status_code == 200
        except Exception:
            return False

    def _set_server_state(self, text: str, ok: bool) -> None:
        def _apply() -> None:
            self.server_state_var.set(text)
            self.server_status_label.configure(fg="#356840" if ok else "#8A2E2E")

        if threading.current_thread() is threading.main_thread():
            _apply()
        else:
            self.root.after(0, _apply)

    def _start_server_if_needed(self) -> bool:
        if self._is_server_healthy():
            self._set_server_state("Server: Online", ok=True)
            return True

        if self._server_process and self._server_process.poll() is None:
            for _ in range(20):
                if self._is_server_healthy():
                    self._set_server_state("Server: Online", ok=True)
                    return True
                time.sleep(0.2)
            self._set_server_state("Server: Process running but unhealthy", ok=False)
            return False

        host = str(self.settings.get("host", "127.0.0.1"))
        port = str(self.settings.get("port", 8181))
        command = [
            sys.executable,
            "-m",
            "uvicorn",
            "whisperflow.fast_server:app",
            "--host",
            host,
            "--port",
            port,
        ]

        try:
            APP_DATA_DIR.mkdir(parents=True, exist_ok=True)
            self._server_log_file = open(SERVER_LOG_PATH, "a", encoding="utf-8")
            creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            self._server_process = subprocess.Popen(
                command,
                cwd=str(APP_ROOT),
                stdout=self._server_log_file,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            self._server_spawned_here = True
        except Exception as exc:
            self._set_server_state(f"Server: Failed to start ({exc})", ok=False)
            return False

        for _ in range(45):
            if self._is_server_healthy(timeout_s=0.8):
                self._set_server_state("Server: Online", ok=True)
                return True
            if self._server_process and self._server_process.poll() is not None:
                break
            time.sleep(0.2)

        self._set_server_state("Server: Start failed (see desktop_server.log)", ok=False)
        return False

    def _start_server_clicked(self) -> None:
        threading.Thread(target=self._start_server_if_needed, daemon=True).start()

    def _stop_server_clicked(self) -> None:
        self._stop_server()

    def _stop_server(self) -> None:
        if self._server_process and self._server_process.poll() is None:
            self._server_process.terminate()
            try:
                self._server_process.wait(timeout=4)
            except Exception:
                self._server_process.kill()
            self._set_server_state("Server: Stopped", ok=False)
        elif self._is_server_healthy():
            self._set_server_state("Server: Running externally", ok=True)
        else:
            self._set_server_state("Server: Offline", ok=False)

        if self._server_log_file:
            try:
                self._server_log_file.close()
            except Exception:
                pass
            self._server_log_file = None
        self._server_process = None
        self._server_spawned_here = False

    def _refresh_server_status(self) -> None:
        healthy = self._is_server_healthy(timeout_s=0.8)
        if healthy:
            self._set_server_state("Server: Online", ok=True)
        else:
            self._set_server_state("Server: Offline", ok=False)
        self.root.after(2200, self._refresh_server_status)

    def _save_settings(self) -> None:
        # Backward compatible entrypoint; primary settings flow is modal.
        self._open_settings_modal()

    def _on_close(self) -> None:
        try:
            self.hotkey_listener.stop()
        except Exception:
            pass

        with self._lock:
            self._recording = False

        if self._capture_thread and self._capture_thread.is_alive():
            self._capture_thread.join(timeout=1.2)

        if self._server_spawned_here:
            self._stop_server()

        self.root.destroy()


def main() -> None:
    root = tk.Tk()
    WhisperFlowDesktopApp(root)
    root.mainloop()


if __name__ == "__main__":
    main()
