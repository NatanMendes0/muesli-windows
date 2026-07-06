# Muesli

**A free, open-source alternative to Granola for Windows.** Muesli is an AI meeting
notepad that takes notes without a bot joining your call. It captures your microphone
and your system audio directly, transcribes the meeting locally with Whisper, and turns
your rough notes into a clean, structured summary. No account, no cloud, no subscription,
no telemetry. Your meetings never leave your computer.

![MIT license](https://img.shields.io/badge/license-MIT-green)
![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11-blue)
![Python](https://img.shields.io/badge/python-3.10--3.12-blue)
![No cloud](https://img.shields.io/badge/cloud-none-lightgrey)

![Muesli screenshot](docs/screenshot.png)

Works with **Zoom, Google Meet, Microsoft Teams, Webex, Slack huddles, phone calls on
speaker, and in-person meetings**, because it listens to your audio devices instead of
plugging into any one meeting platform.

## Why Muesli

Granola is a great product, but it is a paid subscription and your meeting audio and
transcripts go through the cloud. Muesli is the same core idea, rebuilt to run entirely
on your own machine, for free:

| | Muesli | Granola |
|---|---|---|
| Price | Free, MIT-licensed | Subscription after trial |
| Bot joins your meeting | No | No |
| Where audio is processed | Your computer (local Whisper) | Cloud transcription |
| Where notes live | Local SQLite in %APPDATA% | Their cloud |
| Account required | None | Yes |
| AI enhancement | Local Ollama, your own API key, or a built-in offline summarizer | Cloud models |
| Windows support | Yes, Windows first | Yes |
| Works offline | Yes (after first-run model download) | No |

## Features

- **Bot-free capture**: records your mic and your speakers (WASAPI loopback) so it hears
  every participant on any platform, tagged "Me" and "Them".
- **Live local transcription**: streaming speech-to-text with faster-whisper on your CPU.
  Nothing is uploaded.
- **Rough notes in, clean notes out**: jot bullets during the call, hit Enhance after,
  and your notes get merged with the transcript into a structured summary. Your notes act
  as anchors: write "pricing concerns" and every pricing moment gets pulled in.
- **Templates**: Auto, 1:1, Standup, Weekly Sync, Sales/Discovery, Interview, plus your
  own custom templates.
- **Chat with your meetings**: ask "what did we decide?" or "draft a follow-up email",
  scoped to one meeting or across all of them, grounded in your transcripts.
- **Library and search**: full-text search over every transcript, note and summary.
- **Export**: Markdown export and copy-to-clipboard.
- **Free by default, better with more**: with nothing installed you still get a solid
  extractive summary. Install [Ollama](https://ollama.com) for free local AI, or add an
  OpenAI / Anthropic / Groq key for faster, sharper results. Keys stay in a local file.
- **Privacy first**: audio is transcribed in memory and discarded (retention is an
  opt-in setting), notes stay in local SQLite, and there is no telemetry of any kind.

## Install

### Option 1: download the app (no Python needed)

1. Grab `Muesli-windows-x64.zip` from the [latest release](../../releases/latest).
2. Unzip anywhere (for example `C:\Apps\Muesli`).
3. Run `Muesli.exe`. Windows SmartScreen may warn because the build is unsigned:
   click "More info" then "Run anyway", or build from source instead.

### Option 2: run from source

```
git clone https://github.com/shivankoctupie/muesli
cd muesli
run.bat
```

`run.bat` creates a Python virtual environment, installs dependencies and starts the
app. Needs Windows 10/11 and Python 3.10 to 3.12 on PATH. After the first run it starts
in seconds. `run_debug.bat` starts it with a console and devtools.

Either way, the first recording downloads the Whisper "base" model (about 75 MB) and
the Satoshi UI font, so keep the internet on once. After that Muesli works fully
offline.

## Using it

1. Hit **New note** when your meeting starts. Live transcription begins immediately.
2. Type rough notes in the left pane while the transcript builds on the right.
3. Hit **Stop**, pick a template, hit **Enhance notes**.
4. Ask questions in the chat bar, or use the one-click "Action items" and
   "Follow-up email" actions.
5. Find and search everything later in the sidebar library.

Muesli lives in the system tray: closing the window hides it, right-click the tray icon
to quit. Launch-at-startup is in Settings.

## Microphone and system audio on Windows

- **Microphone permission**: Settings > Privacy & security > Microphone. Enable
  "Microphone access" and "Let desktop apps access your microphone".
- **System audio** needs no permission; Muesli captures it through WASAPI loopback on
  your default output device. Pick a specific device in Settings > Audio.
- While recording, Muesli plays silence to the output device. This is inaudible and
  intentional: Windows only feeds loopback data while the device is active, and this
  keeps capture stable through quiet stretches.
- No loopback device listed? Update your audio driver, make sure a default playback
  device is set, and hit "Refresh devices" in Settings > Audio. Muesli falls back to
  mic-only recording with a visible warning, so a meeting is never lost outright.
- If you switch output devices mid-meeting (plugging in headphones), stop and start the
  note again; capture stays bound to the device it started on.

## Optional: smarter AI

- **Ollama (free, local)**: install from [ollama.com](https://ollama.com) and pull a
  model (`ollama pull llama3.2:3b` is fast on CPU; `qwen3:8b` is smarter but slower).
  Muesli detects it automatically.
- **API keys (paid, fastest)**: add an OpenAI, Anthropic or Groq key in Settings > AI
  models. A configured key is preferred automatically; remove it and Muesli falls back
  to Ollama, then to the built-in summarizer. Keys are stored only in
  `%APPDATA%\Muesli\settings.json` and sent only to the provider you configured.
- **Cloud transcription** (optional): switch Settings > Transcription to OpenAI or Groq
  Whisper APIs. If a cloud call fails mid-meeting, Muesli falls back to local Whisper so
  the transcript keeps flowing.

## FAQ

**Is Muesli really free?**
Yes. MIT-licensed, no trial, no locked features, no account. Local Whisper and the
built-in summarizer cost nothing. Ollama is also free. API keys are optional and billed
by their providers, not by Muesli.

**Does it work with Zoom / Google Meet / Teams?**
Yes, and with anything else that makes sound on your PC. Muesli captures device audio,
not a specific app, so there is no integration to break and no bot participant that
other people see in the call.

**Is my audio uploaded anywhere?**
No. Audio is captured, transcribed on your CPU, and discarded (unless you enable
retention in Settings). Transcripts and notes live in a local SQLite database. The only
network traffic is the one-time model and font download, plus any AI provider you
explicitly configure.

**Do I need a GPU?**
No. The default Whisper "base" model transcribes live on a normal laptop CPU. Bigger
models (small/medium) are available in Settings if your machine can take them.

**How is this different from other open-source note takers?**
Most alternatives either join calls as a bot, only record the microphone, or need a
paid transcription API. Muesli does bot-free two-channel capture (you + everyone else),
local STT, note enhancement anchored to your own rough notes, templates, and
chat-with-your-meetings, out of the box.

**Should I tell people I'm recording?**
Yes. Recording laws vary by region and consent is basic courtesy. Settings > General
has an optional reminder with a one-line disclosure snippet you can read out or paste
in the meeting chat.

## Verified end-to-end test

The repo ships a self test (`tests\test_e2e.py`) that speaks a scripted meeting through
the speakers and runs the full real pipeline. Result on the development machine
(Windows 11, Realtek audio, CPU only, Ollama qwen3:8b), 2026-07-07:

- Mic + WASAPI loopback capture: PASS
- Live streaming transcription: PASS, 4/4 test keywords, speakers tagged correctly
- Extractive enhancement (no LLM installed): PASS
- LLM enhancement via local Ollama: PASS, correct decisions and action items
- Chat grounding ("Who is sending the pricing proposal and when?"): PASS
- Packaged exe self test (`Muesli.exe --selftest`): PASS

## Architecture, decisions and limitations

Single Python process: pywebview (WebView2) UI, PyAudioWPatch for WASAPI capture,
faster-whisper for STT, SQLite + FTS5 for storage, a provider router (Anthropic /
OpenAI / Groq / Ollama / built-in extractive) for enhancement and chat. See
[DECISIONS.md](DECISIONS.md) for every design decision and known limitation, including
the WASAPI loopback silence-keeper trick that makes bot-free capture reliable.

Known limits: speaker tagging is an energy heuristic (not diarization), one recording
per note, no calendar sync yet, unsigned exe. Contributions welcome on all of these.

## Contributing

Issues and PRs are welcome. Good first areas: real diarization, calendar prefill,
auto-detecting meeting start, packaging/signing, macOS/Linux ports (the audio layer is
the only Windows-specific piece). Run the test suite with
`.venv\Scripts\python.exe tests\test_e2e.py` (it plays audio out loud).

## License

[MIT](LICENSE). The Satoshi font is not distributed with this repo; each install
downloads it from Fontshare under their free-font EULA (see `ui/fonts/README.md`).
Muesli is an independent project and is not affiliated with or endorsed by Granola Inc.
