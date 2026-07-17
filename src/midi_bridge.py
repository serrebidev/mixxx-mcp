"""
midi_bridge.py — Virtual MIDI ↔ Mixxx Control Object bridge

Both directions share one virtual MIDI port:

  WRITE  MidiBridge.send_control(group, key, value)
           → normalizes value to 0–127
           → sends an rtmidi CC on the virtual port
           → mixxx-mcp.js receives the CC → engine.setValue(group, key, value)

  READ   mixxx-mcp.js engine.makeConnection(...)
           → midi.sendSysexMsg(F0 7D <ascii json> F7)
           → MidiBridge._on_midi() parses it → StateStore

Mixxx's script engine has no network access (no XMLHttpRequest, no fetch), so
SysEx is the only way to get arbitrary state — including strings like track
titles — back out of it. The shared port does not feed back: Mixxx ignores the
SysEx it emits (it matches no CC mapping) and we ignore the CCs we emit.
"""

import json
import logging
import sys
from typing import Optional

# SysEx framing shared with mixxx-mcp.js: F0 <MCP_SYSEX_ID> <ascii json> F7.
SYSEX_START = 0xF0
SYSEX_END = 0xF7
MCP_SYSEX_ID = 0x7D

log = logging.getLogger("mixxx-mcp.midi")


class MidiSendError(RuntimeError):
    """Raised when a control could not actually be delivered to Mixxx."""

# Lazy import — rtmidi optional if using HTTP/OSC-only mode
try:
    import rtmidi
    HAS_RTMIDI = True
except ImportError:
    HAS_RTMIDI = False
    log.warning("python-rtmidi not installed — MIDI bridge disabled. Install: pip install python-rtmidi")


class MidiBridge:
    """
    Manages a virtual MIDI output port.
    Translates (group, key, value) → MIDI CC messages.
    
    The companion Mixxx JS script (mixxx-mcp.js) must be loaded in Mixxx
    → Preferences → Controllers to receive and route these messages.
    """

    PORT_NAME = "mixxx-mcp"
    CHANNEL = 0  # MIDI channel 1 (0-indexed)

    def __init__(self):
        self._out: Optional[object] = None
        self._in: Optional[object] = None
        self.port_name = self.PORT_NAME
        self._connected = False
        self._listening = False

    def connect(self):
        if not HAS_RTMIDI:
            log.warning("MIDI bridge unavailable — running in state-read-only mode")
            return
        try:
            self._out = rtmidi.MidiOut()
            # Check if our virtual port already exists
            ports = self._out.get_ports()
            for i, name in enumerate(ports):
                if self.PORT_NAME in name:
                    self._out.open_port(i)
                    self._connected = True
                    self.port_name = name
                    log.info("MIDI: opened existing port '%s'", name)
                    return
            # Create virtual port (macOS/Linux only; Windows needs loopMIDI)
            self._out.open_virtual_port(self.PORT_NAME)
            self._connected = True
            log.info("MIDI: created virtual port '%s'", self.PORT_NAME)
        except Exception as e:
            log.error("MIDI connect failed: %s", e)

    def send_control(self, group: str, key: str, value: float) -> None:
        """
        Translate a Mixxx control → MIDI CC and send.

        Raises MidiSendError if the control is unmapped, the port is not open,
        or the write fails. Callers must not report success unless this returns
        normally — a silent no-op here is indistinguishable from a real send.

        Encoding:
          - Binary controls (play, sync_enabled, etc.): value 0 or 127
          - Float 0.0–1.0 → 0–127
          - Float -1.0–1.0 (crossfader, rate) → 0–127 (center = 64)
          - Float 0.0–4.0 (EQ, pregain) → 0–127 (unity 1.0 = 32)
          - beatloop_size → index into BEATLOOP_SIZES, scaled to 0–127
        """
        from .controls import ACTIVE_DECK_CC, MIDI_CC_MAP

        mapping = MIDI_CC_MAP.get((group, key))
        deck_relative = False
        if mapping is None:
            mapping = MIDI_CC_MAP.get(("*", key))
            deck_relative = mapping is not None
        if mapping is None:
            raise MidiSendError(f"No CC mapping for ({group}, {key})")

        cc, scale = mapping
        midi_val = _encode(value, scale)

        if not self._connected:
            raise MidiSendError(
                f"MIDI port '{self.PORT_NAME}' is not open — nothing was sent. "
                "Is loopMIDI running with a 'mixxx-mcp' port?"
            )

        # Deck-relative controls share one CC across all decks, so tell the
        # script which deck to act on before sending it.
        if deck_relative and group.startswith("[Channel"):
            try:
                deck = int(group[len("[Channel"):-1])
            except ValueError:
                deck = 0
            if 1 <= deck <= 4:
                self._out.send_message([0xB0 | self.CHANNEL, ACTIVE_DECK_CC, deck])

        try:
            # Control Change: status=0xB0+channel, cc, value
            self._out.send_message([0xB0 | self.CHANNEL, cc, midi_val])
            log.debug("MIDI CC %d val %d → (%s, %s = %s)", cc, midi_val, group, key, value)
        except Exception as e:
            raise MidiSendError(f"MIDI send failed for ({group}, {key}): {e}") from e

    def request_resync(self) -> None:
        """Ask the controller script to re-push every watched value."""
        from .controls import RESYNC_CC

        if not self._connected:
            raise MidiSendError(
                f"MIDI port '{self.PORT_NAME}' is not open — cannot request resync."
            )
        self._out.send_message([0xB0 | self.CHANNEL, RESYNC_CC, 127])
        log.debug("Requested state resync (CC %d)", RESYNC_CC)

    # ── READ path: SysEx state from Mixxx ────────────────────────────────────

    def start_state_listener(self, store) -> bool:
        """
        Open the virtual port for input and feed incoming mixxx-mcp SysEx into
        `store`. Returns True if the listener is running.
        """
        if not HAS_RTMIDI:
            log.warning("python-rtmidi unavailable — state listener not started")
            return False
        try:
            self._in = rtmidi.MidiIn()
            idx = None
            for i, name in enumerate(self._in.get_ports()):
                if self.PORT_NAME in name:
                    idx = i
                    break
            # SysEx is filtered out by default; we depend on it entirely.
            self._in.ignore_types(sysex=False, timing=True, active_sense=True)
            if idx is None and (
                sys.platform.startswith("linux") or sys.platform == "darwin"
            ):
                # RtMidi virtual input and output ports are separate endpoints.
                # connect() creates the output/source; create the matching
                # input/destination so Mixxx can send state back to us.
                self._in.open_virtual_port(self.PORT_NAME)
                log.info(
                    "State listener created virtual MIDI input '%s'",
                    self.PORT_NAME,
                )
            elif idx is None:
                log.warning(
                    "MIDI input port '%s' not found — state will be unavailable",
                    self.PORT_NAME,
                )
                return False
            else:
                self._in.open_port(idx)
                log.info(
                    "State listener attached to MIDI input '%s'",
                    self._in.get_port_name(idx),
                )
            self._in.set_callback(self._on_midi, data=store)
            self._listening = True
            return True
        except Exception as e:
            log.error("Could not start state listener: %s", e)
            return False

    def _on_midi(self, event, store):
        """rtmidi callback. Ignores everything that is not our SysEx."""
        message, _delta = event
        if not message or message[0] != SYSEX_START:
            return  # our own outgoing CCs echo back here; ignore them
        if len(message) < 4 or message[1] != MCP_SYSEX_ID or message[-1] != SYSEX_END:
            return
        try:
            payload = bytes(message[2:-1]).decode("ascii")
            data = json.loads(payload)
            store.set(data["g"], data["k"], data["v"])
        except Exception as e:
            log.warning("Bad state SysEx (%d bytes): %s", len(message), e)

    def close(self):
        if self._out:
            self._out.close_port()
            self._connected = False
        if self._in:
            self._in.cancel_callback()
            self._in.close_port()
            self._listening = False


def _encode(value: float, scale: str) -> int:
    """Normalize float value to 0–127 based on scale type."""
    if scale == "binary":
        return 127 if value else 0
    elif scale == "unipolar":     # 0.0–1.0
        return int(max(0, min(1.0, value)) * 127)
    elif scale == "bipolar":      # -1.0–1.0, centred on 64
        # Centre must be exactly representable: mapping -1..1 onto 0..127
        # linearly puts centre at 63.5, so "centre the crossfader" could only
        # ever land on -0.008. 64 == 0.0, 1 == -1.0, 127 == +1.0.
        return int(round(max(-1.0, min(1.0, value)) * 63 + 64))
    elif scale == "eq":           # 0.0–4.0, unity=1.0 maps to 32
        return int(max(0, min(4.0, value)) / 4.0 * 127)
    elif scale == "beatloop":     # beats → index into BEATLOOP_SIZES → 0–127
        from .controls import BEATLOOP_SIZES

        idx = min(
            range(len(BEATLOOP_SIZES)),
            key=lambda i: abs(BEATLOOP_SIZES[i] - value),
        )
        # Chosen so the JS side's round(midi/127 * (len-1)) recovers idx exactly.
        return round(idx * 127 / (len(BEATLOOP_SIZES) - 1))
    elif scale == "raw":          # pass through 0–127
        return int(max(0, min(127, value)))
    else:
        return int(max(0, min(127, value * 127)))
