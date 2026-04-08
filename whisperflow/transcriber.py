"""Whisper model loader + transcription helpers."""

import os
import asyncio
from typing import Optional

import torch
import numpy as np

import whisper
from whisper import Whisper


DEFAULT_MODEL = "tiny.en.pt"
_models = {}


def _resolve_model_source(file_name: Optional[str] = None) -> str:
    """Resolve model source from local file first, then Whisper model alias."""
    candidate = (file_name or os.getenv("WHISPERFLOW_MODEL_NAME") or DEFAULT_MODEL).strip()
    package_model_path = os.path.join(os.path.dirname(__file__), "models", candidate)

    if os.path.exists(package_model_path):
        return package_model_path
    if os.path.exists(candidate):
        return candidate

    # Whisper aliases are usually "tiny", "base", "small", etc. without ".pt".
    if candidate.endswith(".pt"):
        return candidate[:-3]
    return candidate


def get_model(file_name: Optional[str] = None) -> Whisper:
    """Load and cache a Whisper model."""
    source = _resolve_model_source(file_name)
    if source not in _models:
        _models[source] = whisper.load_model(source).to(
            "cuda" if torch.cuda.is_available() else "cpu"
        )
    return _models[source]


def is_model_loaded(file_name: Optional[str] = None) -> bool:
    """Check whether the resolved model is already loaded in memory."""
    return _resolve_model_source(file_name) in _models


def preload_model(file_name: Optional[str] = None) -> Whisper:
    """Force model load and return it."""
    return get_model(file_name)


def transcribe_pcm_chunks(
    model: Whisper, chunks: list, lang="en", temperature=0.1, log_prob=-0.5
) -> dict:
    """transcribes pcm chunks list"""
    arr = (
        np.frombuffer(b"".join(chunks), np.int16).flatten().astype(np.float32) / 32768.0
    )
    return model.transcribe(
        arr,
        fp16=False,
        language=lang,
        logprob_threshold=log_prob,
        temperature=temperature,
    )


async def transcribe_pcm_chunks_async(
    model: Whisper, chunks: list, lang="en", temperature=0.1, log_prob=-0.5
) -> dict:
    """transcribes pcm chunks async"""
    return await asyncio.get_running_loop().run_in_executor(
        None, transcribe_pcm_chunks, model, chunks, lang, temperature, log_prob
    )
