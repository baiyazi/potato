"""Minimal Edge TTS synthesis core."""

from __future__ import annotations

import asyncio
import random
import re
import shutil
import subprocess
import tempfile
from pathlib import Path

import edge_tts
import requests
from edge_tts.exceptions import NoAudioReceived


LANGUAGES = [
    {"id": "zh-CN", "translate_code": "zh-CN", "name": "中文"},
    {"id": "en", "translate_code": "en", "name": "英语"},
    {"id": "ja", "translate_code": "ja", "name": "日语"},
    {"id": "ko", "translate_code": "ko", "name": "韩语"},
]

VOICES = [
    {"id": "zh-CN-XiaoxiaoNeural", "name": "晓晓", "detail": "女声 · 自然", "language": "zh-CN"},
    {"id": "zh-CN-XiaoyiNeural", "name": "晓伊", "detail": "女声 · 活泼", "language": "zh-CN"},
    {"id": "zh-CN-YunjianNeural", "name": "云健", "detail": "男声 · 纪录片", "language": "zh-CN"},
    {"id": "zh-CN-YunxiNeural", "name": "云希", "detail": "男声 · 年轻", "language": "zh-CN"},
    {"id": "zh-CN-YunyangNeural", "name": "云扬", "detail": "男声 · 新闻", "language": "zh-CN"},
    {"id": "zh-CN-YunyeNeural", "name": "云野", "detail": "男声 · 故事", "language": "zh-CN"},
    {"id": "zh-CN-liaoning-XiaobeiNeural", "name": "晓北", "detail": "女声 · 东北", "language": "zh-CN"},
    {"id": "en-US-JennyNeural", "name": "Jenny", "detail": "女声 · 美式", "language": "en"},
    {"id": "en-US-GuyNeural", "name": "Guy", "detail": "男声 · 美式", "language": "en"},
    {"id": "en-GB-SoniaNeural", "name": "Sonia", "detail": "女声 · 英式", "language": "en"},
    {"id": "en-GB-RyanNeural", "name": "Ryan", "detail": "男声 · 英式", "language": "en"},
    {"id": "ja-JP-NanamiNeural", "name": "Nanami", "detail": "女声", "language": "ja"},
    {"id": "ja-JP-KeitaNeural", "name": "Keita", "detail": "男声", "language": "ja"},
    {"id": "ko-KR-SunHiNeural", "name": "SunHi", "detail": "女声", "language": "ko"},
    {"id": "ko-KR-InJoonNeural", "name": "InJoon", "detail": "男声", "language": "ko"},
]

VOICE_BY_ID = {voice["id"]: voice for voice in VOICES}
LANGUAGE_BY_ID = {language["id"]: language for language in LANGUAGES}

EXACT_PAUSE_PATTERN = re.compile(
    r"\[\[\s*pause\s*:\s*(\d+(?:\.\d+)?)\s*(ms|s)\s*\]\]",
    re.IGNORECASE,
)
PAUSE_PATTERN = re.compile(
    r"\[\[\s*pause\s*:\s*(\d+(?:\.\d+)?)\s*(ms|s)\s*\]\]|(\|{1,3})",
    re.IGNORECASE,
)
SHORT_PAUSES = {"|": 0.3, "||": 0.7, "|||": 1.2}
MIN_PAUSE_SECONDS = 0.05
MAX_PAUSE_SECONDS = 10.0
MAX_TOTAL_PAUSE_SECONDS = 60.0
MAX_PAUSE_COUNT = 50
ESCAPED_PIPE = "\ue000"
ESCAPED_OPEN = "\ue001"


def get_voice(voice_id: str) -> dict:
    """Return validated voice metadata."""
    try:
        return VOICE_BY_ID[voice_id]
    except KeyError as exc:
        raise ValueError("不支持的音色") from exc


def _restore_escaped_text(text: str) -> str:
    return text.replace(ESCAPED_PIPE, "|").replace(ESCAPED_OPEN, "[[")


def _format_pause(seconds: float) -> str:
    milliseconds = round(seconds * 1000)
    if milliseconds < 1000:
        return f"[[pause:{milliseconds}ms]]"
    value = f"{seconds:.3f}".rstrip("0").rstrip(".")
    return f"[[pause:{value}s]]"


def parse_script(text: str) -> list[dict]:
    """Parse text and pause markers into validated synthesis tokens."""
    protected = text.replace(r"\|", ESCAPED_PIPE).replace(r"\[[", ESCAPED_OPEN)
    if re.search(r"\|{4,}", protected):
        raise ValueError("快捷停顿最多连续使用 3 个 | 字符")

    text_without_valid_markers = EXACT_PAUSE_PATTERN.sub("", protected)
    if re.search(r"\[\[\s*pause\b", text_without_valid_markers, re.IGNORECASE):
        raise ValueError("停顿语法错误，请使用 [[pause:800ms]] 或 [[pause:1.5s]]")

    tokens: list[dict] = []
    cursor = 0
    for match in PAUSE_PATTERN.finditer(protected):
        plain_text = _restore_escaped_text(protected[cursor:match.start()])
        if plain_text:
            tokens.append({"type": "text", "value": plain_text})

        if match.group(3):
            seconds = SHORT_PAUSES[match.group(3)]
        else:
            value = float(match.group(1))
            seconds = value / 1000 if match.group(2).lower() == "ms" else value

        if not MIN_PAUSE_SECONDS <= seconds <= MAX_PAUSE_SECONDS:
            raise ValueError("单次停顿必须在 50ms 到 10s 之间")
        tokens.append({"type": "pause", "seconds": seconds})
        cursor = match.end()

    remainder = _restore_escaped_text(protected[cursor:])
    if remainder:
        tokens.append({"type": "text", "value": remainder})

    pause_tokens = [token for token in tokens if token["type"] == "pause"]
    if len(pause_tokens) > MAX_PAUSE_COUNT:
        raise ValueError(f"停顿标记不能超过 {MAX_PAUSE_COUNT} 个")
    total_pause = sum(token["seconds"] for token in pause_tokens)
    if total_pause > MAX_TOTAL_PAUSE_SECONDS:
        raise ValueError("所有停顿时间合计不能超过 60 秒")
    if not any(token["type"] == "text" and token["value"].strip() for token in tokens):
        raise ValueError("请输入需要合成的文本")
    return tokens


def render_script(tokens: list[dict]) -> str:
    """Render tokens using the canonical pause syntax."""
    return "".join(
        token["value"] if token["type"] == "text" else _format_pause(token["seconds"])
        for token in tokens
    )


def script_stats(tokens: list[dict]) -> dict:
    pause_tokens = [token for token in tokens if token["type"] == "pause"]
    return {
        "pause_count": len(pause_tokens),
        "pause_seconds": round(sum(token["seconds"] for token in pause_tokens), 3),
        "spoken_characters": sum(
            len(token["value"].strip()) for token in tokens if token["type"] == "text"
        ),
    }


def translate_text(text: str, target_language: str) -> str:
    """Translate text for a foreign voice without requiring an API key."""
    language = LANGUAGE_BY_ID.get(target_language)
    if language is None:
        raise ValueError("不支持的翻译语言")
    if target_language == "zh-CN":
        return text

    chunks = [text[index:index + 450] for index in range(0, len(text), 450)]
    translated_chunks = []
    for chunk in chunks:
        response = requests.get(
            "https://api.mymemory.translated.net/get",
            params={
                "q": chunk,
                "langpair": f"zh-CN|{language['translate_code']}",
            },
            timeout=(5, 12),
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("responseStatus") != 200:
            raise RuntimeError(payload.get("responseDetails") or "翻译服务返回错误")
        translated_chunks.append(payload.get("responseData", {}).get("translatedText", ""))
    translated = "\n".join(part for part in translated_chunks if part).strip()
    if not translated:
        raise RuntimeError("翻译服务未返回文本")
    return translated


def translate_script(text: str, target_language: str) -> list[dict]:
    """Translate only spoken segments while retaining pause tokens."""
    tokens = parse_script(text)
    if target_language == "zh-CN":
        return tokens

    translated_tokens = []
    for token in tokens:
        if token["type"] == "pause":
            translated_tokens.append(token)
            continue
        value = token["value"]
        translated_value = translate_text(value, target_language) if value.strip() else value
        translated_tokens.append({"type": "text", "value": translated_value})
    return translated_tokens


def speed_to_rate(speed: float) -> str:
    """Convert a speed multiplier to Edge TTS rate syntax."""
    percentage = round((speed - 1.0) * 100)
    sign = "+" if percentage >= 0 else ""
    return f"{sign}{percentage}%"


async def synthesize(
    text: str,
    voice: str,
    speed: float,
    output_path: Path,
    retries: int = 3,
) -> Path:
    """Generate an MP3 file, retrying transient Edge TTS failures."""
    get_voice(voice)
    if not 0.5 <= speed <= 2.0:
        raise ValueError("语速必须在 0.5 到 2.0 之间")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    last_error: Exception | None = None

    for attempt in range(retries + 1):
        if attempt:
            await asyncio.sleep(min(2 ** (attempt - 1) + random.random(), 6))

        try:
            communicator = edge_tts.Communicate(
                text=text,
                voice=voice,
                rate=speed_to_rate(speed),
            )
            await communicator.save(str(output_path))
            if output_path.stat().st_size == 0:
                raise NoAudioReceived("生成的音频为空")
            return output_path
        except NoAudioReceived as exc:
            last_error = exc
        except Exception as exc:
            last_error = exc
            if attempt >= retries:
                break

    if output_path.exists():
        output_path.unlink()
    raise RuntimeError(f"语音生成失败：{last_error}") from last_error


def _run_ffmpeg(command: list[str]) -> None:
    result = subprocess.run(command, capture_output=True, text=True, check=False)
    if result.returncode != 0:
        error = result.stderr.strip().splitlines()
        detail = error[-1] if error else "未知错误"
        raise RuntimeError(f"音频合并失败：{detail}")


async def synthesize_script(
    tokens: list[dict],
    voice: str,
    speed: float,
    output_path: Path,
) -> Path:
    """Synthesize text segments and insert exact-duration silence between them."""
    if not any(token["type"] == "pause" for token in tokens):
        spoken_text = "".join(token["value"] for token in tokens if token["type"] == "text")
        return await synthesize(spoken_text, voice, speed, output_path)

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("使用自定义停顿需要安装 FFmpeg")

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix="potato-tts-") as temp_dir_name:
        temp_dir = Path(temp_dir_name)
        segment_paths: list[Path] = []

        for index, token in enumerate(tokens):
            segment_path = temp_dir / f"segment-{index:03d}.mp3"
            if token["type"] == "text":
                if not token["value"].strip():
                    continue
                await synthesize(token["value"], voice, speed, segment_path)
            else:
                _run_ffmpeg(
                    [
                        ffmpeg,
                        "-hide_banner",
                        "-loglevel",
                        "error",
                        "-f",
                        "lavfi",
                        "-i",
                        "anullsrc=r=24000:cl=mono",
                        "-t",
                        f"{token['seconds']:.3f}",
                        "-c:a",
                        "libmp3lame",
                        "-b:a",
                        "48k",
                        "-y",
                        str(segment_path),
                    ]
                )
            segment_paths.append(segment_path)

        concat_file = temp_dir / "segments.txt"
        concat_file.write_text(
            "".join(f"file '{path.as_posix()}'\n" for path in segment_paths),
            encoding="utf-8",
        )
        _run_ffmpeg(
            [
                ffmpeg,
                "-hide_banner",
                "-loglevel",
                "error",
                "-f",
                "concat",
                "-safe",
                "0",
                "-i",
                str(concat_file),
                "-ar",
                "24000",
                "-ac",
                "1",
                "-c:a",
                "libmp3lame",
                "-b:a",
                "48k",
                "-y",
                str(output_path),
            ]
        )

    if not output_path.is_file() or output_path.stat().st_size == 0:
        raise RuntimeError("音频合并后文件为空")
    return output_path
