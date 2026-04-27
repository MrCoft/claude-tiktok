#!/usr/bin/env python3
"""
claude-tiktok Stop hook (cross-platform).

Read last assistant message from session transcript, summarize with Haiku,
synthesize via TikTok TTS, decode + apply 1.2x pitch-preserving tempo,
play via winsound (Windows) or afplay (macOS) using bundled mpg123 + SoX.

Config: API key injected by Claude Code from plugin userConfig as
CLAUDE_PLUGIN_OPTION_API_KEY. Voice, max words, and speed are constants
below.

On any failure: log it, play microwave-ping.wav fallback, exit 0 so the
user isn't left wondering. Debug log: <tempdir>/claude-tiktok.log
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

API_KEY = os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "")
MAX_WORDS = 9
VOICE = "en_us_001"
SPEED_PERCENT = 120

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
PLATFORM_DIR = "win" if IS_WIN else "mac"
BIN_DIR = PLUGIN_ROOT / "bin" / PLATFORM_DIR
PING_PATH = SCRIPT_DIR / "microwave-ping.wav"
TEMP_DIR = Path(tempfile.gettempdir())
LOG_PATH = TEMP_DIR / "claude-tiktok.log"


def log(msg: str) -> None:
    try:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{ts} [Stop] [pid {os.getpid()}] {msg}\n")
    except Exception:
        pass


def play_wav_sync(path: Path) -> None:
    if IS_WIN:
        import winsound
        winsound.PlaySound(str(path), winsound.SND_FILENAME)
    elif IS_MAC:
        subprocess.run(["afplay", str(path)], check=False)


def play_ping() -> None:
    try:
        if PING_PATH.exists():
            play_wav_sync(PING_PATH)
    except Exception as exc:
        log(f"play_ping failed: {exc}")


def play_fallback() -> None:
    play_ping()


def play_mp3_sync(mp3_path: Path) -> None:
    play_ping()
    time.sleep(0.2)

    mpg123 = BIN_DIR / ("mpg123.exe" if IS_WIN else "mpg123")
    sox = BIN_DIR / ("sox.exe" if IS_WIN else "sox")
    if not mpg123.exists() or not sox.exists():
        raise FileNotFoundError(f"missing decoder/sox in {BIN_DIR}")

    decoded = TEMP_DIR / "claude-tiktok-decoded.wav"
    sped = TEMP_DIR / "claude-tiktok-sped.wav"

    rc = subprocess.run(
        [str(mpg123), "-q", "-w", str(decoded), str(mp3_path)],
        capture_output=True,
    ).returncode
    if rc != 0:
        raise RuntimeError(f"mpg123 failed (exit {rc})")

    tempo = SPEED_PERCENT / 100.0
    rc = subprocess.run(
        [str(sox), "-q", str(decoded), str(sped), "tempo", str(tempo)],
        capture_output=True,
    ).returncode
    if rc != 0:
        raise RuntimeError(f"sox failed (exit {rc})")

    play_wav_sync(sped)
    log(f"sox tempo={tempo} played")


def get_last_assistant_text(transcript_path: str) -> str | None:
    p = Path(transcript_path)
    if not p.exists():
        return None
    try:
        lines = p.read_text(encoding="utf-8").splitlines()
    except Exception:
        return None
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except Exception:
            continue
        if entry.get("type") != "assistant":
            continue
        content = entry.get("message", {}).get("content")
        if not content:
            continue
        texts = [b.get("text", "") for b in content if b.get("type") == "text"]
        if texts:
            return "\n".join(texts)
    return None


def _post_json(url: str, body: dict, headers: dict, timeout: float) -> dict:
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json; charset=utf-8", **headers},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def invoke_haiku(text: str) -> str:
    if not API_KEY:
        raise RuntimeError("CLAUDE_PLUGIN_OPTION_API_KEY not set")
    prompt = (
        f"Summarize the message below in one short sentence (max {MAX_WORDS} words) "
        "to be spoken aloud. Lead with what happened and end with what's needed from "
        "the user; adapt if nothing is asked. Output ONLY the sentence, no quotes, "
        f"no preamble.\n\nMessage:\n{text}"
    )
    body = {
        "model": "claude-haiku-4-5-20251001",
        "max_tokens": 80,
        "messages": [{"role": "user", "content": prompt}],
    }
    headers = {"x-api-key": API_KEY, "anthropic-version": "2023-06-01"}
    try:
        resp = _post_json(
            "https://api.anthropic.com/v1/messages", body, headers, timeout=15.0
        )
    except urllib.error.HTTPError as exc:
        body_text = ""
        try:
            body_text = exc.read().decode("utf-8", errors="replace")
        except Exception:
            pass
        log(f"HAIKU ERROR status={exc.code} body: {body_text}")
        raise
    return resp["content"][0]["text"].strip()


def invoke_tiktok_tts(text: str) -> Path:
    body = {"text": text, "voice": VOICE}
    resp = _post_json(
        "https://ottsy.weilbyte.dev/api/generation",
        body,
        headers={},
        timeout=10.0,
    )
    if not resp.get("data"):
        raise RuntimeError("TTS returned no audio data")
    mp3_path = TEMP_DIR / "claude-tiktok.mp3"
    mp3_path.write_bytes(base64.b64decode(resp["data"]))
    return mp3_path


def main() -> int:
    log(
        f"hook fired; cwd={os.getcwd()}; keyPresent={bool(API_KEY)}; "
        f"platform={sys.platform}"
    )
    try:
        stdin_data = sys.stdin.read()
        log(f"stdin bytes={len(stdin_data)}")
        hook_input = json.loads(stdin_data) if stdin_data else None

        if not hook_input or not hook_input.get("transcript_path"):
            log("no transcript_path in payload -> fallback")
            play_fallback()
            return 0

        text = get_last_assistant_text(hook_input["transcript_path"])
        if not text:
            log("no assistant text found -> fallback")
            play_fallback()
            return 0
        log(f"got text len={len(text)}")
        if len(text) > 4000:
            text = text[:4000]

        summary = invoke_haiku(text)
        if not summary:
            log("haiku returned empty -> fallback")
            play_fallback()
            return 0
        log(f"summary: {summary}")

        mp3 = invoke_tiktok_tts(summary)
        log(f"mp3 written: {mp3} ({mp3.stat().st_size} bytes)")

        play_mp3_sync(mp3)
        log("playback done")
        return 0
    except Exception as exc:
        log(f"EXCEPTION: {exc}\n{traceback.format_exc()}")
        try:
            play_fallback()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
