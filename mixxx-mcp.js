/**
 * mixxx-mcp.js — Mixxx Controller Script v1.1.0
 *
 * Bridges Mixxx ControlObjects ↔ mixxx-mcp Python server via:
 *   WRITE:  Python → MIDI CC (ch 1) → this script → engine.setValue()
 *   READ:   engine.makeConnection() → MIDI SysEx → Python state store
 *
 * Both directions share the one virtual MIDI port. The read path uses SysEx
 * because Mixxx's script engine has no network access at all (no
 * XMLHttpRequest, no fetch), and because SysEx can carry arbitrary-length
 * payloads — a 7-bit CC cannot represent a BPM or a track title.
 * Mixxx ignores the SysEx it emits (it matches no CC mapping), and the Python
 * side ignores the CCs it emits, so the shared port does not feed back.
 *
 * Compatible: Mixxx 2.4+ (QJSEngine)
 *
 * Install: %LOCALAPPDATA%\Mixxx\controllers\  (Windows)
 *          ~/.mixxx/controllers/               (Linux)
 *          ~/Library/.../Mixxx/controllers/   (macOS)
 */

"use strict";

// ── Config ────────────────────────────────────────────────────────────────
// SysEx: F0 <MCP_SYSEX_ID> <ascii JSON> F7. 0x7D is the MIDI-reserved
// "non-commercial / educational" manufacturer ID.
const MCP_SYSEX_ID = 0x7D;
const MCP_DEBOUNCE_MS = 50; // min ms between state pushes per control

// ── CC → (group, key, scale) routing table ────────────────────────────────
// MUST mirror MIDI_CC_MAP in src/controls.py exactly. Built from the same block
// layout so the two cannot drift:
//   0–37 Ch1 (0–13 controls, 14–37 hotcues 1–8), 38–75 Ch2, 76–87 Ch3,
//   88–99 Ch4, 100–104 Master, 105–111 Effects, 112–118 nudge/beatjump.
const DECK_CONTROLS = [
    ["play",              "binary"],
    ["cue_default",       "binary"],
    ["sync_enabled",      "binary"],
    ["volume",            "unipolar"],
    ["pregain",           "eq"],
    ["rate",              "bipolar"],
    ["filterLow",         "eq"],
    ["filterMid",         "eq"],
    ["filterHigh",        "eq"],
    ["beatloop_size",     "beatloop"],
    ["beatloop_activate", "binary"],
    ["reloop_toggle",     "binary"],
    ["loop_halve",        "binary"],
    ["loop_double",       "binary"],
];

const DECK34_CONTROLS = [
    ["play",              "binary"],
    ["cue_default",       "binary"],
    ["sync_enabled",      "binary"],
    ["volume",            "unipolar"],
    ["pregain",           "eq"],
    ["rate",              "bipolar"],
    ["filterLow",         "eq"],
    ["filterMid",         "eq"],
    ["filterHigh",        "eq"],
    ["beatloop_activate", "binary"],
    ["loop_halve",        "binary"],
    ["loop_double",       "binary"],
];

// Not controls: the server sends these to ask for a full state re-push, and to
// select which deck the deck-relative CCs (nudge/beatjump) act on.
// Must match RESYNC_CC / ACTIVE_DECK_CC in src/controls.py.
const RESYNC_CC = 119;
const ACTIVE_DECK_CC = 120;

const HOTCUE_ACTIONS = ["set", "goto", "clear"];
const DECK12_BASE = { 1: 0, 2: 38 };
const DECK12_HOTCUE_OFFSET = 14;
const DECK34_BASE = { 3: 76, 4: 88 };
const MASTER_BASE = 100;
const FX_BASE = 105;
const NUDGE_BASE = 112;
const LOAD_SELECTED_CC = 121;
const LIBRARY_BASE = 122;
const BEATSYNC_PHASE_CC = 126;
const BPM_CC = 127;
// Must match BPM_MIN in src/controls.py.
const BPM_MIN = 60;

const CC_ROUTE = {};

(function buildRoutes() {
    // Decks 1–2: controls + hotcues 1–8
    for (const deck of [1, 2]) {
        const base = DECK12_BASE[deck];
        const group = `[Channel${deck}]`;
        DECK_CONTROLS.forEach(([key, scale], i) => {
            CC_ROUTE[base + i] = { group, key, scale };
        });
        for (let slot = 1; slot <= 8; slot++) {
            const slotBase = base + DECK12_HOTCUE_OFFSET + (slot - 1) * 3;
            HOTCUE_ACTIONS.forEach((action, j) => {
                CC_ROUTE[slotBase + j] = {
                    group, key: `hotcue_${slot}_${action}`, scale: "binary",
                };
            });
        }
    }
    // Decks 3–4: reduced control set
    for (const deck of [3, 4]) {
        const base = DECK34_BASE[deck];
        const group = `[Channel${deck}]`;
        DECK34_CONTROLS.forEach(([key, scale], i) => {
            CC_ROUTE[base + i] = { group, key, scale };
        });
    }
    // Master
    [["crossfader", "bipolar"], ["volume", "unipolar"], ["headVolume", "unipolar"],
     ["headMix", "bipolar"], ["balance", "bipolar"]].forEach(([key, scale], i) => {
        CC_ROUTE[MASTER_BASE + i] = { group: "[Master]", key, scale };
    });
    // Effects: 4 unit mixes then 3 effect enables
    for (let i = 0; i < 4; i++) {
        CC_ROUTE[FX_BASE + i] = {
            group: `[EffectRack1_EffectUnit${i + 1}]`, key: "mix", scale: "unipolar",
        };
    }
    for (let i = 0; i < 3; i++) {
        CC_ROUTE[FX_BASE + 4 + i] = {
            group: `[EffectRack1_EffectUnit1_Effect${i + 1}]`, key: "enabled", scale: "binary",
        };
    }
    // Deck-relative nudge / beatjump
    [["rate_perm_up_small", "binary"], ["rate_perm_down_small", "binary"],
     ["rate_perm_up", "binary"], ["rate_perm_down", "binary"],
     ["beatjump_size", "raw"], ["beatjump_forward", "binary"],
     ["beatjump_backward", "binary"]].forEach(([key, scale], i) => {
        CC_ROUTE[NUDGE_BASE + i] = { group: null, key, scale, deckRelative: true };
    });
    // Deck-relative load: one CC for all four decks, ACTIVE_DECK_CC picks the target.
    CC_ROUTE[LOAD_SELECTED_CC] = {
        group: null, key: "LoadSelectedTrack", scale: "binary", deckRelative: true,
    };
    // Track-table navigation uses the focus-independent legacy control. The
    // [Library] Move controls emulate keypresses and fail when Mixxx does not
    // have keyboard focus (which is normally the case while an agent runs).
    CC_ROUTE[LIBRARY_BASE] = {
        group: "[Playlist]", key: "SelectTrackKnob", scale: "signed7",
    };
    [["GoToItem", "binary"], ["clear_search", "binary"],
     ["focused_widget", "raw"]].forEach(([key, scale], i) => {
        CC_ROUTE[LIBRARY_BASE + 1 + i] = { group: "[Library]", key, scale };
    });
    // Beatmatching: phase alignment and beatgrid repair, both deck-relative.
    CC_ROUTE[BEATSYNC_PHASE_CC] = {
        group: null, key: "beatsync_phase", scale: "binary", deckRelative: true,
    };
    CC_ROUTE[BPM_CC] = {
        group: null, key: "bpm", scale: "bpm", deckRelative: true,
    };
})();

// ── Controls to watch (state push to Python) ──────────────────────────────
const WATCH = {
    // NB: track_artist / track_title are NOT ControlObjects in Mixxx — Mixxx
    // logs "non-existent" and returns 0.0 for them, so watching them only
    // produced misleading zeroes. Track metadata is not exposed to controller
    // scripts; use track_loaded/duration to tell whether a deck has a track.
    // beat_distance is the only way to see beat phase. Without it "synced"
    // decks that sound unmixed cannot be told from aligned ones, and
    // beatsync_phase cannot be verified -- it reports nothing back.
    "[Channel1]": ["play","bpm","playposition","volume","pregain","rate",
                   "sync_enabled","loop_enabled","beatloop_size",
                   "filterLow","filterMid","filterHigh",
                   "track_loaded","duration","track_samplerate","beat_distance",
                   "hotcue_1_position","hotcue_2_position","hotcue_3_position",
                   "hotcue_4_position"],
    "[Channel2]": ["play","bpm","playposition","volume","pregain","rate",
                   "sync_enabled","loop_enabled","beatloop_size",
                   "filterLow","filterMid","filterHigh",
                   "track_loaded","duration","track_samplerate","beat_distance"],
    // track_loaded/duration are watched on every deck, not just 1–2: they are the
    // only way to tell whether a deck has a track (there is no track metadata),
    // and load_selected_track() reads duration back to confirm what it loaded.
    "[Channel3]": ["play","bpm","playposition","volume","rate","sync_enabled",
                   "track_loaded","duration","beat_distance"],
    "[Channel4]": ["play","bpm","playposition","volume","rate","sync_enabled",
                   "track_loaded","duration","beat_distance"],
    "[Master]":   ["crossfader","volume","headVolume","headMix","balance"],
    "[EffectRack1_EffectUnit1]": ["mix","enabled"],
    "[EffectRack1_EffectUnit2]": ["mix","enabled"],
};

// ── Scale decode (MIDI 0–127 → Mixxx float) ───────────────────────────────
function decode(midiVal, scale) {
    switch (scale) {
        case "binary":   return midiVal >= 64 ? 1.0 : 0.0;
        case "unipolar": return midiVal / 127.0;
        // Centred on 64 so 0.0 is exactly representable — see _encode() in
        // src/midi_bridge.py. 64 = 0.0, 1 = -1.0, 127 = +1.0.
        case "bipolar":  return Math.max(-1.0, Math.min(1.0, (midiVal - 64) / 63.0));
        case "eq":       return (midiVal / 127.0) * 4.0;
        case "beatloop": {
            // Index into BEATLOOP_SIZES in src/controls.py — keep in sync.
            const sizes = [0.03125,0.0625,0.125,0.25,0.5,1,2,4,8,16,32,64];
            return sizes[Math.round((midiVal / 127.0) * (sizes.length - 1))] || 4;
        }
        // Relative step count centred on 64 — see _encode() in src/midi_bridge.py.
        case "signed7": return midiVal - 64;
        case "bpm":     return midiVal + BPM_MIN;
        case "raw": return midiVal;
        default:    return midiVal / 127.0;
    }
}

// ── SysEx state push (fire-and-forget, debounced) ─────────────────────────
const _lastPush = {};

// SysEx data bytes must be 7-bit, so escape anything above ASCII '~' as \uXXXX.
// JSON.stringify already escapes control characters and quotes, and Python's
// json.loads decodes the \uXXXX escapes back to the original text.
function asciiSafe(s) {
    return s.replace(/[\u007F-\uFFFF]/g, function (c) {
        return "\\u" + ("0000" + c.charCodeAt(0).toString(16)).slice(-4);
    });
}

function pushState(group, key, value) {
    const ck = `${group}/${key}`;
    const now = Date.now();
    if (_lastPush[ck] && (now - _lastPush[ck]) < MCP_DEBOUNCE_MS) return;
    _lastPush[ck] = now;

    try {
        const json = asciiSafe(JSON.stringify({ g: group, k: key, v: value }));
        const bytes = [0xF0, MCP_SYSEX_ID];
        for (let i = 0; i < json.length; i++) {
            bytes.push(json.charCodeAt(i) & 0x7F);
        }
        bytes.push(0xF7);
        midi.sendSysexMsg(bytes, bytes.length);
    } catch (e) {
        // Nothing listening, or the port is closed — writes still work.
    }
}

// ── Trigger keys (use triggerControl, not setValue) ───────────────────────
const TRIGGER_KEYS = new Set([
    "cue_default","beatloop_activate","reloop_toggle",
    "loop_halve","loop_double","beatjump_forward","beatjump_backward",
    "rate_perm_up_small","rate_perm_down_small","rate_perm_up","rate_perm_down",
    "LoadSelectedTrack","GoToItem","clear_search","beatsync_phase",
]);

// Relative encoders: Mixxx drops a setValue that does not change the control
// (ControlDoublePrivate ignores no-ops), so sending SelectTrackKnob=1 twice in a
// row would move once and then silently do nothing. Reset to 0 first — a
// 0-step move is itself a no-op — so the real value is always a change.
const RELATIVE_KEYS = new Set(["SelectTrackKnob"]);

// ── Main controller object ────────────────────────────────────────────────
// Declared with `var`, not `const`: Mixxx resolves the mapping's function names
// (e.g. "MixxxMCP.handleCC") in a separate evaluate() call, which only sees the
// global object. A top-level `const` stays lexically scoped and is invisible there.
var MixxxMCP = {
    _activeDeck: 1,
    _connections: [],

    getActiveGroup() {
        return `[Channel${this._activeDeck}]`;
    },

    // ── Mixxx lifecycle ──────────────────────────────────────────────────
    init(id, debug) {
        console.log("[mixxx-mcp] init — wiring state connections");

        // Wire live state push for all watched controls
        for (const [grp, keys] of Object.entries(WATCH)) {
            for (const key of keys) {
                try {
                    const conn = engine.makeConnection(grp, key, function(val, g, k) {
                        pushState(g, k, val);
                    });
                    this._connections.push(conn);
                } catch(e) {
                    // Control may not exist in this Mixxx version — skip silently
                }
            }
        }

        // Push an initial snapshot so a server that is already running sees
        // values immediately. Mixxx's script engine has no setTimeout;
        // engine.beginTimer(ms, cb, true) is the one-shot equivalent.
        engine.beginTimer(1000, () => this.pushSnapshot(), true);

        console.log(`[mixxx-mcp] ready — ${this._connections.length} connections active`);
    },

    /**
     * Re-push every watched control. Mixxx normally starts before the MCP
     * server, so the boot snapshot lands with nothing listening; the server
     * sends RESYNC_CC on connect to ask for this again.
     */
    pushSnapshot() {
        let n = 0;
        for (const [grp, keys] of Object.entries(WATCH)) {
            for (const key of keys) {
                try {
                    const val = engine.getValue(grp, key);
                    if (val !== undefined && val !== null) {
                        delete _lastPush[`${grp}/${key}`]; // bypass debounce
                        pushState(grp, key, val);
                        n++;
                    }
                } catch(e) {}
            }
        }
        console.log(`[mixxx-mcp] state snapshot pushed (${n} values)`);
    },

    shutdown(id) {
        this._connections.forEach(c => { try { c.disconnect(); } catch(e) {} });
        this._connections = [];
        console.log("[mixxx-mcp] shutdown");
    },

    // ── MIDI CC handler ──────────────────────────────────────────────────
    handleCC(channel, control, value, status, group) {
        if (control === RESYNC_CC) {
            this.pushSnapshot();
            return;
        }
        if (control === ACTIVE_DECK_CC) {
            // The server sends this immediately before each deck-relative CC.
            if (value >= 1 && value <= 4) {
                this._activeDeck = value;
            }
            return;
        }
        const route = CC_ROUTE[control];
        if (!route) {
            console.log(`[mixxx-mcp] Unknown CC ${control} — ignored`);
            return;
        }

        const grp = route.deckRelative ? this.getActiveGroup() : route.group;
        const val  = decode(value, route.scale);

        // Hotcue triggers
        if (route.key && /^hotcue_\d+_(set|goto|clear)$/.test(route.key)) {
            if (val === 1.0) script.triggerControl(grp, route.key, 100);
            return;
        }

        // Pulse triggers
        if (TRIGGER_KEYS.has(route.key)) {
            if (val === 1.0) script.triggerControl(grp, route.key, 100);
            return;
        }

        if (RELATIVE_KEYS.has(route.key)) {
            if (val === 0) return;
            engine.setValue(grp, route.key, 0);
            engine.setValue(grp, route.key, val);
            console.log(`[mixxx-mcp] MOVE ${grp}.${route.key} by ${val}`);
            return;
        }

        engine.setValue(grp, route.key, val);
        console.log(`[mixxx-mcp] SET ${grp}.${route.key} = ${val}`);
    },
};
