# Muesli architecture decisions

Decisions made autonomously during the build, with the reasoning. Newest problems last.

## 1. pywebview + pure Python instead of Electron + Python sidecar

The spec suggested Electron with a Python sidecar. I swapped it for a single Python
process rendering the UI through pywebview (Edge WebView2, preinstalled on Win 11).
Reasons:

- Audio capture and Whisper already force Python into the stack. Electron would add a
  second runtime, an IPC bridge, npm packaging, and a class of "sidecar died silently"
  failure modes for zero functional gain here.
- pywebview gives the same HTML/CSS/JS surface for the Granola-style UI, with a direct
  js_api bridge (JS calls Python methods, gets Promises back).
- Tray comes from pystray, startup from an HKCU Run key. Nothing needed Node.
- Tradeoff accepted: pywebview is less battle-tested than Electron and the UI polls
  instead of receiving push events. Polling every 700 ms is fine at this scale.

## 2. Audio capture: PyAudioWPatch WASAPI loopback, plus a silence keeper

PyAudioWPatch exposes WASAPI loopback devices as PortAudio inputs, which testing
confirmed on this machine ("Speakers (Realtek(R) Audio) [Loopback]").

Two real problems surfaced in testing, both worth remembering:

- A loopback stream delivers **no data while nothing is playing**; blocking reads hang
  forever.
- Worse, with a blocking read pending while the render engine changed state, the
  process died with an access violation (0xC0000005) inside PortAudio. Reproduced
  reliably; mic-only capture was fine.

Fix: while recording, Muesli plays a continuous stream of zeros to the default output
device ("silence keeper" thread). The render device is then always active, loopback
always has data, reads never block indefinitely, and the crash is gone. Verified with a
tone test (loopback captured a 440 Hz tone played by the same process, RMS 0.20) and
the full TTS end-to-end test. Inaudible, negligible CPU.

Each source (mic, loopback) runs its own reader thread at the device's native rate and
is resampled to 16 kHz mono with linear interpolation (numpy only, no scipy). Mixing
clips at [-1, 1]. If either device is missing the session continues with the other and
surfaces a warning to the UI, so a meeting is never lost to a missing device.

## 3. "Me" vs "Them": RMS energy comparison, not diarization

Real diarization (pyannote et al.) is heavy, slow on CPU, and Granola itself only labels
desktop audio Me/Them. Muesli keeps a per-source RMS timeline in 0.25 s buckets; each
transcript segment is labeled by which source carried more energy over its time span
(1.5x dominance required, otherwise unlabeled). Cheap, works well for headset calls,
degrades gracefully for speakerphone setups.

## 4. STT: faster-whisper, chunked streaming with silence-based flushing

- Default model "base" (int8, CPU): ~75 MB download, fast enough for live use on modest
  hardware. User can switch to tiny/small/medium in Settings; models download to
  %APPDATA%\Muesli\models on first use.
- Streaming approach: buffer mixed audio; flush a chunk to Whisper when the trailing
  0.7 s is silent (natural pause) or at 12 s hard cap. Silence-first cutting avoids
  slicing words in half. VAD filtering inside faster-whisper drops non-speech. Chunks
  that are pure silence skip the model call entirely.
- Verified live: a five-sentence TTS script transcribed with all four target keywords
  while it was still playing.
- Cloud STT (OpenAI whisper-1, Groq whisper-large-v3-turbo) is an optional engine. Any
  cloud failure falls back to local Whisper mid-meeting rather than dropping audio.

## 5. Enhancement: provider router with a no-LLM floor

Order in "auto" mode: Anthropic > OpenAI > Groq > Ollama > built-in extractive. Cloud
keys outrank Ollama because the spec says a configured key should be preferred; each
level falls through on any error, and an empty LLM response is treated as an error (a
real case: qwen3 once returned only a think block, which stripped to empty and silently
downgraded the result).

- Ollama: /api/chat with think:false (stripped <think> blocks as a belt-and-braces),
  temperature 0.3, 16k ctx, 900 s timeout. qwen3:8b on this machine measured ~99 s for
  a small prompt and ~187 s for a full enhance, hence the generous timeout and the UI
  note that local enhancement takes minutes.
- Long meetings: instead of LLM map-reduce (which multiplies those minutes), transcripts
  over budget are shrunk deterministically: segments are scored by word frequency plus
  a boost for terms from the user's rough notes, and the top scorers are kept in
  chronological order. Budget is 11k chars for local models, 26k for cloud.
- Extractive fallback needs nothing installed: frequency-scored key points, regex cue
  detection for decisions ("we decided", "agreed", "signed off") and action items
  ("I'll send", "by Friday", "follow up"), plus a "Your notes, expanded" section that
  quotes the top transcript matches for every line the user wrote. Tested: produces a
  coherent structured summary from the test meeting.

## 6. Templates

Six builtins (Auto, 1:1, Standup, Weekly Sync, Sales/Discovery, Interview) modeled on
Granola's public template library; each is a list of {title, hint} sections that the
enhancement prompt must follow (H2 headings, omit-if-empty). Custom templates are the
same shape, stored in SQLite, editable in Settings. The LLM prompt and the extractive
fallback both consume the same section structure, so custom templates work without an
LLM too.

## 7. Chat: retrieval + router, honest degradation

Meeting-scoped chat stuffs that meeting's notes, summary and (relevance-shrunk)
transcript into the prompt. Global chat retrieves via SQLite FTS5 (falls back to LIKE
if FTS5 is unavailable) grouped by meeting, with recent-meeting summaries as backstop.
Same LLM router as enhancement. With no LLM anywhere, chat returns the matching
transcript excerpts and says exactly why it cannot do better, instead of pretending.

## 8. Storage

SQLite in %APPDATA%\Muesli (WAL mode, one connection per thread), FTS5 index on
segments maintained by triggers. Settings in a JSON file next to it: trivially
inspectable and editable, which suits a local-first personal tool. API keys live in
that JSON in plain text; documented in the README. DPAPI encryption would be a nice
later hardening, but any local process running as the user could read DPAPI-decrypted
data anyway, so the marginal gain is small for a personal machine.

## 9. Privacy defaults

Mirrors Granola: audio is transcribed and discarded (retention is an off-by-default
toggle), transcripts and notes are kept locally forever, nothing is transmitted unless
the user configures a provider. No telemetry of any kind. Consent helper is off by
default with a copyable snippet.

## 10. Things deliberately skipped, and why

- **Google Calendar sync**: requires an OAuth client registration and consent-screen
  process; the spec said skip it if it needs complex OAuth. Skipped, listed as future
  work. (An MCP/ICS-file route could do it keylessly later.)
- **Auto meeting detection**: heuristics (loopback energy without a session) are doable
  but false-positive prone (music!). Spec allows manual start as sufficient. Skipped.
- **People/Companies view**: nice-to-have per spec; search covers most of the need.
- **Installer packaging**: initially deferred, then built on request. PyInstaller 6
  (onedir, windowed) packages the whole stack into dist\Muesli\Muesli.exe (~250 MB).
  What it took: --add-data for ui/, --collect-data faster_whisper (silero VAD assets),
  hidden imports for pystray._win32 and the two pywebview Windows backends, a frozen
  branch in app.py (UI path from sys._MEIPASS), and the startup Run key pointing at
  sys.executable when frozen. A --selftest mode inside the exe verifies the risky
  native pieces (PortAudio devices, ctranslate2, onnxruntime, PyAV, WebView2 bridge)
  and passed on this machine: audio backend OK, 5 mics, 1 loopback. Onedir over
  onefile deliberately: onefile unpacks 250 MB to temp on every launch.
- **Live diarization, image paste, sharing, recipes**: out of scope for a personal
  local tool's v0.1.

## 11. Post-build adversarial review round

After the first fully passing end-to-end run, a five-dimension review (UI/backend
contract, threading, pure logic, Windows integration, frontend state machine) with
per-finding adversarial verification confirmed 43 issues. All were fixed except two
accepted with reasons:

- Fixed highlights: a stalled loopback source could gate the mixer and silently freeze
  transcription (now sources stop gating after 5 s without data, and the silence keeper
  restarts itself if the output device hiccups); a Whisper warmup failure left capture
  running with no consumer (unbounded memory, empty retained WAV; the worker now keeps
  draining audio even when STT is down); the startup Run key used `-m muesli`, which
  cannot resolve from the System32 working directory Run keys launch with (now a
  cwd-independent launch.py, verified by launching from System32); stop/start races
  (session stop is now idempotent and locked, meeting start is serialized); a batch of
  frontend stale-view bugs (enhance/chat/delete acting on whichever meeting happened to
  be open when an async job finished, transcript duplication after a UI reload, notes
  lost when switching meetings during the autosave debounce).
- Accepted, not fixed: the hide-to-tray close handler also runs during Windows
  shutdown, so Windows may show the app as blocking shutdown for a moment before
  force-killing it (pywebview does not expose WM_QUERYENDSESSION to tell the cases
  apart); and the stop path can wait up to 90 s for the final transcription drain,
  which is legitimate work on a slow CPU rather than a hang.

## 12. Visual design pass (Satoshi, quiet minimalism)

Reworked on request to look professional and minimal, closer to Granola's
content-over-chrome feel, and to remove the "AI product" styling tells:

- Typeface: Satoshi (Fontshare's ITF Free Font License, permitted for this use;
  license bundled at ui/fonts/FFL.txt). Served locally via @font-face so the app
  stays fully offline; variable weight plus static 400/500/700 fallbacks.
- Killed the AI-glow elements: the sparkle icon on Enhance, the lime-filled buttons,
  colored speaker bubbles, filled pill tabs, and the soft drop shadows on every card.
- New system: warm paper background (#F7F6F2), near-black ink (#21201C), one muted
  olive accent used only for tiny signals (the "Me" speaker label, status text).
  Hairline borders instead of shadows (the only shadows left are modal and toast).
  Tabs are text with a 2 px ink underline. Primary actions are solid ink buttons.
  Speaker labels are small uppercase text, not chips. Tabular numerals for timers.
- Verified with a static preview server: Satoshi loads, buttons/tabs/panes computed
  styles all match the new system at desktop width. The exe was rebuilt so the
  bundled UI matches the source tree.

## 13. Public release

Published to GitHub (MIT license) with these changes for a public audience:

- **Font licensing**: Fontshare's free-font EULA forbids redistributing Satoshi files
  ("uploading them in a public server" is explicitly disallowed), so the woff2 files
  were removed from the tree and from the packaged app. muesli/fonts.py now downloads
  Satoshi from Fontshare's own API on first run (a delivery mode the EULA supports),
  caches it in %APPDATA%\Muesli\fonts, copies it into ui/fonts when writable, and the
  frontend injects file:/// @font-face rules as a fallback. With no download the UI
  falls back to system fonts and nothing breaks.
- **Privacy sweep before publishing**: no personal paths, emails or keys in tracked
  files (the only grep hits were inside gitignored .venv/build/dist folders and the
  masked key placeholders in the settings UI). Settings, databases and models all live
  in %APPDATA%, never in the repo.
- **Discoverability**: README rewritten around what people actually search for (free
  Granola alternative for Windows, bot-free AI meeting notes, local Whisper
  transcription), with a comparison table, FAQ phrased as real questions, a screenshot,
  and repo topics set on GitHub. A prebuilt zip is attached to the release so
  non-developers can use it without Python.
- **Trademark care**: the README states Muesli is independent and not affiliated with
  Granola Inc., and no Granola branding or assets are used anywhere.

## 14. Test harness

tests\test_e2e.py generates a scripted meeting with Windows SAPI TTS rendered to WAV,
plays it through the real speakers with PyAudio (loopback captures it exactly like a
Zoom call), and asserts on keyword recall, extractive output and LLM output. This is
the same path a real meeting takes, minus a human. One environment quirk: TTS spoken
directly (Speak()) did not render audio in the automated shell session, so the test
synthesizes to WAV first and plays the file itself; both paths are equivalent for the
capture pipeline.
