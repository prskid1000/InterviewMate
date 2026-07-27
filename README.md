# InterviewMate

A real-time interview assistant for Windows. It listens to the interviewer's
question (captured from your system audio) and to your own answer (from your
mic), transcribes both, and streams a suggested answer into a floating overlay
— so you can give a stronger, more consistent answer, including under deep
follow-up grilling.

It runs as a lightweight desktop **overlay with no system-tray icon**: a small
draggable HUD with live audio meters, a separate answer panel, and a settings
window summoned by a hotkey. Everything runs on **free AI providers** by
default (bring your own free API key); paid providers can be added as custom
endpoints.

> Use it to genuinely prepare — replay recordings, practice out loud, and learn
> the model answers. Feeding yourself answers during a live interview without the
> other party's knowledge is deceptive and can cost you the offer.

---

## Features

- **Dual-channel capture** — interviewer (WASAPI system-audio loopback) and you
  (microphone) on separate channels, so speakers are labelled perfectly without
  AI diarization. Works with Google Meet, Zoom, Teams, anything.
- **Manual recording** — click the HUD or press a hotkey to start/stop. No
  always-on listening.
- **Merged HUD** — one bar showing the status indicator plus **live signal
  meters** for both INTERVIEWER and YOU, so you can confirm both are heard.
- **Continuous session** — the answer panel accumulates the whole Q&A as one
  scrolling conversation; the AI keeps full history so deep follow-ups
  ("why?", "go deeper", "what's the trade-off?") stay consistent.
- **Improves your attempt** — if you started answering, it fixes/completes what
  you said instead of repeating it.
- **Fully custom providers** — add any number of AI models
  (OpenAI-compatible *or* Anthropic-compatible) and speech-to-text providers
  (OpenAI-compatible audio *or* Gemini). Tried top-first with automatic
  fallback on rate limits.
- **Persona** — one editable box controls tone, format, length, and language of
  every answer (ships tuned for concise, natural, speakable answers).
- **Profiles** — technical / behavioral / system-design prompt templates, or add
  your own; switchable mid-interview.
- **Configurable global hotkeys** — rebind any shortcut from the settings window.
- **Free-first** — default providers are Gemini and Groq free tiers.

---

## Requirements

- Windows 10/11
- Python 3.10+ (developed on 3.12)
- One free API key (Gemini recommended — no credit card):
  https://aistudio.google.com/apikey

## Setup

```powershell
cd interview-copilot
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env      # then paste your key(s)
```

`.env`:

```
GEMINI_API_KEY=your_key_here
GROQ_API_KEY=
OPENROUTER_API_KEY=
```

## Run

```powershell
run.bat
```

The overlay appears at the bottom-center of your screen. There is **no tray
icon** — the HUD's right-click menu and the global hotkeys are the controls.

---

## Using it

| Hotkey (default) | Action |
|------------------|--------|
| `Ctrl + Space`   | Start / stop recording |
| `Ctrl + Alt + C` | Open / close settings |
| `Ctrl + Alt + H` | Show / hide the answer panel |
| `Ctrl + Alt + B` | Show / hide the HUD |
| `Ctrl + Alt + Q` | Quit |

All hotkeys are rebindable in **Settings → Hotkeys** (click a shortcut, press
the new combo).

1. When the interviewer starts asking, press **Ctrl + Space** (or click the HUD
   status dot). Both channels start buffering; the meters move.
2. Answer if you want — your mic is captured separately.
3. Press **Ctrl + Space** again. Both channels are transcribed and the AI streams
   a suggested answer into the panel.
4. For a follow-up, just record again — the AI remembers the whole session.

No audio device? Open the browser view at `http://127.0.0.1:8765` and type
questions manually.

---

## Settings (Ctrl + Alt + C)

- **Speech** — which engine transcribes. Both are offline, no key:
  - **Auto** (default) — use VoxType if its OpenAI HTTP server is up on
    `127.0.0.1:6600`, otherwise load our own model. VoxType
    already keeps Whisper resident on the GPU, so borrowing it means no second
    copy in VRAM and no model-load wait at startup. If VoxType disappears
    mid-session, transcription switches to the built-in model automatically.
  - **VoxType API only** — never load a local model; error if VoxType is down.
  - **Built-in model only** — always load faster-whisper (`large-v3` on CUDA
    float16, automatic CPU int8 fallback; use `small.en` on CPU-only machines).
  Hit **Test** next to the URL to check VoxType before an interview.
- **AI Model** — add answer providers. Types: `openai` (any OpenAI-compatible
  chat endpoint) or `anthropic` (Claude `/messages`). Tick to enable, reorder
  with ↑/↓; a rate-limited provider falls through to the next.
  A **local** endpoint (`127.0.0.1`) — such as the **Telecode local (llama.cpp)**
  preset, telecode's Anthropic-compatible proxy on `:1235` — is a good top
  entry: no quota, no cost, and if telecode isn't running it's skipped in ~0.4 s
  and the next provider answers.
- **Interview** — active profile, role/subject, the **Persona** box (how it
  responds), and your resume + job description.
- **Hotkeys** — rebind any shortcut.

Presets in "Add provider" list **free-tier** options only (Gemini, Groq,
OpenRouter). Paid providers (OpenAI, Anthropic) are added via **Custom…** —
pick the api type and paste the base URL, model, and key. Each provider can
carry its own inline key, or reference an environment variable (`api_key_env`).

---

## Free-tier reality

- **Gemini** free tier is small and **per-model** (~20 requests/day for
  `gemini-2.5-flash`). The defaults use `gemini-2.5-flash-lite` (its own quota).
  Transcription is local, so the quota is spent on answers only.
- **Groq** free tier is far more generous (~14,400 chat req/day). For real
  practice, get a free key at https://console.groq.com/keys, paste it into the
  Groq row under **AI Model** and tick it.

---

## How it works

```
[System audio (loopback)] → "interviewer" ┐
                                           ├─ transcribe (VoxType API or local Whisper)
[Microphone]              → "you"          ┘        → profile + persona + session history
                                                    → AI chain (OpenAI/Anthropic, fallback)
                                                    → streamed answer in the overlay
```

Audio never leaves the machine — transcription is local either way (VoxType is
also on `127.0.0.1`). Only transcribed text and your resume/JD context are sent
to the configured AI provider.

## Configuration file

`config.yaml` holds providers, hotkeys, persona, profile, and vars. The settings
window writes to it (comments are not preserved on save). API keys live in
`.env` (referenced via `api_key_env`) or inline per provider.

## Project layout

See [CLAUDE.md](CLAUDE.md) for the module map and architecture.

## License

MIT (see LICENSE).
