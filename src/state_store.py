"""
state_store.py — thread-safe cache of live Mixxx control state

Filled by MidiBridge's SysEx listener (see midi_bridge.py) and read by the MCP
state tools, so answering a state question costs no round-trip to Mixxx.

This replaces the original OSC listener: Mixxx has no OSC output, and its
controller script engine has no network access at all, so state comes back over
MIDI SysEx instead.
"""

import copy
import logging
import threading
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger("mixxx-mcp.state")


class StateStore:
    """
    Thread-safe KV cache for Mixxx control state.

    Key scheme: (group, key) → value
    Example: ("[Channel1]", "play") → 1.0
    """

    def __init__(self):
        self._state: Dict[Tuple[str, str], Any] = {}
        self._lock = threading.RLock()

    def get(self, group: str, key: str) -> Optional[Any]:
        with self._lock:
            return self._state.get((group, key))

    def set(self, group: str, key: str, value: Any):
        with self._lock:
            self._state[(group, key)] = value

    def snapshot(self) -> Dict[str, Dict[str, Any]]:
        """Return all state grouped by control group."""
        with self._lock:
            result: Dict[str, Dict[str, Any]] = {}
            for (grp, key), val in self._state.items():
                result.setdefault(grp, {})[key] = val
            return copy.deepcopy(result)
