<#
claude-tiktok hook script.

Stop mode:         reads last assistant message from transcript, summarizes with Haiku, speaks via TikTok TTS.
Notification mode: speaks the notification message directly (no summarization).

Config is supplied by Claude Code as env vars (injected from plugin userConfig):
  $env:CLAUDE_PLUGIN_OPTION_API_KEY     (required; Anthropic API key)
  $env:CLAUDE_PLUGIN_OPTION_VOICE       (default en_us_001)
  $env:CLAUDE_PLUGIN_OPTION_MAX_WORDS   (default 12)

On any failure, plays a short console beep as a minimal fallback so the user isn't left in silence.
Debug log: %TEMP%\claude-tiktok.log
#>
param(
    [Parameter(Mandatory = $true)]
    [ValidateSet("Stop")]
    [string]$Mode
)

$ErrorActionPreference = "Stop"

$ApiKey       = $env:CLAUDE_PLUGIN_OPTION_API_KEY
$MaxWords     = if ($env:CLAUDE_PLUGIN_OPTION_MAX_WORDS) { [int]$env:CLAUDE_PLUGIN_OPTION_MAX_WORDS } else { 9 }
$Voice        = "en_us_001"
$SpeedPercent = 120

function Write-DebugLog {
    param([string]$Msg)
    try {
        $logPath = Join-Path $env:TEMP "claude-tiktok.log"
        "$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') [$Mode] [pid $PID] $Msg" | Out-File -FilePath $logPath -Append -Encoding utf8
    } catch {}
}

Write-DebugLog "hook fired; cwd=$($PWD.Path); keyLen=$($ApiKey.Length); voice=$Voice; maxWords=$MaxWords"

function Play-Ping {
    try {
        $pingPath = Join-Path $PSScriptRoot "microwave-ping.wav"
        if (Test-Path $pingPath) {
            (New-Object Media.SoundPlayer $pingPath).PlaySync()
            return
        }
    } catch {}
    try { [console]::beep(800, 200) } catch {}
}

function Play-Fallback { Play-Ping }

function Play-Mp3Sync {
    param([string]$Path)
    # Attention lead-in: microwave ping + short pause primes the listener so
    # the first spoken syllable isn't wasted on a distracted user.
    Play-Ping
    Start-Sleep -Milliseconds 200

    # Preferred path: mpg123 decodes MP3 to WAV, SoX applies pitch-preserving
    # tempo, SoundPlayer plays the WAV synchronously. All three binaries are
    # bundled in ../bin/ and work without PATH dependencies.
    $binDir = Join-Path (Split-Path -Parent $PSScriptRoot) "bin"
    $mpg = Join-Path $binDir "mpg123.exe"
    $sox = Join-Path $binDir "sox.exe"

    if ($SpeedPercent -ne 100 -and (Test-Path $mpg) -and (Test-Path $sox)) {
        try {
            $tempo = [double]$SpeedPercent / 100.0
            $wav     = Join-Path $env:TEMP "claude-tiktok-decoded.wav"
            $spedWav = Join-Path $env:TEMP "claude-tiktok-sped.wav"
            & $mpg -q -w $wav $Path 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "mpg123 failed (exit $LASTEXITCODE)" }
            & $sox -q $wav $spedWav tempo $tempo 2>&1 | Out-Null
            if ($LASTEXITCODE -ne 0) { throw "sox failed (exit $LASTEXITCODE)" }
            (New-Object Media.SoundPlayer $spedWav).PlaySync()
            Write-DebugLog "sox tempo=$tempo played"
            return
        } catch {
            Write-DebugLog "sox pipeline failed: $($_.Exception.Message)"
        }
    }

    # MCI fallback at normal speed (no speed control, but reliably plays MP3).
    if (-not ("Win32.WinMM" -as [type])) {
        Add-Type -Name WinMM -Namespace Win32 -MemberDefinition @"
[System.Runtime.InteropServices.DllImport("winmm.dll", CharSet=System.Runtime.InteropServices.CharSet.Auto)]
public static extern int mciSendString(string lpszCommand, System.Text.StringBuilder lpszReturnString, int cchReturn, System.IntPtr hwndCallback);
"@
    }
    $sb = New-Object System.Text.StringBuilder 256
    $alias = "claudetiktok_$PID"
    [Win32.WinMM]::mciSendString("close $alias", $sb, 256, [System.IntPtr]::Zero) | Out-Null
    $openRc = [Win32.WinMM]::mciSendString("open `"$Path`" type mpegvideo alias $alias", $sb, 256, [System.IntPtr]::Zero)
    Write-DebugLog "MCI open rc=$openRc"
    if ($openRc -ne 0) { return }
    $playRc = [Win32.WinMM]::mciSendString("play $alias wait", $sb, 256, [System.IntPtr]::Zero)
    Write-DebugLog "MCI play rc=$playRc"
    [Win32.WinMM]::mciSendString("close $alias", $sb, 256, [System.IntPtr]::Zero) | Out-Null
}

function Invoke-TikTokTts {
    param([string]$Text)
    $body = @{ text = $Text; voice = $Voice } | ConvertTo-Json -Compress
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    $resp = Invoke-RestMethod -Uri "https://tiktok-tts.weilnet.workers.dev/api/generation" `
        -Method Post -ContentType "application/json; charset=utf-8" -Body $bodyBytes -TimeoutSec 10
    if (-not $resp.data) { throw "TTS returned no audio data" }
    $mp3 = Join-Path $env:TEMP "claude-tiktok.mp3"
    [IO.File]::WriteAllBytes($mp3, [Convert]::FromBase64String($resp.data))
    return $mp3
}

function Invoke-Haiku {
    param([string]$Text)
    if (-not $ApiKey) { throw "CLAUDE_PLUGIN_OPTION_API_KEY not set" }
    $prompt = "Summarize the message below in one short sentence (max $MaxWords words) to be spoken aloud. Lead with what happened and end with what's needed from the user; adapt if nothing is asked. Output ONLY the sentence, no quotes, no preamble.`n`nMessage:`n$Text"
    $body = @{
        model      = "claude-haiku-4-5-20251001"
        max_tokens = 80
        messages   = @(@{ role = "user"; content = $prompt })
    } | ConvertTo-Json -Depth 5 -Compress
    $bodyBytes = [System.Text.Encoding]::UTF8.GetBytes($body)
    try {
        $resp = Invoke-RestMethod -Uri "https://api.anthropic.com/v1/messages" -Method Post -Headers @{
            "x-api-key"         = $ApiKey
            "anthropic-version" = "2023-06-01"
        } -ContentType "application/json; charset=utf-8" -Body $bodyBytes -TimeoutSec 15
        return $resp.content[0].text.Trim()
    } catch [System.Net.WebException] {
        $errBody = ""
        if ($_.Exception.Response) {
            try {
                $reader = New-Object System.IO.StreamReader($_.Exception.Response.GetResponseStream())
                $errBody = $reader.ReadToEnd()
            } catch {}
        }
        Write-DebugLog "HAIKU ERROR body: $errBody"
        throw
    }
}

function Get-LastAssistantText {
    param([string]$TranscriptPath)
    if (-not (Test-Path $TranscriptPath)) { return $null }
    $lines = Get-Content $TranscriptPath
    for ($i = $lines.Length - 1; $i -ge 0; $i--) {
        try {
            $entry = $lines[$i] | ConvertFrom-Json
            if ($entry.type -eq "assistant" -and $entry.message.content) {
                $texts = @()
                foreach ($block in $entry.message.content) {
                    if ($block.type -eq "text") { $texts += $block.text }
                }
                if ($texts.Count -gt 0) {
                    return ($texts -join "`n")
                }
            }
        } catch { continue }
    }
    return $null
}

try {
    $stdinJson = [Console]::In.ReadToEnd()
    Write-DebugLog "stdin bytes=$($stdinJson.Length)"
    $hookInput = if ($stdinJson) { $stdinJson | ConvertFrom-Json } else { $null }

    if (-not $hookInput -or -not $hookInput.transcript_path) {
        Write-DebugLog "no transcript_path in payload -> fallback"
        Play-Fallback; exit 0
    }
    $text = Get-LastAssistantText -TranscriptPath $hookInput.transcript_path
    if (-not $text) {
        Write-DebugLog "no assistant text found -> fallback"
        Play-Fallback; exit 0
    }
    Write-DebugLog "got text len=$($text.Length)"
    if ($text.Length -gt 4000) { $text = $text.Substring(0, 4000) }
    $summary = Invoke-Haiku -Text $text
    if (-not $summary) {
        Write-DebugLog "haiku returned empty -> fallback"
        Play-Fallback; exit 0
    }
    Write-DebugLog "summary: $summary"
    $mp3 = Invoke-TikTokTts -Text $summary
    Write-DebugLog "mp3 written: $mp3 ($((Get-Item $mp3).Length) bytes)"
    Play-Mp3Sync -Path $mp3
    Write-DebugLog "playback done"
} catch {
    Write-DebugLog "EXCEPTION: $($_.Exception.Message)`n$($_.ScriptStackTrace)"
    Play-Fallback
}
