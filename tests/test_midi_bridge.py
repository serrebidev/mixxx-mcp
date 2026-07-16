"""Tests for value encoding, SysEx state parsing, and honest failure reporting."""

import json

import pytest

from src.controls import BEATLOOP_SIZES
from src.midi_bridge import MCP_SYSEX_ID, MidiBridge, MidiSendError, _encode
from src.state_store import StateStore


# ── encoding ─────────────────────────────────────────────────────────────────

def test_binary_encoding():
    assert _encode(1.0, "binary") == 127
    assert _encode(0.0, "binary") == 0


def test_unipolar_encoding():
    assert _encode(0.0, "unipolar") == 0
    assert _encode(1.0, "unipolar") == 127


def test_bipolar_encoding_is_clamped():
    assert _encode(-1.0, "bipolar") == 1
    assert _encode(1.0, "bipolar") == 127
    # out-of-range input must not produce an invalid MIDI byte
    assert 0 <= _encode(-5.0, "bipolar") <= 127
    assert 0 <= _encode(5.0, "bipolar") <= 127


def test_signed7_encoding_roundtrips_through_js_decode():
    """MoveVertical(-3) must arrive as -3, not as an unsigned 3 or a clamp to 0."""
    def js_decode(midi_val):
        return midi_val - 64

    for rows in (-64, -3, -1, 0, 1, 3, 63):
        assert js_decode(_encode(rows, "signed7")) == rows


def test_signed7_encoding_is_clamped_to_valid_midi():
    for rows in (-500, 500):
        assert 0 <= _encode(rows, "signed7") <= 127


def test_bipolar_centre_roundtrips_exactly():
    """
    "Centre the crossfader" must actually centre it. A linear -1..1 -> 0..127
    mapping puts centre at 63.5, so it could only ever reach -0.008.
    """
    def js_decode(midi_val):
        return max(-1.0, min(1.0, (midi_val - 64) / 63.0))

    assert _encode(0.0, "bipolar") == 64
    assert js_decode(_encode(0.0, "bipolar")) == 0.0
    assert js_decode(_encode(-1.0, "bipolar")) == -1.0
    assert js_decode(_encode(1.0, "bipolar")) == 1.0


def test_all_scales_produce_valid_midi_bytes():
    scales = ["binary", "unipolar", "bipolar", "eq", "beatloop", "raw"]
    for scale in scales:
        for v in (-10, -1, 0, 0.5, 1, 4, 64, 1000):
            out = _encode(v, scale)
            assert 0 <= out <= 127, f"{scale} encoded {v} as {out}"


def test_beatloop_roundtrips_through_the_js_decoder():
    """
    Python encodes beatloop_size as an index into BEATLOOP_SIZES; the JS decodes
    with round(midi/127 * (len-1)). Previously Python sent the raw beat count,
    so set_loop(4) arrived as 0.03125.
    """
    def js_decode(midi_val):
        return BEATLOOP_SIZES[round((midi_val / 127.0) * (len(BEATLOOP_SIZES) - 1))]

    for beats in BEATLOOP_SIZES:
        assert js_decode(_encode(beats, "beatloop")) == beats


# ── honest failures ──────────────────────────────────────────────────────────

def test_send_control_raises_when_port_closed():
    b = MidiBridge()  # never connected
    with pytest.raises(MidiSendError):
        b.send_control("[Master]", "crossfader", 0.0)


def test_send_control_raises_for_unmapped_control():
    b = MidiBridge()
    b._connected = True
    b._out = _FakeOut()
    with pytest.raises(MidiSendError):
        b.send_control("[Channel3]", "hotcue_1_set", 1.0)


# ── SysEx state parsing ──────────────────────────────────────────────────────

class _FakeOut:
    def __init__(self):
        self.sent = []

    def send_message(self, msg):
        self.sent.append(list(msg))


def _sysex(payload: str):
    return [0xF0, MCP_SYSEX_ID] + [ord(c) for c in payload] + [0xF7]


def test_on_midi_stores_state_from_sysex():
    b = MidiBridge()
    store = StateStore()
    msg = _sysex(json.dumps({"g": "[Channel1]", "k": "bpm", "v": 128.5}))
    b._on_midi((msg, 0.0), store)
    assert store.get("[Channel1]", "bpm") == 128.5


def test_on_midi_decodes_escaped_non_ascii():
    """The JS escapes non-ASCII as \\uXXXX so SysEx stays 7-bit."""
    b = MidiBridge()
    store = StateStore()
    payload = '{"g":"[Channel1]","k":"x","v":"Caf\\u00e9"}'
    assert all(ord(c) < 128 for c in payload)
    b._on_midi((_sysex(payload), 0.0), store)
    assert store.get("[Channel1]", "x") == "Café"


def test_on_midi_ignores_our_own_echoed_cc():
    """Our outgoing CCs loop back on the shared port and must be ignored."""
    b = MidiBridge()
    store = StateStore()
    b._on_midi(([0xB0, 100, 64], 0.0), store)
    assert store.snapshot() == {}


def test_on_midi_ignores_foreign_sysex():
    b = MidiBridge()
    store = StateStore()
    b._on_midi(([0xF0, 0x43, 0x01, 0xF7], 0.0), store)  # some other vendor
    assert store.snapshot() == {}


def test_on_midi_survives_malformed_payload():
    b = MidiBridge()
    store = StateStore()
    b._on_midi((_sysex("not json"), 0.0), store)  # must not raise
    assert store.snapshot() == {}


# ── deck-relative targeting ──────────────────────────────────────────────────

def test_deck_relative_control_selects_the_deck_first():
    """
    nudge/beatjump share one CC across decks, so the script must be told which
    deck to act on. Previously _activeDeck was never set and every nudge landed
    on deck 1.
    """
    from src.controls import ACTIVE_DECK_CC

    b = MidiBridge()
    b._connected = True
    b._out = _FakeOut()
    b.send_control("[Channel2]", "rate_perm_up_small", 1.0)

    assert len(b._out.sent) == 2, "expected an active-deck select then the trigger"
    assert b._out.sent[0][1] == ACTIVE_DECK_CC
    assert b._out.sent[0][2] == 2, "should select deck 2"


def test_non_deck_relative_control_sends_one_message():
    b = MidiBridge()
    b._connected = True
    b._out = _FakeOut()
    b.send_control("[Master]", "crossfader", 0.0)
    assert len(b._out.sent) == 1
