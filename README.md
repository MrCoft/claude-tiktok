# claude-tiktok

Plays a TikTok-voice spoken summary of Claude's last message when it stops and waits for your input. Replaces Claude Code's default silence / terminal bell with a short attention-grabbing soundbite.

## Requirements

- Windows
- **Anthropic API key** with prepaid credits (separate from your Claude Code subscription; ~$0.0002 per summary via Haiku 4.5)
- Internet access to `api.anthropic.com` and `tiktok-tts.weilnet.workers.dev`

## Install

```text
/plugin marketplace add <source>
/plugin install claude-tiktok@claude-tiktok-local
```

You'll be prompted for your Anthropic API key (stored in OS secure storage).

## License

MIT.
