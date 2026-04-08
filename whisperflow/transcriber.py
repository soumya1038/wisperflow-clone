"""Whisper model loader + lightweight transcription helpers."""

import asyncio
import io
import os
import threading
import wave
from concurrent.futures import ThreadPoolExecutor
from typing import Optional

import av
import numpy as np
from faster_whisper import WhisperModel


DEFAULT_MODEL = "tiny.en"
DEFAULT_DEVICE = os.getenv("WHISPERFLOW_DEVICE", "cpu").strip() or "cpu"
DEFAULT_COMPUTE_TYPE = os.getenv("WHISPERFLOW_COMPUTE_TYPE", "int8").strip() or "int8"
DEFAULT_BEAM_SIZE = max(1, int(os.getenv("WHISPERFLOW_BEAM_SIZE", "1")))
MAX_CONCURRENT_TRANSCRIBES = max(1, int(os.getenv("WHISPERFLOW_MAX_CONCURRENT_TRANSCRIBES", "2")))
SAMPLE_RATE = 16000
CHUNK_SECONDS = max(8.0, float(os.getenv("WHISPERFLOW_CHUNK_SECONDS", "16")))
CHUNK_OVERLAP_SECONDS = max(0.0, min(2.0, float(os.getenv("WHISPERFLOW_CHUNK_OVERLAP_SECONDS", "0.5"))))

_models: dict[str, WhisperModel] = {}
_transcribe_slots = threading.Semaphore(MAX_CONCURRENT_TRANSCRIBES)
_executor = ThreadPoolExecutor(max_workers=MAX_CONCURRENT_TRANSCRIBES)


def _resolve_model_source(file_name: Optional[str] = None) -> str:
    """Resolve model source from local folder first, then model alias."""
    candidate = (file_name or os.getenv("WHISPERFLOW_MODEL_NAME") or DEFAULT_MODEL).strip()

    # Backward compatibility with legacy tiny.en.pt naming.
    if candidate.endswith(".pt"):
        candidate = candidate[:-3]

    package_model_path = os.path.join(os.path.dirname(__file__), "models", candidate)

    if os.path.exists(package_model_path):
        return package_model_path
    if os.path.exists(candidate):
        return candidate

    return candidate


def _pcm_bytes_to_float32(audio_bytes: bytes) -> np.ndarray:
    """Convert PCM16 or WAV bytes to float32 mono waveform."""
    if not audio_bytes:
        return np.array([], dtype=np.float32)

    # WAV container path.
    if audio_bytes.startswith(b"RIFF") and b"WAVE" in audio_bytes[:16]:
        with wave.open(io.BytesIO(audio_bytes), "rb") as wav_file:
            frames = wav_file.readframes(wav_file.getnframes())
            channels = wav_file.getnchannels()
            sample_width = wav_file.getsampwidth()

        if sample_width != 2:
            raise ValueError("Only 16-bit WAV is supported.")

        pcm = np.frombuffer(frames, np.int16).astype(np.float32)
        if channels > 1:
            pcm = pcm.reshape(-1, channels).mean(axis=1)
        return pcm / 32768.0

    # Browser media containers (webm/ogg/mp4/m4a) decode path.
    is_container = (
        audio_bytes.startswith(b"\x1aE\xdf\xa3")  # webm/matroska
        or audio_bytes.startswith(b"OggS")  # ogg
        or (len(audio_bytes) > 8 and audio_bytes[4:8] == b"ftyp")  # mp4/m4a
    )
    if is_container:
        try:
            with av.open(io.BytesIO(audio_bytes), mode="r") as container:
                resampler = av.audio.resampler.AudioResampler(
                    format="s16",
                    layout="mono",
                    rate=16000,
                )
                chunks: list[np.ndarray] = []
                for frame in container.decode(audio=0):
                    resampled_frames = resampler.resample(frame)
                    if not isinstance(resampled_frames, list):
                        resampled_frames = [resampled_frames]
                    for resampled in resampled_frames:
                        chunk = resampled.to_ndarray()
                        if chunk.ndim == 2:
                            chunk = chunk[0]
                        chunks.append(chunk.astype(np.int16, copy=False))

            if not chunks:
                return np.array([], dtype=np.float32)

            pcm = np.concatenate(chunks).astype(np.float32)
            return pcm / 32768.0
        except Exception as exc:
            raise ValueError(
                "Unable to decode uploaded audio. Supported formats: PCM16, WAV, WebM, OGG, MP4/M4A."
            ) from exc

    # Raw PCM16 path.
    return np.frombuffer(audio_bytes, np.int16).astype(np.float32) / 32768.0


def get_model(file_name: Optional[str] = None) -> WhisperModel:
    """Load and cache a faster-whisper model."""
    source = _resolve_model_source(file_name)
    if source not in _models:
        _models[source] = WhisperModel(
            source,
            device=DEFAULT_DEVICE,
            compute_type=DEFAULT_COMPUTE_TYPE,
        )
    return _models[source]


def is_model_loaded(file_name: Optional[str] = None) -> bool:
    """Check whether the resolved model is already loaded in memory."""
    return _resolve_model_source(file_name) in _models


def preload_model(file_name: Optional[str] = None) -> WhisperModel:
    """Force model load and return it."""
    return get_model(file_name)


def transcribe_pcm_chunks(
    model: WhisperModel,
    chunks: list,
    lang: str = "en",
    temperature: float = 0.0,
    log_prob: float = -0.8,
) -> dict:
    """Transcribe PCM/WAV chunks and return Whisper-style dict."""
    audio_bytes = b"".join(chunks)
    audio = _pcm_bytes_to_float32(audio_bytes)
    if audio.size == 0:
        return {"text": "", "language": lang, "segments": []}

    def _run_one_pass(audio_slice: np.ndarray):
        segments, info = model.transcribe(
            audio_slice,
            language=lang or None,
            beam_size=DEFAULT_BEAM_SIZE,
            temperature=temperature,
            log_prob_threshold=log_prob,
            vad_filter=True,
            vad_parameters={
                "min_silence_duration_ms": 250,
                "speech_pad_ms": 80,
            },
            condition_on_previous_text=False,
            word_timestamps=False,
        )
        segment_list = list(segments)
        text_value = " ".join(seg.text.strip() for seg in segment_list if seg.text.strip()).strip()
        return text_value, info

    def _join_texts(parts: list[str]) -> str:
        if not parts:
            return ""
        result = parts[0].strip()
        for nxt in parts[1:]:
            nxt = nxt.strip()
            if not nxt:
                continue
            rw = result.split()
            nw = nxt.split()
            merged = False
            for n in range(min(8, len(rw), len(nw)), 2, -1):
                if rw[-n:] == nw[:n]:
                    result = (result + " " + " ".join(nw[n:])).strip()
                    merged = True
                    break
            if not merged:
                result = (result + " " + nxt).strip()
        return result

    with _transcribe_slots:
        duration_s = len(audio) / SAMPLE_RATE
        if duration_s <= CHUNK_SECONDS:
            text, info = _run_one_pass(audio)
        else:
            chunk_n = int(CHUNK_SECONDS * SAMPLE_RATE)
            overlap_n = int(CHUNK_OVERLAP_SECONDS * SAMPLE_RATE)
            step_n = max(1, chunk_n - overlap_n)
            texts: list[str] = []
            info = None

            offset = 0
            while offset < len(audio):
                end = min(offset + chunk_n, len(audio))
                chunk = audio[offset:end]
                if len(chunk) < int(SAMPLE_RATE * 0.25):
                    break
                chunk_text, chunk_info = _run_one_pass(chunk)
                if chunk_text:
                    texts.append(chunk_text)
                if info is None:
                    info = chunk_info
                if end >= len(audio):
                    break
                offset += step_n

            text = _join_texts(texts)
            if info is None:
                class _Info:
                    language = lang

                info = _Info()

    return {
        "text": text,
        "language": getattr(info, "language", lang),
        "segments": [],
    }


async def transcribe_pcm_chunks_async(
    model: WhisperModel,
    chunks: list,
    lang: str = "en",
    temperature: float = 0.0,
    log_prob: float = -0.8,
) -> dict:
    """Async transcription wrapper bounded by a small thread pool."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(
        _executor,
        transcribe_pcm_chunks,
        model,
        chunks,
        lang,
        temperature,
        log_prob,
    )
