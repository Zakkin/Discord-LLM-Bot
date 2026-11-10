"""音声・動画メディアの前処理・文字起こし・キーフレーム要約モジュール。"""
from __future__ import annotations

import asyncio
import base64
import logging
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any

from ollama_bot.common.config_helpers import cfg, cfg_bool, cfg_int, cfg_float
from front.discord.common.message_media import _download_bytes, describe_images_with_vision_model
from ollama_bot.common.http_session import get_shared_http_session

log = logging.getLogger("front.discord.common.message_media_av")

AUDIO_EXTENSIONS = {".ogg", ".mp3", ".m4a", ".wav"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm"}


def is_audio_attachment(att: Any) -> bool:
    """添付ファイルが音声であるかを判定する"""
    content_type = str(getattr(att, "content_type", "") or "").lower()
    if content_type.startswith("audio/"):
        return True
    filename = str(getattr(att, "filename", "") or "").lower()
    return any(filename.endswith(ext) for ext in AUDIO_EXTENSIONS)


def is_video_attachment(att: Any) -> bool:
    """添付ファイルが動画であるかを判定する"""
    content_type = str(getattr(att, "content_type", "") or "").lower()
    if content_type.startswith("video/"):
        return True
    filename = str(getattr(att, "filename", "") or "").lower()
    return any(filename.endswith(ext) for ext in VIDEO_EXTENSIONS)


async def transcribe_audio(file_path: Path, *, model: str, timeout_sec: float) -> str | None:
    """OpenAI互換の /v1/audio/transcriptions エンドポイントを呼び出して音声を文字起こしする"""
    api_base = os.environ.get("OPENAI_API_BASE") or os.environ.get("OPENAI_BASE_URL", "https://api.openai.com/v1")
    api_key = os.environ.get("OPENAI_API_KEY", "")
    endpoint = f"{api_base.rstrip('/')}/audio/transcriptions"

    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    headers["User-Agent"] = "discord-ai-bot/1.0"

    try:
        session = await get_shared_http_session(timeout_sec=timeout_sec, headers=headers)
        import aiohttp
        data = aiohttp.FormData()
        data.add_field("model", model)
        data.add_field("file", file_path.read_bytes(), filename=file_path.name)

        async with session.post(endpoint, data=data) as resp:
            if resp.status == 200:
                result = await resp.json()
                return str(result.get("text", "")).strip()
            log.warning("transcribe_audio failed status=%d: %s", resp.status, await resp.text())
            return None
    except Exception as e:
        log.warning("transcribe_audio exception: %r", e)
        return None


async def build_audio_analysis_text(attachment: Any, *, idx: int) -> str | None:
    """音声添付ファイルを文字起こしして整形テキストを返す"""
    if not cfg_bool("MEDIA_AUDIO_ENABLED", False):
        return None

    max_mb = cfg_int("MEDIA_DOWNLOAD_MAX_MB", 25)
    max_bytes = max_mb * 1024 * 1024
    timeout_sec = cfg_float("MEDIA_ASR_TIMEOUT_SEC", 60.0)
    model = str(cfg("MEDIA_ASR_MODEL", "whisper-1") or "whisper-1")

    url = getattr(attachment, "url", None)
    if not url:
        return None

    data = await _download_bytes(str(url), max_bytes=max_bytes, timeout_sec=timeout_sec)
    if not data:
        return None

    suffix = Path(getattr(attachment, "filename", "audio.ogg") or "audio.ogg").suffix or ".ogg"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)

    try:
        text = await transcribe_audio(tmp_path, model=model, timeout_sec=timeout_sec)
        if text:
            return f"添付音声{idx}の文字起こし:\n{text}"
        return None
    finally:
        if tmp_path.exists():
            tmp_path.unlink()


async def build_video_analysis_text(attachment: Any, *, idx: int) -> str | None:
    """動画添付ファイルからキーフレームを抽出しVision要約（＋音声文字起こし）して整形テキストを返す"""
    if not cfg_bool("MEDIA_VIDEO_ENABLED", False):
        return None

    if not (shutil.which("ffmpeg") and shutil.which("ffprobe")):
        log.warning("ffmpeg or ffprobe not found in PATH; skipping video analysis")
        return None

    max_mb = cfg_int("MEDIA_DOWNLOAD_MAX_MB", 25)
    max_bytes = max_mb * 1024 * 1024
    timeout_sec = cfg_float("MEDIA_ASR_TIMEOUT_SEC", 60.0)
    max_frames = cfg_int("MEDIA_VIDEO_MAX_FRAMES", 4)
    model = str(cfg("MEDIA_ASR_MODEL", "whisper-1") or "whisper-1")

    url = getattr(attachment, "url", None)
    if not url:
        return None

    data = await _download_bytes(str(url), max_bytes=max_bytes, timeout_sec=timeout_sec)
    if not data:
        return None

    suffix = Path(getattr(attachment, "filename", "video.mp4") or "video.mp4").suffix or ".mp4"
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as tmp:
        tmp.write(data)
        video_path = Path(tmp.name)

    tmp_dir = Path(tempfile.mkdtemp(prefix="video_av_"))
    try:
        # 1. ffprobe で動画の長さを取得
        proc = await asyncio.create_subprocess_exec(
            "ffprobe", "-v", "error", "-show_entries", "format=duration",
            "-of", "default=noprint_wrappers=1:nokey=1", str(video_path),
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE
        )
        stdout, _ = await proc.communicate()
        try:
            duration = float(stdout.decode().strip())
        except (ValueError, TypeError):
            duration = 0.0

        # 2. キーフレームの抽出タイムスタンプ計算（0%, 33%, 66%, 90%）
        timestamps = [0.1]
        if duration > 1.0 and max_frames > 1:
            ratios = [0.33, 0.66, 0.90][:max_frames - 1]
            timestamps.extend([duration * r for r in ratios])
        timestamps = timestamps[:max_frames]

        frame_b64_list: list[str] = []
        for f_idx, ts in enumerate(timestamps):
            frame_path = tmp_dir / f"frame_{f_idx}.jpg"
            ff_proc = await asyncio.create_subprocess_exec(
                "ffmpeg", "-y", "-ss", f"{ts:.2f}", "-i", str(video_path),
                "-frames:v", "1", "-q:v", "2", str(frame_path),
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
            )
            await ff_proc.communicate()
            if frame_path.exists() and frame_path.stat().st_size > 0:
                frame_b64_list.append(base64.b64encode(frame_path.read_bytes()).decode("ascii"))

        captions = await describe_images_with_vision_model(frame_b64_list) if frame_b64_list else []
        caption_text = "\n".join(f"- {c}" for c in captions) if captions else "（映像内容の抽出なし）"

        # 3. 音声トラックの抽出と文字起こし
        audio_extracted_path = tmp_dir / "extracted_audio.mp3"
        ff_audio = await asyncio.create_subprocess_exec(
            "ffmpeg", "-y", "-i", str(video_path), "-vn", "-acodec", "libmp3lame", str(audio_extracted_path),
            stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL
        )
        await ff_audio.communicate()

        transcript: str | None = None
        if audio_extracted_path.exists() and audio_extracted_path.stat().st_size > 1000:
            transcript = await transcribe_audio(audio_extracted_path, model=model, timeout_sec=timeout_sec)

        result_parts = [f"添付動画{idx}の解析内容:\n{caption_text}"]
        if transcript:
            result_parts.append(f"音声の文字起こし:\n{transcript}")
        return "\n".join(result_parts)
    except Exception as e:
        log.warning("build_video_analysis_text exception: %r", e)
        return None
    finally:
        if video_path.exists():
            video_path.unlink()
        shutil.rmtree(tmp_dir, ignore_errors=True)


__all__ = [
    "AUDIO_EXTENSIONS",
    "VIDEO_EXTENSIONS",
    "is_audio_attachment",
    "is_video_attachment",
    "transcribe_audio",
    "build_audio_analysis_text",
    "build_video_analysis_text",
]
