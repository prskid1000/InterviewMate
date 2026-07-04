# CLAUDE.md — InterviewMate

Guidance for AI coding agents (and humans) working in this repo.

## What this is

A Windows real-time interview assistant. Captures the interviewer (WASAPI
system-audio loopback) and the user (mic) on two channels, transcribes both on
manual start/stop, and streams an AI-suggested answer into a frameless desktop
overlay. No system-tray icon; controlled by a HUD + global hotkeys + a native
settings window. Free-provider-first, but any OpenAI-/Anthropic-compatible
endpoint can be added.

## Run / dev

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
# put a key in .env (GEMINI_API_KEY=… ; free at aistudio.google.com/apikey)
.venv\Scripts\python -m copilot.main      # or run.bat
```

- Overlay mode is default. Set `overlay.enabled: false` in `config.yaml` to run
  headless with only the browser UI at `http://127.0.0.1:8765`.
- The FastAPI server runs in a background thread; **Qt owns the main thread**.

## Module map (`copilot/`)

| File | Responsibility |
|------|----------------|
| `main.py` | Entry point. Starts uvicorn in a thread, builds the Qt app, HUD, settings window, and the global-hotkey listener (`build_hotkeys()` re-registers on rebind). |
| `server.py` | `Copilot` core + FastAPI. Multiple **sessions** (tabs) — `transcript`/`exchanges` are properties proxying the active session; `new_/switch_/close_session` post `sessions`/`session_activated` events. Recording buffers, transcription, prompt assembly (profile + per-profile context block + persona + history), the answer stream, optional **auto-silence** VAD in `_on_chunk`, and the `/ws` websocket. `apply_settings()` hot-reloads config. |
| `overlay.py` | ONE merged, all-edge-resizable window: `Hud` paints the top BAR (status glyph + two vertical-bar equalizer meters) and hosts `SessionTabs` + `AnswerView` (the streamed answer) below it. Capture-hidden + opacity from `overlay.*`; geometry/visibility/collapse persisted. Subscribes to `Copilot.listeners` via a Qt `Signal`. |
| `settings_ui.py` | Native `SettingsWindow` (sidebar + pages), `ProviderEditor` (the reusable custom-provider list used by both Speech and AI Model), hotkey capture. |
| `llm.py` | `ProviderChain` — user-defined chat providers. Supports `openai` (OpenAI SDK, streaming) and `anthropic` (Anthropic SDK, `messages.stream`). No hardcoded providers. |
| `stt.py` | `SttChain` — user-defined STT providers: `openai` (`/audio/transcriptions`), `gemini` (`generateContent` inline audio). Also has legacy `local` (faster-whisper) code, not offered in UI. |
| `audio.py` | `AudioEngine` — PyAudioWPatch capture. `candidate` = default mic, `interviewer` = default WASAPI loopback. Resamples to 16 kHz mono float32. |
| `profiles.py` | CRUD for `profiles/*.md` (YAML frontmatter + body): `save_profile`/`delete_profile`. Each profile has its own **context files** in `context/<id>/` (`list_/set_context_files`), injected via `context_block()` and available as `{{title}}` template vars. Loose files at the top of `context/` remain shared vars. |
| `config.py` | `load_config()` / `save_config()` over `config.yaml`; `CFG` is the shared live dict. |
| `qt_theme_local.py` | Dark QSS for the settings window. |
| `uikit.py` | `ResizableMixin` (all-edge frameless resize) + `load_state`/`save_state` (`overlay_state.json`). |
| `vad.py` | Legacy energy VAD from the auto-segment era. **Unused** (recording is manual). Safe to delete. |

## Provider model (important)

Providers are **fully user-defined** — do not reintroduce hardcoded provider
names. Each entry:

```yaml
{ name, api_type, base_url, model, enabled,
  api_key: "inline"  # optional, OR
  api_key_env: "ENV_NAME" }   # resolved from .env
```

- LLM `api_type`: `openai` | `anthropic`. STT `api_type`: `openai` | `gemini`.
- Key resolution: `entry.api_key or os.getenv(entry.api_key_env)`.
- Chains try entries top-first; a provider that errors before emitting text falls
  through. Rate limits (429) put a provider on a 60 s cooldown.
- Gemini detection for thinking-off is by **base_url** (`generativelanguage`),
  not name — keep it that way so custom-named Gemini entries still work.
- To add a new api type: extend `ProviderChain._client`/`_stream_*` (LLM) or
  `SttChain._build` (STT), add it to the `api_types` list + `*_PRESETS` in
  `settings_ui.py`. Presets list **free tiers only**; paid via "Custom…".

## Prompt assembly (`server._answer`)

`system = render(active_profile, vars)` + persona (`config.persona`) appended as
a "RESPONSE STYLE" block. Then the last `history_exchanges` Q/A pairs, then the
current turn (question + the user's spoken attempt). History makes deep
follow-ups coherent; the persona controls tone/format/length/language on top of
whatever profile is active.

## Threading / events

- `Copilot` runs recording + STT + LLM on worker threads.
- It publishes events via `self.post(msg)` → asyncio queue (websocket) **and**
  `self.listeners` (the overlay's `event_sig.emit`, marshalled to the Qt thread).
- Hotkey callbacks fire on the pynput thread → they call `*_threadsafe()` which
  emits `ui_sig` so UI work happens on the Qt thread. Never touch widgets from a
  worker/pynput thread directly.

## Gemini specifics

- Free tier is small and **per-model** (~20 req/day for 2.5-flash). Defaults use
  `gemini-2.5-flash-lite` (separate quota). Groq is the durable free option.
- Thinking is disabled for latency: `reasoning_effort="none"` (LLM, OpenAI-compat)
  and `generationConfig.thinkingConfig.thinkingBudget=0` (STT `generateContent`).

## Testing conventions (no test suite yet)

- **Widgets**: render offline with `widget.grab().save(png)` under a throwaway
  `QApplication` + a stub `co` object (see prior scratchpad scripts) and inspect
  the PNG. Translucent always-on-top Tool windows do **not** show up in GDI
  desktop screenshots — grab the widget directly.
- **Server**: connect to `ws://127.0.0.1:8765/ws`, send `{"type":"ask","text":…}`,
  read `answer_delta`/`answer_done`.
- Always `python -m compileall -q copilot` after edits.

## Conventions

- Match existing style: concise, comments only for non-obvious constraints.
- Persist window geometry to `overlay_state.json` via `uikit.save_state`.
- Don't commit `.env` (gitignored) — it holds the API key.
- After changing `config.yaml` schema, update `save_config` callers in
  `settings_ui._save` and the defaults here + in README.
