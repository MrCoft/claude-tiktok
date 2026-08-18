#!/usr/bin/env python3
"""
claude-tiktok Stop hook (cross-platform).

Fires when Claude finishes a turn, but stays quiet while background work is
still running, so it only speaks up when the turn is really yours again.

With an API key (plugin userConfig, arriving as CLAUDE_PLUGIN_OPTION_API_KEY) it
reads a TikTok-voice summary of the last assistant message. Without one it just
plays the microwave ping and makes no network calls at all.

Voice, max words and speed are constants below.

Summaries switch themselves off after an auth/credit failure so a dead key
isn't retried every turn. The block is keyed to the key's fingerprint, so
pasting a different key clears it.

On any failure: log it, play microwave-ping.wav so the user isn't left
wondering, exit 0. Debug log: <tempdir>/claude-tiktok.log
"""
from __future__ import annotations

import base64
import hashlib
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

MAX_WORDS = 9
VOICE = "en_us_001"
SPEED_PERCENT = 130

SCRIPT_DIR = Path(__file__).resolve().parent
PLUGIN_ROOT = SCRIPT_DIR.parent
IS_WIN = sys.platform == "win32"
IS_MAC = sys.platform == "darwin"
PLATFORM_DIR = "win" if IS_WIN else "mac"
BIN_DIR = PLUGIN_ROOT / "bin" / PLATFORM_DIR
PING_PATH = SCRIPT_DIR / "microwave-ping.wav"
TEMP_DIR = Path(tempfile.gettempdir())
LOG_PATH = TEMP_DIR / "claude-tiktok.log"
LOG_MAX_BYTES = 512 * 1024
STATE_PATH = TEMP_DIR / "claude-tiktok-state.json"

# Task types that mean "Claude will wake itself up again", so the turn isn't
# really over. Names are the payload's display types; raw registry types are
# matched too in case that mapping changes. auto-mode scan and dream are
# internal housekeeping rather than the user's work, so they don't hold the
# notification back.
BLOCKING_TASK_TYPES = {
    "subagent", "local_agent",
    "workflow", "local_workflow",
    "shell", "local_bash",
    "monitor", "monitor_mcp", "monitor_ws",
    "MCP task", "mcp_task",
    "teammate", "in_process_teammate",
    "cloud session", "remote_agent",
}


API_KEY = os.environ.get("CLAUDE_PLUGIN_OPTION_API_KEY", "").strip()


def log(msg: str) -> None:
    try:
        if LOG_PATH.exists() and LOG_PATH.stat().st_size > LOG_MAX_BYTES:
            tail = LOG_PATH.read_bytes()[-LOG_MAX_BYTES // 2:]
            LOG_PATH.write_bytes(b"[log truncated]\n" + tail)
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        with LOG_PATH.open("a", encoding="utf-8") as f:
            f.write(f"{ts} [Stop] [pid {os.getpid()}] {msg}\n")
    except Exception:
        pass


def key_fingerprint() -> str:
    if not API_KEY:
        return ""
    return hashlib.sha256(API_KEY.encode("utf-8")).hexdigest()[:16]


def summaries_blocked_reason() -> str | None:
    """Why this key's summaries are switched off, or None if they're live."""
    try:
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return None
    if state.get("disabled_key") and state["disabled_key"] == key_fingerprint():
        return state.get("reason", "an earlier API failure")
    return None


def block_summaries(reason: str) -> None:
    try:
        STATE_PATH.write_text(
            json.dumps({"disabled_key": key_fingerprint(), "reason": reason}),
            encoding="utf-8",
        )
    except Exception as exc:
        log(f"could not write state file: {exc}")


# --- playback ---------------------------------------------------------------

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


def play_mp3_sync(mp3_path: Path) -> None:
    play_ping()
    time.sleep(0.2)

    mpg123 = BIN_DIR / ("mpg123.exe" if IS_WIN else "mpg123")
    sox = BIN_DIR / ("sox.exe" if IS_WIN else "sox")
    if not mpg123.exists() or not sox.exists():
        raise FileNotFoundError(f"missing decoder/sox in {BIN_DIR}")

    if IS_MAC:
        for b in (mpg123, sox):
            os.chmod(b, 0o755)
            subprocess.run(
                ["xattr", "-d", "com.apple.quarantine", str(b)],
                capture_output=True,
                check=False,
            )

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
        log(f"sox failed (exit {rc}); playing un-sped wav")
        play_wav_sync(decoded)
        return

    play_wav_sync(sped)
    log(f"sox tempo={tempo} played")


# --- turn inspection --------------------------------------------------------

def live_background_tasks(payload: dict) -> list[dict]:
    """Background work that will wake Claude up again after this Stop.

    Claude Code lists every running or pending backgrounded task in the Stop
    payload. An absent key means a build that doesn't report them, in which
    case we can't tell and treat the turn as finished.
    """
    tasks = payload.get("background_tasks")
    if not isinstance(tasks, list):
        return []
    return [
        t for t in tasks
        if isinstance(t, dict) and t.get("type") in BLOCKING_TASK_TYPES
    ]


def describe_tasks(tasks: list[dict]) -> str:
    return ", ".join(
        f"{t.get('type')}:{(t.get('description') or '?')[:40]}" for t in tasks
    )


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


def turn_text(payload: dict) -> str | None:
    text = (payload.get("last_assistant_message") or "").strip()
    if text:
        return text
    transcript = payload.get("transcript_path")
    return get_last_assistant_text(transcript) if transcript else None


# --- APIs -------------------------------------------------------------------

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
        # A rejected or unfunded key fails the same way on every later turn, so
        # stop asking. Rate limits and server errors are transient, keep those.
        fatal = exc.code in (401, 403) or (
            exc.code == 400 and "credit balance" in body_text.lower()
        )
        if fatal:
            block_summaries(f"HTTP {exc.code} from the Anthropic API")
            log(
                "voice summaries switched off for this key; ping only from now "
                f"on. Paste a working key, or delete {STATE_PATH}, to re-enable."
            )
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


# --- main -------------------------------------------------------------------

def summary_skip_reason() -> str | None:
    if not API_KEY:
        return "no API key configured"
    return summaries_blocked_reason()


def announce(payload: dict) -> None:
    skip = summary_skip_reason()
    if skip:
        log(f"ping only ({skip})")
        play_ping()
        return

    text = turn_text(payload)
    if not text:
        log("no assistant text found -> ping")
        play_ping()
        return
    log(f"got text len={len(text)}")

    # A summary is a nicety; a network or API hiccup shouldn't cost the user
    # their notification, and the reason is already logged where it happened.
    try:
        summary = invoke_haiku(text[:4000])
        if not summary:
            log("haiku returned empty -> ping")
            play_ping()
            return
        log(f"summary: {summary}")
        mp3 = invoke_tiktok_tts(summary)
        log(f"mp3 written: {mp3} ({mp3.stat().st_size} bytes)")
    except (urllib.error.URLError, OSError, ValueError, KeyError, RuntimeError) as exc:
        log(f"summary unavailable ({type(exc).__name__}: {exc}) -> ping")
        play_ping()
        return

    play_mp3_sync(mp3)
    log("playback done")


def main() -> int:
    log(
        f"hook fired; cwd={os.getcwd()}; platform={sys.platform}; "
        f"keyPresent={bool(API_KEY)}"
    )
    try:
        stdin_data = sys.stdin.read()
        payload = json.loads(stdin_data) if stdin_data.strip() else {}
        if not isinstance(payload, dict):
            payload = {}
        log(f"stdin bytes={len(stdin_data)}; keys={sorted(payload)}")

        pending = live_background_tasks(payload)
        if pending:
            log(f"silent: {len(pending)} live -> {describe_tasks(pending)}")
            return 0

        announce(payload)
        return 0
    except Exception as exc:
        log(f"EXCEPTION: {exc}\n{traceback.format_exc()}")
        try:
            play_ping()
        except Exception:
            pass
        return 0


if __name__ == "__main__":
    sys.exit(main())
