# mixxx-mcp

MCP (Model Context Protocol) server for [Mixxx](https://mixxx.org) DJ Software.
Lets AI agents (Claude, Claude Code, etc.) control Mixxx in real time — transport, mixing, EQ, loops, hotcues, effects.

---

## Architecture

Both directions share one virtual MIDI port named `mixxx-mcp`.

**Write path:** `mcp.tool → MidiBridge → rtmidi CC (ch 1) → Mixxx JS → engine.setValue()`

**Read path:** `Mixxx JS engine.makeConnection() → MIDI SysEx → MidiBridge listener → StateStore → mcp.tool`

Mixxx's controller script engine has **no network access** — no `XMLHttpRequest`,
no `fetch`, no OSC output — so state comes back as SysEx (`F0 7D <ascii json> F7`).
SysEx is used rather than CCs because a 7-bit CC cannot carry a BPM or a
position. The shared port does not feed back: Mixxx ignores the SysEx it emits
(it matches no CC mapping), and the server ignores the CCs it emits.

Mixxx normally starts before the MCP server, so its boot snapshot lands with
nothing listening. The server therefore sends a resync request (CC 119) on
connect, and `resync_state()` can request one at any time.

### Precision and limits

- Control values cross as 7-bit MIDI, so reads come back quantised
  (`set_volume(0.9)` reads back as ~0.898). Bipolar controls are centred on 64
  so `0.0` is exact.
- Hotcues are mapped for **decks 1–2 only** — 8 slots x 3 actions x 4 decks does
  not fit in 128 CCs. Hotcue calls for decks 3–4 fail with a clear error.
- **Track artist/title are unavailable.** Mixxx does not expose track metadata to
  controller scripts as ControlObjects. Use `track_loaded` / `duration`.
- **Tracks cannot be loaded by path.** See below.
- **Beatgrids cannot be edited.** Mixxx exposes no beatgrid-editing control to
  controller scripts. `set_bpm()` reaches a tempo with the rate slider, so it
  cannot rescue a track the analyser read at half or two-thirds time — that
  needs the library UI. Such a track also cannot be synced: the rate required
  is outside the slider range, so it clamps and plays at the wrong tempo with
  no error raised anywhere.

### Loading tracks

Mixxx has no control that takes a file path or a track id — `LoadSelectedTrack`
acts on whatever the library view currently highlights, and every navigation
control (`MoveVertical`, `SelectTrackKnob`, …) is a *relative* move. Nothing
reports the current selection back, so the server cannot know where the
highlight is, only move it and look at the result.

That makes loading a two-step, best-effort operation:

```python
library_move(-64)           # clamps at the top of the list = known origin
library_move(7)             # down to the row you want
load_selected_track(1)      # -> {"track_loaded": true, "duration": 338.9}
```

`library_move()` uses `[Playlist],SelectTrackKnob`, so it works while Mixxx is
in the background. `library_focus()` and `library_go_to_item()` use `[Library]`
controls that emulate keyboard actions and require a Mixxx window to have
keyboard focus.

`load_selected_track()` returns the deck's `duration` after loading rather than
just `ok: true`, because that is the only way to tell *which* track arrived.
Compare it against the duration you expected.

This is reliable when the view is small and its order is known — a playlist
sorted by position, or a search narrowed to one hit. It is **not** reliable
against a large sorted library: row N depends on Mixxx's locale-aware sort
collation, which the server cannot reproduce.

If you need a specific file on a specific deck with no ambiguity, pass it on the
command line at startup instead — `mixxx "a.mp3" "b.mp3"` loads each file into
the next virtual deck. There is no equivalent for an already-running Mixxx.

---

## Setup

### 1. Install Python server

```bash
pip install -e ".[dev]"
# or without dev deps:
pip install mcp python-rtmidi
```

### 2. Install Mixxx controller script

Copy both files to your Mixxx controllers directory:

```bash
# macOS
cp mixxx-mcp.js mixxx-mcp.midi.xml \
  ~/Library/Containers/org.mixxx.mixxx/Data/Library/Application\ Support/Mixxx/controllers/

# Linux
cp mixxx-mcp.js mixxx-mcp.midi.xml ~/.mixxx/controllers/

# Windows
cp mixxx-mcp.js mixxx-mcp.midi.xml %LOCALAPPDATA%\Mixxx\controllers\
```

### 3. Create the virtual MIDI port

The port must exist **before** Mixxx starts, or Mixxx will not list the device.

- **Windows:** install [loopMIDI](https://www.tobias-erichsen.de/software/loopmidi.html)
  (`winget install TobiasErichsen.loopMIDI`), then add a port named exactly
  `mixxx-mcp`. loopMIDI must be running whenever you use this.
- **macOS:** enable an IAC bus named `mixxx-mcp` in Audio MIDI Setup.
- **Linux:** the server creates the virtual port itself via ALSA.

### 4. Enable in Mixxx

1. Open **Mixxx → Preferences → Controllers**
2. Select **mixxx-mcp** from the device list
3. Set **Load Mapping** to `mixxx-mcp`
4. Check **Enabled**, then click **OK**

Verify: Mixxx's log should show `[mixxx-mcp] ready — N connections active`.
If a control does nothing, start Mixxx with `--controller-debug --log-level debug
--log-flush-level debug` — incoming MIDI and every `SET` are logged. (Without
`--log-flush-level debug`, debug lines are buffered and the log will look stale.)

### 5. Run the MCP server

```bash
# stdio mode (Claude Desktop / claude-code)
python main.py

# HTTP mode (remote / multi-client)
python main.py --http --port 8080
```

### 6. Configure Claude Desktop

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "mixxx": {
      "command": "python",
      "args": ["/path/to/mixxx-mcp/main.py"],
      "env": {}
    }
  }
}
```

---

## Available Tools

| Tool | Description |
|------|-------------|
| `play(deck)` | Start playback |
| `stop(deck)` | Stop playback |
| `cue(deck)` | Trigger CUE |
| `sync(deck)` | Toggle BPM sync (tempo only) |
| `beatsync_phase(deck)` | Align beats to the sync leader without changing tempo |
| `set_bpm(deck, bpm)` | Playback tempo in BPM, via the rate slider |
| `set_volume(deck, value)` | Channel fader 0.0–1.0 |
| `set_crossfader(value)` | Crossfader -1.0–1.0 |
| `set_eq(deck, low, mid, high)` | EQ bands 0.0–4.0 |
| `set_pregain(deck, value)` | Trim 0.0–4.0 |
| `set_rate(deck, value)` | Tempo pitch -1.0–1.0 |
| `nudge_tempo(deck, direction, size)` | Tempo nudge up/down |
| `set_loop(deck, beats)` | Activate beat loop |
| `exit_loop(deck)` | Deactivate loop |
| `halve_loop(deck)` / `double_loop(deck)` | Resize loop |
| `set_hotcue(deck, slot)` | Set hotcue 1–8 (decks 1–2) |
| `goto_hotcue(deck, slot)` | Jump to hotcue (decks 1–2) |
| `clear_hotcue(deck, slot)` | Delete hotcue (decks 1–2) |
| `beatjump(deck, beats)` | Jump ±N beats |
| `load_selected_track(deck)` | Load the highlighted library track; returns the resulting `duration` |
| `library_move(rows)` | Move track-table selection ±N rows (-64–63), independent of keyboard focus |
| `library_focus(widget)` | Focus `none`/`searchbar`/`sidebar`/`tracks` (requires Mixxx keyboard focus) |
| `library_go_to_item()` | Activate the highlighted item (requires Mixxx keyboard focus) |
| `library_clear_search()` | Clear the search box |
| `get_deck_state(deck)` | Read live deck state |
| `get_mixer_state()` | Read master mixer state |
| `get_all_state()` | Full state dump |
| `resync_state()` | Ask Mixxx to re-push all state |
| `send_control(group, key, value)` | Raw control escape hatch |
| `toggle_effect(unit, effect, enabled)` | Effect on/off |
| `set_effect_mix(unit, value)` | Effect wet/dry |

Write tools raise on failure rather than reporting success — an `ok: true` means
the CC was really delivered to the MIDI port.

---

## Development

```bash
pip install -e ".[dev]"
pytest
```

`src/controls.py` (`MIDI_CC_MAP`) and `mixxx-mcp.js` (`CC_ROUTE`) describe the
same CC allocation and **must stay in sync**; both are generated from the same
block layout, and `controls.py` asserts on import that no two controls share a
CC. Mixxx dispatches an unmatched CC to nothing at all — silently — so a
collision here presents as "the control just doesn't work", with no error.

---

## Mixxx Controls Reference

Full control list: https://manual.mixxx.org/latest/en/chapters/appendix/mixxx_controls.html

Key groups:
- `[Channel1]` – `[Channel4]` — Decks
- `[Master]` — Mixer master
- `[Sampler1]` – `[Sampler64]` — Samplers
- `[EffectRack1_EffectUnit1]` – `[_EffectUnit4]` — Effect units
- `[EffectRack1_EffectUnit1_Effect1]` – `[_Effect3]` — Individual effects
- `[Playlist]` — Library navigation

---

## License

MIT
