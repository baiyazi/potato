"""Local web interface for the standalone Potato TTS tool."""

from __future__ import annotations

import argparse
import asyncio
import json
import mimetypes
import uuid
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import unquote, urlparse

from tts import (
    LANGUAGES,
    VOICES,
    get_voice,
    render_script,
    script_stats,
    synthesize_script,
    translate_script,
)


ROOT = Path(__file__).resolve().parent
INDEX_FILE = ROOT / "index.html"
OUTPUT_DIR = ROOT / "outputs"
MAX_TEXT_LENGTH = 5000


class AppHandler(BaseHTTPRequestHandler):
    server_version = "PotatoTTS/0.1"

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/":
            self._send_file(INDEX_FILE, "text/html; charset=utf-8")
            return
        if path == "/api/voices":
            self._send_json({"languages": LANGUAGES, "voices": VOICES})
            return
        if path.startswith("/audio/"):
            filename = unquote(path.removeprefix("/audio/"))
            if Path(filename).name != filename or not filename.endswith(".mp3"):
                self._send_json({"error": "无效文件名"}, HTTPStatus.BAD_REQUEST)
                return
            audio_file = OUTPUT_DIR / filename
            if not audio_file.is_file():
                self._send_json({"error": "音频不存在"}, HTTPStatus.NOT_FOUND)
                return
            self._send_file(audio_file, mimetypes.types_map.get(".mp3", "audio/mpeg"))
            return
        self._send_json({"error": "页面不存在"}, HTTPStatus.NOT_FOUND)

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        if path not in {"/api/tts", "/api/translate"}:
            self._send_json({"error": "接口不存在"}, HTTPStatus.NOT_FOUND)
            return

        try:
            payload = self._read_payload()
            text = str(payload.get("text", "")).strip()
            self._validate_text(text)

            if path == "/api/translate":
                target_language = str(payload.get("target_language", ""))
                translated_tokens = translate_script(text, target_language)
                self._send_json(
                    {
                        "translated_text": render_script(translated_tokens),
                        "target_language": target_language,
                        **script_stats(translated_tokens),
                    }
                )
                return

            voice = str(payload.get("voice", "zh-CN-XiaoxiaoNeural"))
            speed = float(payload.get("speed", 1.0))
            voice_info = get_voice(voice)
            target_language = voice_info["language"]
            spoken_tokens = translate_script(text, target_language)
            spoken_text = render_script(spoken_tokens)
            stats = script_stats(spoken_tokens)
            filename = f"voice-{uuid.uuid4().hex}.mp3"
            output_path = OUTPUT_DIR / filename
            asyncio.run(synthesize_script(spoken_tokens, voice, speed, output_path))
            self._send_json(
                {
                    "audio_url": f"/audio/{filename}",
                    "filename": filename,
                    "characters": stats["spoken_characters"],
                    "spoken_text": spoken_text,
                    "translated": target_language != "zh-CN",
                    "target_language": target_language,
                    **stats,
                }
            )
        except (ValueError, json.JSONDecodeError) as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            self._send_json({"error": str(exc)}, HTTPStatus.INTERNAL_SERVER_ERROR)

    def _read_payload(self) -> dict:
        content_length = int(self.headers.get("Content-Length", "0"))
        if content_length <= 0 or content_length > 100_000:
            raise ValueError("请求内容无效")
        return json.loads(self.rfile.read(content_length))

    @staticmethod
    def _validate_text(text: str) -> None:
        if not text:
            raise ValueError("请输入需要合成的文本")
        if len(text) > MAX_TEXT_LENGTH:
            raise ValueError(f"文本不能超过 {MAX_TEXT_LENGTH} 个字符")

    def log_message(self, fmt: str, *args: object) -> None:
        print(f"[{self.log_date_time_string()}] {fmt % args}")

    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def _send_file(self, path: Path, content_type: str) -> None:
        data = path.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)


def main() -> None:
    parser = argparse.ArgumentParser(description="Potato standalone TTS tool")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8765, type=int)
    parser.add_argument("--no-browser", action="store_true")
    args = parser.parse_args()

    OUTPUT_DIR.mkdir(exist_ok=True)
    server = ThreadingHTTPServer((args.host, args.port), AppHandler)
    url = f"http://{args.host}:{args.port}"
    print(f"Potato 已启动：{url}")
    print("按 Control+C 停止。")

    if not args.no_browser:
        webbrowser.open(url)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在停止……")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
