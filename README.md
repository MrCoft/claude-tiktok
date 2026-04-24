# claude-tiktok

Replaces Claude Code's usual silence / terminal bell with a short **TikTok-voice** soundbite summarizing what Claude just asked you.

When Claude stops and waits for input (or fires a permission notification), this plugin:
1. Reads Claude's last message from the session transcript
2. Summarizes it to one punchy sentence via Claude Haiku
3. Sends that sentence to a TikTok-voice TTS endpoint
4. Plays the resulting MP3 through your speakers

If any step fails, you hear a short console beep instead — you're never left wondering whether Claude is waiting for you.

## Requirements

- **Windows** (uses Windows PowerShell 5.1 and Win32 MCI for audio; no macOS/Linux support yet)
- An **Anthropic API key** with prepaid credits (separate from your Claude Code subscription). Haiku 4.5 costs ~$0.0002 per summary, so $5 of credits lasts roughly 20,000 hook firings.
- Internet access to:
  - `api.anthropic.com` — summarization
  - `tiktok-tts.weilnet.workers.dev` — TikTok voice synthesis (community-run, occasionally goes down)

No other external dependencies. The plugin bundles **mpg123** (MP3 decoder) and **SoX** (audio processor) in `bin/` — ~4.4 MB, used together for pitch-preserving time-stretched playback. Licenses included (`LICENSE.mpg123.txt`, `LICENSE.sox.txt`).

## Install

```text
/plugin marketplace add <marketplace-source>
/plugin install claude-tiktok@<marketplace-name>
```

Claude Code will prompt you for:

| Field | Purpose |
|-------|---------|
| **Anthropic API key** | Stored in OS secure storage. Get one at https://console.anthropic.com, load credits. |
| **Max summary words** | Defaults to 9 (~3.5s of speech). Cap on spoken length. |

Voice (`en_us_001`, Jessie) and playback speed (120%) are hardcoded in `hooks/tts-hook.ps1`. Edit those two variables near the top of the script if you want to change them.

## Changing config later

```text
/plugin
```

Pick `claude-tiktok` → **Configure** → re-prompts. Disable the whole plugin from the same menu.

## How it works

- `hooks/hooks.json` registers `Stop` and `Notification` hooks pointing at `hooks/tts-hook.ps1`.
- Claude Code injects your config as env vars (`CLAUDE_PLUGIN_OPTION_API_KEY`, `_VOICE`, `_MAX_WORDS`) when it runs the script.
- The script reads the session transcript from `transcript_path` in the hook stdin payload, extracts the last assistant text, and runs the summarize → synthesize → play pipeline.
- Playback uses Win32 MCI (`mciSendString`) because COM-based players (`WMPlayer.OCX`) silently fail in non-UI subprocesses.

## Troubleshooting

**I hear the fallback beep instead of a voice.** Something in the pipeline failed. Open the debug log:

```powershell
notepad $env:TEMP\claude-tiktok.log
```

Common causes, from the log line where it died:

- `EXCEPTION: (400) Bad Request` with `credit balance is too low` → [load credits](https://console.anthropic.com/settings/billing).
- `EXCEPTION: (401) Unauthorized` → API key is wrong; reconfigure via `/plugin`.
- `EXCEPTION: ... surrogates not allowed` → shouldn't happen (this is the known PS 5.1 UTF-8 body bug; the plugin already works around it). File an issue.
- `HAIKU ERROR body: rate_limit_error` → you're sending too many summaries too fast; wait a minute.
- `MCI open rc=<nonzero>` → audio subsystem problem; unusual on Windows with working audio.

**Audio plays but it's the wrong voice.** Check the `voice` config in `/plugin → claude-tiktok → Configure`. Must be a valid TikTok voice ID (not a human-readable name).

**Nothing happens at all.** The plugin needs a `/hooks` reload or full Claude Code restart after install for the env to propagate.

## Limits

- Windows-only for now. MCI is the Win32 audio API; macOS (`afplay`) and Linux (`aplay`/`paplay`) would need separate backends.
- The TikTok TTS worker is community-run — if it goes down, every Stop falls back to beep until it's restored or the endpoint is swapped (`Invoke-TikTokTts` in `tts-hook.ps1`).
- The first ~half-second of each Stop is blocked on Haiku latency. Usually unnoticeable but worth knowing.

## License

MIT.
