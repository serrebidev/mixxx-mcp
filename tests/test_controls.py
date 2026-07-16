"""Regression tests for the CC allocation.

The original mapping generated hotcue CCs with
    base = 50 + (ch - 1) * 18 + (slot - 3) * 3
which ran to CC 85 and silently overwrote the Channel3, Channel4 and Master
blocks. Because the JS routed those CCs to hotcue handlers — which return
without doing anything unless the value is exactly 1.0 — the crossfader, the
master volume and all of deck 4 were dead, with no error anywhere.
"""

import pytest

from src.controls import (
    ACTIVE_DECK_CC,
    BEATLOOP_SIZES,
    FOCUS_TRACKS_TABLE,
    MIDI_CC_MAP,
    RESYNC_CC,
    resolve_channel,
    validate_group,
)


def test_no_two_controls_share_a_cc():
    seen = {}
    for (group, key), (cc, _scale) in MIDI_CC_MAP.items():
        assert cc not in seen, (
            f"CC {cc} assigned to both {seen.get(cc)} and {(group, key)}"
        )
        seen[cc] = (group, key)


def test_all_ccs_in_range():
    for (group, key), (cc, _scale) in MIDI_CC_MAP.items():
        assert 0 <= cc <= 127, f"CC {cc} out of range for ({group}, {key})"


def test_reserved_ccs_are_not_used_by_controls():
    used = {cc for cc, _ in MIDI_CC_MAP.values()}
    assert RESYNC_CC not in used
    assert ACTIVE_DECK_CC not in used


@pytest.mark.parametrize("group,key", [
    ("[Master]", "crossfader"),
    ("[Master]", "volume"),
    ("[Master]", "headVolume"),
    ("[Master]", "headMix"),
    ("[Master]", "balance"),
    ("[Channel4]", "play"),
    ("[Channel4]", "volume"),
    ("[Channel3]", "loop_halve"),
])
def test_previously_clobbered_controls_are_mapped(group, key):
    """These were all silently overwritten by hotcue routes."""
    assert (group, key) in MIDI_CC_MAP


def test_hotcues_mapped_for_decks_1_and_2():
    for deck in (1, 2):
        for slot in range(1, 9):
            for action in ("set", "goto", "clear"):
                assert (f"[Channel{deck}]", f"hotcue_{slot}_{action}") in MIDI_CC_MAP


def test_hotcues_not_claimed_for_decks_3_and_4():
    """Decks 3-4 have no hotcue CCs; the tools must fail loudly, not silently."""
    for deck in (3, 4):
        assert (f"[Channel{deck}]", "hotcue_1_set") not in MIDI_CC_MAP


def test_resolve_channel_rejects_bad_decks():
    assert resolve_channel(1) == "[Channel1]"
    for bad in (0, 5, -1):
        with pytest.raises(ValueError):
            resolve_channel(bad)


def test_validate_group_rejects_unknown():
    validate_group("[Channel1]")
    validate_group("[Master]")
    with pytest.raises(ValueError):
        validate_group("[Nonsense]")


def test_beatloop_sizes_sorted_and_positive():
    assert BEATLOOP_SIZES == sorted(BEATLOOP_SIZES)
    assert all(s > 0 for s in BEATLOOP_SIZES)


# ── Library / loading ─────────────────────────────────────────────────────────

def test_load_selected_track_is_deck_relative():
    """One CC serves all four decks; ACTIVE_DECK_CC names the target."""
    assert ("*", "LoadSelectedTrack") in MIDI_CC_MAP
    for deck in (1, 2, 3, 4):
        assert (f"[Channel{deck}]", "LoadSelectedTrack") not in MIDI_CC_MAP


@pytest.mark.parametrize("key", [
    "MoveVertical", "GoToItem", "clear_search", "focused_widget",
])
def test_library_controls_are_mapped(key):
    assert ("[Library]", key) in MIDI_CC_MAP


def test_move_vertical_is_signed():
    """A relative move must carry a sign; unipolar/raw cannot express 'up'."""
    _cc, scale = MIDI_CC_MAP[("[Library]", "MoveVertical")]
    assert scale == "signed7"


def test_library_group_validates():
    validate_group("[Library]")


def test_focus_ids_match_mixxx_enum():
    assert FOCUS_TRACKS_TABLE == 3
