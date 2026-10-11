# InterviewMate

A real-time interview assistant for Windows. It listens to the interviewer's
question (captured from your system audio) and to your own answer (from your
mic), transcribes both, and streams a suggested answer into a floating overlay
— so you can give a stronger, more consistent answer, including under deep
follow-up grilling.

It runs as a lightweight desktop **overlay with no system-tray icon**: a small
draggable HUD with live audio meters, a separate answer panel, and a settings
window. You drive it with buttons on the HUD; the only hotkey shows / hides
it. By default answers come from a local model via Telecode; free cloud
providers and paid ones can be added as custom endpoints.

> Use it to genuinely prepare — replay recordings, practice out loud, and learn
> the model answers. Feeding yourself answers during a live interview without the
> other party's knowledge is deceptive and can cost you the offer.

---

## Features

- **Dual-channel capture** — interviewer (WASAPI system-audio loopback) and you
  (microphone) on separate channels, so speakers are labelled perfectly without
  AI diarization. Works with Google Meet, Zoom, Teams, anything.
- **Manual, button-driven** — two icons on the HUD bar: **●** Listen / **■**
  Stop opens and closes both devices; **✦** Answer sends when *you* decide (it
  turns into **✕** to cancel while answering). Nothing is sent on its own.
- **Fast Answer** — while you listen, audio is transcribed in the background in
  ~20 s chunks, so pressing Answer only waits for the last few seconds.
- **Works on speakers** — an echo gate mutes your mic while the interviewer
  talks, and "you" lines that just repeat the interviewer are dropped.
- **Merged HUD** — one bar with the status indicator, **live signal meters**
  for INTERVIEWER and YOU and the Listen / Answer / settings icons, over the
  answer panel.
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
- **One hotkey** — show / hide the overlay (rebindable in Settings → Hotkeys).
- **Local-first** — the default provider is the local Telecode (llama.cpp)
  proxy; free cloud tiers (Gemini, Groq, OpenRouter) are one click away as presets.

---

## Requirements

- Windows 10/11
- Python 3.10+ (developed on 3.12)
- An answer provider: the local Telecode proxy on `127.0.0.1:1235` (default), or
  a free cloud key, e.g. Gemini (https://aistudio.google.com/apikey)

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
icon** — the controls are the icons at the right of the HUD bar (● Listen,
✦ Answer, ⚙ settings) and the right-click menu (also Quit).

---

## Using it

| Icon (HUD bar) | Action |
|----------------|--------|
| **●** Listen / **■** Stop | Open / close the mic and system-audio capture |
| **✦** Answer | Send everything said since the last answer (interviewer + you, in order) and stream what to say — if you'd started answering, it continues / improves your attempt |
| **✕** (while answering) | Cancel the answer |
| **⚙** | Settings |

The only hotkey, `Ctrl + Alt + B` (rebindable in **Settings → Hotkeys**), shows /
hides the overlay.

1. Press **●** before the interview starts (it also wakes a local model up,
   so the first answer isn't slow). Both meters move.
2. When you want help, press **✦**. Lines appear in the panel as they're
   transcribed; the answer streams in. Listening carries on. An amber dot on
   ✦ means the last few seconds are still being transcribed.
3. If you started answering yourself, ✦ continues / improves what you said.
4. Follow-ups: just press ✦ again — the AI keeps the whole session.

No audio device? Open the browser view at `http://127.0.0.1:8765` and type
questions manually.

---

## Settings (⚙ on the HUD)

- **Speech** — which engine transcribes. Both are offline, no key:
  - **Auto** (default) — use VoxType if its OpenAI HTTP server is up on
    `127.0.0.1:6600`, otherwise load our own model. VoxType
    already keeps Whisper resident on the GPU, so borrowing it means no second
    copy in VRAM and no model-load wait at startup. If VoxType disappears
    mid-session, transcription switches to the built-in model automatically.
  - **VoxType API only** — never load a local model; error if VoxType is down.
  - **Built-in model only** — always load our own Whisper. It's VoxType's
    transformers backend ported, so it shares VoxType's HF cache (no second
    download) and its knobs: model (HF repo id), device, precision, CPU
    threads, task, and under *Decoding* / *Performance* beams, temperature,
    repetition penalty, initial prompt, attention, warm-up, torch.compile.
    Default `openai/whisper-large-v3` on CUDA fp16 with automatic CPU fp32
    fallback; use `openai/whisper-small.en` on CPU-only machines. Both channels
    share one loaded copy.
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
- **Overlay & Audio** — opacity, hide-from-screen-share, the **echo gate**
  (keep on with speakers), the background chunk length, and the speech threshold.
- **Hotkeys** — rebind show / hide.

Presets in "Add provider" list **free-tier** options only (Gemini, Groq,
OpenRouter). Paid providers (OpenAI, Anthropic) are added via **Custom…** —
pick the api type and paste the base URL, model, and key. Each provider can
carry its own inline key, or reference an environment variable (`api_key_env`).

---

## Free-tier reality

- **Gemini** free tier is small and **per-model** (~20 requests/day for
  `gemini-2.5-flash`). The preset uses `gemini-2.5-flash-lite` (its own quota).
  Transcription is local, so the quota is spent on answers only.
- **Groq** free tier is far more generous (~14,400 chat req/day). For real
  practice, get a free key at https://console.groq.com/keys, then add the Groq
  preset under **AI Model** and paste it in.

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
