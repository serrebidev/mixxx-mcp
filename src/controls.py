"""
controls.py — Mixxx control group/key → MIDI CC mapping table

CC allocation (0–127, single MIDI channel). Every CC is used by exactly one
(group, key); the assertion at the bottom of this module enforces that.

  0–37    Channel1   (0–13 controls, 14–37 hotcues 1–8)
  38–75   Channel2   (38–51 controls, 52–75 hotcues 1–8)
  76–87   Channel3   (12 controls, no hotcues)
  88–99   Channel4   (12 controls, no hotcues)
  100–104 Master     (crossfader, volume, headVolume, headMix, balance)
  105–111 Effects    (4 unit mixes, 3 effect enables)
  112–118 Deck-relative nudge / beatjump
  119     Resync request (server → script: re-push the full state snapshot)
  120     Active-deck select (server → script: target for deck-relative CCs)
  121     Deck-relative LoadSelectedTrack
  122–125 Library     (MoveVertical, GoToItem, clear_search, focused_widget)
  126     Deck-relative beatsync_phase
  127     Deck-relative bpm

The 128-CC space is now fully allocated. Anything further needs a SysEx command
channel (Mixxx dispatches SysEx input to a script via incomingData), which would
also carry full float precision instead of 7 bits.

Hotcues are only mapped for decks 1–2: 8 slots x 3 actions x 4 decks would not
fit in the 128-CC space alongside everything else.

Loading a track is *selection-based*, not path-based: Mixxx exposes no control
that takes a file path or a track id, so LoadSelectedTrack acts on whatever the
library view currently highlights. The [Library] block above is what makes that
selection reachable — see load_selected_track() in server.py.

The companion Mixxx JS script (mixxx-mcp.js) mirrors this table exactly and must
be kept in sync — see CC_ROUTE there.
"""

from typing import Dict, Tuple, Optional

# Beatloop sizes, in beats. Both sides encode/decode beatloop_size as an index
# into this list (see the "beatloop" scale) rather than as a raw beat count —
# a raw 0–127 MIDI byte cannot represent fractional sizes like 0.125.
BEATLOOP_SIZES = [0.03125, 0.0625, 0.125, 0.25, 0.5, 1, 2, 4, 8, 16, 32, 64]

# Per-deck control offsets within a deck's CC block (decks 1–2).
_DECK_CONTROLS = [
    ("play",              "binary"),
    ("cue_default",       "binary"),
    ("sync_enabled",      "binary"),
    ("volume",            "unipolar"),
    ("pregain",           "eq"),
    ("rate",              "bipolar"),
    ("filterLow",         "eq"),
    ("filterMid",         "eq"),
    ("filterHigh",        "eq"),
    ("beatloop_size",     "beatloop"),
    ("beatloop_activate", "binary"),
    ("reloop_toggle",     "binary"),
    ("loop_halve",        "binary"),
    ("loop_double",       "binary"),
]

# Decks 3–4 carry a reduced set (no hotcues, no beatloop_size/reloop_toggle).
_DECK34_CONTROLS = [
    ("play",              "binary"),
    ("cue_default",       "binary"),
    ("sync_enabled",      "binary"),
    ("volume",            "unipolar"),
    ("pregain",           "eq"),
    ("rate",              "bipolar"),
    ("filterLow",         "eq"),
    ("filterMid",         "eq"),
    ("filterHigh",        "eq"),
    ("beatloop_activate", "binary"),
    ("loop_halve",        "binary"),
    ("loop_double",       "binary"),
]

_HOTCUE_ACTIONS = ["set", "goto", "clear"]

# Not a control: sending this CC asks mixxx-mcp.js to re-push every watched
# value. Mixxx pushes its snapshot when *it* starts, which is usually before
# this server exists, so without this the state store starts out empty.
RESYNC_CC = 119

# Not a control: selects which deck the deck-relative CCs (nudge/beatjump/
# LoadSelectedTrack) act on. Those share one CC across all decks, so the script
# needs to be told the target first — otherwise every nudge lands on deck 1.
ACTIVE_DECK_CC = 120

# Widget ids for [Library] focused_widget, mirroring Mixxx's
# LibraryControl::FocusWidget enum. Set focused_widget before a MoveVertical so
# the move lands on the tracks table rather than the sidebar or the searchbox.
FOCUS_NONE = 0
FOCUS_SEARCHBAR = 1
FOCUS_SIDEBAR = 2
FOCUS_TRACKS_TABLE = 3

# Block base addresses.
DECK12_BASE = {1: 0, 2: 38}
DECK12_HOTCUE_OFFSET = 14
DECK34_BASE = {3: 76, 4: 88}
MASTER_BASE = 100
FX_BASE = 105
NUDGE_BASE = 112
LOAD_SELECTED_CC = 121
LIBRARY_BASE = 122
BEATSYNC_PHASE_CC = 126
BPM_CC = 127

# "bpm" encodes as an offset from BPM_MIN, one MIDI step per BPM, so whole
# BPMs survive the 7-bit trip exactly: 140 -> 80 -> 140. A proportional scale
# would land on 140.1 and quietly drift the beatgrid over a long track.
BPM_MIN = 60
BPM_MAX = BPM_MIN + 127

DECKS_WITH_HOTCUES = (1, 2)

# (group, key) → (cc_number, scale)
# scale: "binary" | "unipolar" | "bipolar" | "eq" | "beatloop" | "raw"
MIDI_CC_MAP: Dict[Tuple[str, str], Tuple[int, str]] = {}

# ── Decks 1–2: controls + hotcues 1–8 ─────────────────────────────────────────
for _deck, _base in DECK12_BASE.items():
    _grp = f"[Channel{_deck}]"
    for _i, (_key, _scale) in enumerate(_DECK_CONTROLS):
        MIDI_CC_MAP[(_grp, _key)] = (_base + _i, _scale)
    for _slot in range(1, 9):
        _slot_base = _base + DECK12_HOTCUE_OFFSET + (_slot - 1) * 3
        for _j, _action in enumerate(_HOTCUE_ACTIONS):
            MIDI_CC_MAP[(_grp, f"hotcue_{_slot}_{_action}")] = (_slot_base + _j, "binary")

# ── Decks 3–4: reduced control set ────────────────────────────────────────────
for _deck, _base in DECK34_BASE.items():
    _grp = f"[Channel{_deck}]"
    for _i, (_key, _scale) in enumerate(_DECK34_CONTROLS):
        MIDI_CC_MAP[(_grp, _key)] = (_base + _i, _scale)

# ── Master ────────────────────────────────────────────────────────────────────
for _i, (_key, _scale) in enumerate([
    ("crossfader", "bipolar"),
    ("volume",     "unipolar"),
    ("headVolume", "unipolar"),
    ("headMix",    "bipolar"),
    ("balance",    "bipolar"),
]):
    MIDI_CC_MAP[("[Master]", _key)] = (MASTER_BASE + _i, _scale)

# ── Effects ───────────────────────────────────────────────────────────────────
for _i in range(4):
    MIDI_CC_MAP[(f"[EffectRack1_EffectUnit{_i + 1}]", "mix")] = (FX_BASE + _i, "unipolar")
for _i in range(3):
    MIDI_CC_MAP[(f"[EffectRack1_EffectUnit1_Effect{_i + 1}]", "enabled")] = (
        FX_BASE + 4 + _i, "binary",
    )

# ── Deck-relative nudge / beatjump (wildcard group) ───────────────────────────
for _i, (_key, _scale) in enumerate([
    ("rate_perm_up_small",   "binary"),
    ("rate_perm_down_small", "binary"),
    ("rate_perm_up",         "binary"),
    ("rate_perm_down",       "binary"),
    ("beatjump_size",        "raw"),
    ("beatjump_forward",     "binary"),
    ("beatjump_backward",    "binary"),
]):
    MIDI_CC_MAP[("*", _key)] = (NUDGE_BASE + _i, _scale)

# LoadSelectedTrack is deck-relative for the same reason nudge is: one CC for
# all four decks, with ACTIVE_DECK_CC naming the target first.
MIDI_CC_MAP[("*", "LoadSelectedTrack")] = (LOAD_SELECTED_CC, "binary")

# sync_enabled alone matches tempo but leaves the beats out of phase, which is
# what "synced but sounds wrong" actually is. beatsync_phase realigns them.
MIDI_CC_MAP[("*", "beatsync_phase")] = (BEATSYNC_PHASE_CC, "binary")

# Writing bpm moves the rate slider to reach that tempo; it does not touch the
# beatgrid. Verified against Mixxx 2.5: bpm 138 -> 145 left rate at -0.634.
# It is set_rate() without having to know the deck's rate range.
MIDI_CC_MAP[("*", "bpm")] = (BPM_CC, "bpm")

# ── Library navigation ────────────────────────────────────────────────────────
# MoveVertical is "signed7" rather than "raw": it is a relative move, so it must
# carry a sign, and 0–127 cannot. focused_widget is an absolute enum id, so it
# stays raw.
for _i, (_key, _scale) in enumerate([
    ("MoveVertical",   "signed7"),
    ("GoToItem",       "binary"),
    ("clear_search",   "binary"),
    ("focused_widget", "raw"),
]):
    MIDI_CC_MAP[("[Library]", _key)] = (LIBRARY_BASE + _i, _scale)


# ── Integrity checks ──────────────────────────────────────────────────────────
# An earlier revision generated hotcue CCs that silently overwrote the Channel3,
# Channel4 and Master blocks, so those controls were dead. Fail loudly instead.
def _assert_no_collisions() -> None:
    seen: Dict[int, Tuple[str, str]] = {}
    for (group, key), (cc, _scale) in MIDI_CC_MAP.items():
        if not 0 <= cc <= 127:
            raise AssertionError(f"CC {cc} out of range for ({group}, {key})")
        if cc in seen:
            raise AssertionError(
                f"CC {cc} assigned to both {seen[cc]} and {(group, key)}"
            )
        seen[cc] = (group, key)


_assert_no_collisions()


# ── Reverse lookup: CC → (group, key) ────────────────────────────────────────
REVERSE_MAP: Dict[int, Tuple[str, str]] = {
    cc: (group, key)
    for (group, key), (cc, _) in MIDI_CC_MAP.items()
}

# ── Validation ────────────────────────────────────────────────────────────────
VALID_GROUP_PREFIXES = [
    "[Channel", "[Sampler", "[Master]", "[Playlist]", "[Library]",
    "[PreviewDeck", "[EffectRack", "[Microphone",
]


def validate_group(group: str):
    if not any(group.startswith(p) for p in VALID_GROUP_PREFIXES):
        raise ValueError(
            f"Unknown group '{group}'. Valid prefixes: {VALID_GROUP_PREFIXES}"
        )


def resolve_channel(deck: int) -> str:
    if not 1 <= deck <= 4:
        raise ValueError(f"Deck must be 1–4, got {deck}")
    return f"[Channel{deck}]"


# ── Full control reference (for discoverability) ───────────────────────────────
CONTROL_MAP = {
    "[ChannelN]": {
        "play":              "Binary. 1=play, 0=pause.",
        "cue_default":       "Binary. Trigger CUE action.",
        "sync_enabled":      "Binary. BPM sync on/off.",
        "volume":            "0.0–1.0. Channel fader.",
        "pregain":           "0.0–4.0. Pre-fader gain (trim).",
        "rate":              "-1.0–1.0. Tempo pitch slider.",
        "filterLow":         "0.0–4.0. EQ low band.",
        "filterMid":         "0.0–4.0. EQ mid band.",
        "filterHigh":        "0.0–4.0. EQ high band.",
        "beatloop_size":     "Float beats. Decks 1–2 only. Snapped to BEATLOOP_SIZES.",
        "beatloop_activate": "Binary. Activate/deactivate beat loop.",
        "reloop_toggle":     "Binary. Toggle loop on/off. Decks 1–2 only.",
        "loop_halve":        "Binary. Halve loop length (trigger).",
        "loop_double":       "Binary. Double loop length (trigger).",
        "hotcue_N_set":      "Binary. Set hotcue N (1–8). Decks 1–2 only.",
        "hotcue_N_goto":     "Binary. Jump to hotcue N. Decks 1–2 only.",
        "hotcue_N_clear":    "Binary. Clear hotcue N. Decks 1–2 only.",
        "beatjump_size":     "Float. Beatjump size in beats.",
        "beatjump_forward":  "Binary. Jump forward by beatjump_size.",
        "beatjump_backward": "Binary. Jump backward by beatjump_size.",
        "rate_perm_up_small":"Binary. Nudge tempo up small step.",
        "rate_perm_down_small":"Binary. Nudge tempo down small step.",
        "bpm":               "Read-only. Current BPM.",
        "playposition":      "0.0–1.0. Current playback position.",
        "duration":          "Read-only. Track duration in seconds.",
        "track_artist":      "Read-only. Loaded track artist.",
        "track_title":       "Read-only. Loaded track title.",
    },
    "[Master]": {
        "crossfader":  "-1.0–1.0. Crossfader position.",
        "volume":      "0.0–1.0. Master output volume.",
        "headVolume":  "0.0–5.0. Headphone output volume.",
        "headMix":     "-1.0–1.0. Headphone mix (cue vs master).",
        "balance":     "-1.0–1.0. Master balance.",
    },
    "[EffectRack1_EffectUnitN]": {
        "mix":         "0.0–1.0. Wet/dry mix for the effect unit.",
        "enabled":     "Binary. Enable/disable effect unit.",
    },
    "[EffectRack1_EffectUnitN_EffectM]": {
        "enabled":     "Binary. Enable/disable individual effect slot.",
    },
}
