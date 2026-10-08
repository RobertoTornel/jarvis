# Claire architecture — the hub

This is the entry point for anyone new to the codebase. It orients you in a few
minutes, shows how a spoken sentence travels from the microphone back out of the
speaker, and links to one focused page per subsystem.

If you read only one other thing, read the subsystem page for the part you are
about to change, and the `*.spec.md` file sitting next to that code. Both are
short by design.

---

## What Claire is

Claire is a standalone, always-on voice assistant for Linux, built for one
machine and one person. She keeps a microphone open, transcribes speech
continuously, decides whether the last thing said was addressed to her, answers
out loud in a cloned voice, remembers what happened, and can look at a screen, a
camera, a web page or a calendar. She exposes a local web panel and a phone
bridge, and she can call tools — built-in ones, and any MCP server configured
for her.

She is a personal fork of [`isair/jarvis`](https://github.com/isair/jarvis).
This fork deliberately allows cloud services where they give the best result,
with two load-bearing rules that shape the architecture:

- **Every LLM call goes through one local gateway.** Chat, tool routing,
  planning, digests, the intent judge and vision all dispatch through
  `src/claire/llm/`, which points at OmniRouter, an OpenAI-compatible endpoint
  at `http://127.0.0.1:20129/v1` by default. The key comes from the
  `OMNIROUTE_API_KEY` environment variable, never from a repository file.
- **Speech recognition, memory, the diary, the knowledge graph and tool
  execution stay on the machine.** Speech synthesis is the cloud exception:
  the default engine is Fish Audio streaming TTS with a cloned voice, and
  Piper and Chatterbox remain as offline fallbacks.

One process owns the assistant: `python -m claire` (or the `claire` console
script) starts the daemon in `src/claire/daemon.py`, which brings up the
database, TTS, the listener, the phone bridge and the background workers in that
order (`daemon.py:1563` onwards). The PyQt6 desktop app under `src/desktop_app/`
is a separate process that depends on `claire`, never the other way round, and
the web panel is its own loopback process too (`python -m claire.panel.server`,
default `127.0.0.1:8765`).

For day-to-day use there is one command: `./start.sh` at the repository root
builds the virtualenv if needed, starts the daemon and the panel, and can render
and install the systemd user unit (`./start.sh --install-service`). It is
documented in the [deployment spoke](subsystems/deployment.md).

## How a sentence travels end to end

This is the verified path. Every arrow names a real module or function.

```
microphone
  PortAudio callback            listening/listener.py:2382  _on_audio
  framing + WebRTC VAD          listening/listener.py:3227  _consume_audio_frames
                                listening/listener.py:1602  _is_speech_frame
  speech gates                  listening/listener.py:1654  _speech_gate_reason
  utterance finalised           listening/listener.py:3340  _finalize_utterance
  authoritative STT             listening/listener.py:2203  _run_transcription_worker
                                listening/listener.py:2057  _decode_utterance
                                listening/stt/parakeet.py    (Parakeet v2)
  turn assembled                listening/turn.py            TurnAssembler
                                listening/transcript.py      TurnTranscript (authoritative text)
  gates, in order               listening/listener.py:721   _process_transcript
                                  stop command               listening/wake_detection.py:201
                                  acoustic echo gate         listening/listener.py:898
                                  text echo + salvage        listening/echo_detection.py
                                  intent judge               listening/intent_judge.py:397
                                  wake word                  listening/wake_detection.py:140
                                listening/listener.py:1455  _dispatch_query
  reply loop                    reply/engine.py:780         run_reply_engine
                                  model dispatch             llm/
                                  tool routing               tools/selection.py:385
                                  planning                   reply/planner.py:424
                                  memory recall              reply/enrichment.py, memory/
                                  agentic turns              reply/engine.py:1945
                                  tool execution             tools/registry.py:315
                                  MCP tools                  tools/external/mcp_runtime.py
  speech                        output/fish_tts.py:785      FishTTS.speak
                                  cloud synth                output/fish_client.py:428
                                  streaming playback         output/fish_tts.py:1056
speaker
  hot window opens              listening/listener.py:1528  completion callback
```

In prose: capture is framed into 10/20/30 ms frames and classified by WebRTC
VAD; a pre-roll keeps the start of the utterance; silence for
`endpoint_silence_ms` ends it. Two gates then decide whether the buffer is worth
decoding at all. The authoritative recogniser (Parakeet by default) transcribes
the turn once, and the result is bound into a `TurnTranscript` whose guarded
accessors are the only way to reach the text the turn acts on — provisional
streaming text may wake her and label context, but it is never persisted and
never acted on (`turn.spec.md`). `_process_transcript` then applies the gates in
priority order and, if the utterance survives, dispatches it to
`run_reply_engine`, which routes tools, plans, recalls memory, runs an agentic
tool loop and returns reply text. The listener hands that text to the TTS engine,
which for Fish streams sentence by sentence into a local player, and the
completion callback opens the follow-up window.

## Component map

| Subsystem | Package | Owns | Spoke |
|---|---|---|---|
| Listening | `src/claire/listening/` | Device capture, VAD, utterance assembly, the two recognisers, wake word, echo detection, the intent judge | [Listening and speech capture](subsystems/listening.md) |
| Reply loop | `src/claire/reply/` | Redaction, context budget, tool routing, planning, prompts, the agentic loop, digests | [Intent and the reply loop](subsystems/intent-and-reply.md) |
| LLM gateway | `src/claire/llm/` | The single backend abstraction every LLM call dispatches through | [Intent and the reply loop](subsystems/intent-and-reply.md) |
| Tools | `src/claire/tools/` | Built-in tools, selection strategies, execution and retries, the MCP runtime | [MCP integration](subsystems/mcp.md) |
| Speech output | `src/claire/output/` | Fish streaming TTS, Piper and Chatterbox fallbacks, the thinking tune | [Speech output and TTS](subsystems/speech-output.md) |
| Memory | `src/claire/memory/` | SQLite store, dialogue memory, the diary, the markdown vault and notes, the knowledge graph, recall gating | [Long-term memory](subsystems/memory.md) |
| Scheduling | `src/claire/schedule/` | Calendar store, timers, event rules | — (see `schedule/schedule.spec.md`) |
| Senses | `src/claire/senses/` | Screen reading (`screen.py`), camera vision (`camera.py`), browser (`browser.py`) | — (see `senses/*.spec.md`) |
| Panel | `src/claire/panel/` | The local HTTP surface and UI | [The panel](subsystems/panel.md) |
| Phone bridge | `src/claire/phone/` | The phone's HTTP intake into the same listener | [The phone bridge](subsystems/phone-bridge.md) |
| Outreach | `src/claire/outreach/` | Urgency routing and the Signal and Discord transports | — (see `outreach/*.spec.md`) |
| Platform | `src/claire/platform/` | Key store, backups, the systemd service helper | [Deployment and startup](subsystems/deployment.md) |
| Desktop app | `src/desktop_app/` | The PyQt6 tray app, chat window, settings and memory viewer | - |

## Spoke index

| Spoke | Read it when |
|---|---|
| [Listening and speech capture](subsystems/listening.md) | You are changing capture, VAD, endpointing, the speech gates, wake matching or the intent judge. |
| [Intent and the reply loop](subsystems/intent-and-reply.md) | You are changing what the model is asked, which tools it sees, how many turns it gets, or how a malformed model reply is handled. |
| [Speech output and TTS](subsystems/speech-output.md) | You are changing how a reply becomes audio, adding a TTS engine, or debugging silence or barge-in. |
| [Long-term memory](subsystems/memory.md) | You are touching the SQLite store, the diary, the vault and notes, the knowledge graph, recall gating or conversation summarisation. |
| [The panel](subsystems/panel.md) | You are adding or changing an HTTP route or the panel UI. |
| [MCP integration](subsystems/mcp.md) | You are adding an MCP server, changing discovery or timeouts, or debugging a tool call. |
| [The phone bridge](subsystems/phone-bridge.md) | You are changing the phone transport, its port, its security, or how a phone utterance reaches the listener. |
| [Deployment and startup](subsystems/deployment.md) | You are changing the systemd unit, the startup script, entry points, or how the daemon boots. |
| [Configuration](subsystems/configuration.md) | You are adding a config key, changing a default, or regenerating `examples/config.json`. |

## Where the authoritative documentation lives

**Specs sit next to the code they describe and they win over prose.** Any
`*.spec.md` under `src/` is the contract for the module beside it; this hub and
the spokes are orientation, not authority. If a spoke and a spec disagree, the
spec and the code decide — and the disagreement is a documentation bug worth
fixing in the same change.

```
src/claire/listening/listening.spec.md      capture, gates, listening modes
src/claire/listening/turn.spec.md           provisional vs authoritative text
src/claire/listening/stt/stt.spec.md        the two recognisers
src/claire/reply/reply.spec.md              the reply loop
src/claire/reply/planner.spec.md            planning
src/claire/reply/prompts/prompts.spec.md    prompts
src/claire/tools/selection.spec.md          tool routing
src/claire/tools/external/mcp_runtime.spec.md   MCP runtime
src/claire/output/tts.spec.md               speech output
src/claire/memory/db.spec.md                SQLite store
src/claire/memory/router.spec.md            the vault router
src/claire/memory/recall_gate.spec.md       recall gating
src/claire/memory/graph.spec.md             the knowledge graph
src/claire/memory/summariser.spec.md        summarisation
src/claire/panel/panel.spec.md              HTTP routes
src/claire/phone/bridge.spec.md             the phone bridge
src/claire/llm/llm.spec.md                  the LLM gateway
src/claire/platform/keys.spec.md            the key store
src/claire/platform/service.spec.md         the service helper
src/claire/platform/backup.spec.md          backups
```

The list above is the spine. The complete rule is simpler than the list: every
`*.spec.md` under `src/` is the contract for the module next to it, including the
senses, the desktop app and the built-in tools.

Line anchors in the hub and the spokes name a symbol or a function alongside the
line, because the line is only a snapshot of the working tree: the symbol is the
stable part, the number is a pointer. Files under `src/` change, so treat any
`file:line` here as "start looking around there", and prefer the named symbol.

Three other documents are long-form guides rather than specs, and are worth
knowing about: `docs/CONFIGURATION.md` (every key, by hand), `docs/llm_contexts.md`
(every LLM call in the app — model, gating, inputs, outputs, limits and the flow
between them; any change that adds, removes or alters an LLM context must update
it in the same change) and `README.md` (install, run, diagnose). The project's
own working rules live in `AGENTS.md` at the repository root.

## Running the tests

`pytest.ini` sets `testpaths = tests` and `addopts = -m "not performance"`.

```bash
# unit and integration tests (performance is excluded by default)
pytest

# the fast unit tier only — this is the gate
.venv/bin/python -m pytest tests/ -m unit -q -p no:cacheprovider

# a single file
pytest tests/test_phone_bridge.py -v
```

The **unit gate** is the `-m unit` run above. It must pass with zero failures
before a change is considered done; the suite pins behaviour across the
listener, the reply engine, the planner, the phone bridge, every sense, the
calendar, the memory stores, the panel, and the daemon's TTS wiring. The
`integration` and `e2e` markers need more setup and are run by the hooks;
`performance` and `eval` are manual — `tests/performance/` is a timing harness
against a live model, and `evals/` holds live-model quality evaluations driven
by `scripts/run_evals.sh`.

Docs-only changes must still pass the unit gate, because some docs are pinned by
tests: `tests/test_readme_presentation.py` checks README images and relative
links, and `tests/test_examples_config_current.py` pins the generated
`examples/config.json` against the defaults.

## Reading order for a first change

1. This hub.
2. The spec beside the code you are changing.
3. The matching spoke above, for the flow around it.
4. `AGENTS.md` for the project rules (data privacy, secrets, specs, logging).
5. `pytest -m unit` to see the behaviour already pinned.
