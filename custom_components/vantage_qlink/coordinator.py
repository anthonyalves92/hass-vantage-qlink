"""Polling coordinator + physical->contractor mapping learner.

Entities are addressed by *contractor number* (VGL/VLO), but VOL push
reporting identifies loads by *physical address* (master/enclosure/
module/load). The controller offers no query to translate between the
two, so the coordinator learns the mapping by observation:

- When Home Assistant itself sets a level, the write is remembered; a
  matching LO push that follows pins the physical address to that
  contractor number immediately.
- When a keypad press changes loads, the LO pushes are remembered and
  the debounced sweep that follows diffs levels; an unambiguous single
  match becomes a mapping candidate, confirmed after two observations.

Learned mappings persist in .storage and make subsequent pushes update
their entity instantly, with no sweep at all.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
import logging
import time
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers.debounce import Debouncer
from homeassistant.helpers.storage import Store
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import DOMAIN
from .hub import QLinkConnectionError, QLinkError, QLinkHub

_LOGGER = logging.getLogger(__name__)

STORAGE_VERSION = 1
PUSH_DEBOUNCE_SECONDS = 2.0
OBSERVATION_TTL = 120.0  # seconds an LO observation stays eligible
WRITE_MATCH_WINDOW = 5.0  # seconds an HA write can claim an LO push
CONFIRMATIONS_REQUIRED = 2


@dataclass
class _Observation:
    """An LO push we could not yet attribute to a contractor number."""

    phys_key: str
    level: int
    ts: float = field(default_factory=time.monotonic)


class QLinkCoordinator(DataUpdateCoordinator[dict[int | str, int]]):
    """Sweeps configured contractor loads and folds in push updates."""

    def __init__(
        self,
        hass: HomeAssistant,
        hub: QLinkHub,
        entry_id: str,
        loads: list[int | str],
        scan_interval: int,
    ) -> None:
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN} loads",
            update_interval=timedelta(seconds=scan_interval),
        )
        self.hub = hub
        self.loads = loads
        self._store: Store[dict[str, Any]] = Store(
            hass, STORAGE_VERSION, f"{DOMAIN}.{entry_id}.load_map"
        )
        self.load_map: dict[str, int] = {}  # "m-e-mod-l" -> contractor number
        # Levels written (optimistic or push) since they were last confirmed
        # by a poll read: load id -> (level, monotonic ts). Used to keep a
        # slow sweep from clobbering fresher state it started before.
        self._fresh: dict[int | str, tuple[int, float]] = {}
        self._candidates: dict[str, dict[int, int]] = {}
        self._observations: list[_Observation] = []
        self._recent_writes: list[tuple[int, int, float]] = []  # (con, level, ts)
        self._push_debouncer = Debouncer(
            hass,
            _LOGGER,
            cooldown=PUSH_DEBOUNCE_SECONDS,
            immediate=False,
            function=self.async_request_refresh,
        )

    async def async_load_map(self) -> None:
        stored = await self._store.async_load()
        if stored and isinstance(stored.get("map"), dict):
            self.load_map = {
                str(k): int(v) for k, v in stored["map"].items() if str(v).isdigit()
            }
            _LOGGER.debug("Loaded %d learned load mappings", len(self.load_map))

    async def _save_map(self) -> None:
        await self._store.async_save({"map": self.load_map})

    # ------------------------------------------------------------ polling

    async def _async_update_data(self) -> dict[int, int]:
        old = dict(self.data) if self.data else {}
        levels: dict[int | str, int] = {}
        read_ts: dict[int | str, float] = {}
        failures = 0
        for con in self.loads:
            try:
                levels[con] = await self.hub.get_load_level(con, low_priority=True)
                read_ts[con] = time.monotonic()
            except QLinkConnectionError as err:
                raise UpdateFailed(f"Controller connection lost: {err}") from err
            except QLinkError as err:
                # One bad load (error 257, timeout) must not fail the whole
                # sweep; keep its last known level and move on.
                failures += 1
                if con in old:
                    levels[con] = old[con]
                _LOGGER.debug("Poll failed for load %s: %s", con, err)
        if failures and failures >= len(self.loads):
            raise UpdateFailed("Every load poll failed this sweep")

        # A sweep takes many seconds; never let a level it read early
        # overwrite a write that landed later (dashboard tap mid-sweep).
        for con, (lvl, ts) in list(self._fresh.items()):
            if ts > read_ts.get(con, 0.0):
                levels[con] = lvl
            else:
                del self._fresh[con]  # poll read after the write; confirmed

        if old:
            self._reconcile_observations(old, levels)
        return levels

    # -------------------------------------------------------- push intake

    def note_write(self, contractor_number: int | str, level: int) -> None:
        """Record an HA-initiated level write (for mapping attribution)."""
        now = time.monotonic()
        self._recent_writes = [
            w for w in self._recent_writes if now - w[2] < WRITE_MATCH_WINDOW
        ]
        self._recent_writes.append((contractor_number, level, now))

    def handle_load_push(self, phys_key: str, level: int) -> None:
        """Handle an LO/LS push: instant update if mapped, else learn."""
        con = self.load_map.get(phys_key)
        if con is not None:
            self.apply_level(con, level)
            return

        # Attribute to a recent HA write when unambiguous — but only as a
        # mapping *candidate*: a coincidental keypad press at the same
        # level within the window must not create a permanent wrong map.
        now = time.monotonic()
        matches = [
            w
            for w in self._recent_writes
            if w[1] == level and now - w[2] < WRITE_MATCH_WINDOW
        ]
        if len(matches) == 1:
            cands = self._candidates.setdefault(phys_key, {})
            cands[matches[0][0]] = cands.get(matches[0][0], 0) + 1
            if cands[matches[0][0]] >= CONFIRMATIONS_REQUIRED:
                self._learn(phys_key, matches[0][0], source="write-echo")
                self.apply_level(matches[0][0], level)
                return

        self._observations = [
            o for o in self._observations if now - o.ts < OBSERVATION_TTL
        ]
        self._observations.append(_Observation(phys_key, level))
        self.hass.async_create_task(self._push_debouncer.async_call())

    def apply_level(self, contractor_number: int | str, level: int) -> None:
        """Fold a known level into coordinator data and notify entities."""
        if contractor_number not in self.loads:
            return
        self._fresh[contractor_number] = (level, time.monotonic())
        data = dict(self.data) if self.data else {}
        data[contractor_number] = level
        self.async_set_updated_data(data)

    # ----------------------------------------------------------- learning

    def _reconcile_observations(
        self, old: dict[int, int], new: dict[int, int]
    ) -> None:
        if not self._observations:
            return
        now = time.monotonic()
        remaining: list[_Observation] = []
        for obs in self._observations:
            if now - obs.ts > OBSERVATION_TTL or obs.phys_key in self.load_map:
                continue
            changed_to = [
                con
                for con in self.loads
                if new.get(con) == obs.level and old.get(con) != new.get(con)
            ]
            if len(changed_to) == 1:
                cands = self._candidates.setdefault(obs.phys_key, {})
                cands[changed_to[0]] = cands.get(changed_to[0], 0) + 1
                if cands[changed_to[0]] >= CONFIRMATIONS_REQUIRED:
                    self._learn(obs.phys_key, changed_to[0], source="sweep-diff")
                continue  # observation consumed either way
            if not changed_to:
                continue  # stale or untracked load; drop
            remaining.append(obs)  # ambiguous this round; keep briefly
        self._observations = remaining

    def _learn(self, phys_key: str, contractor_number: int, *, source: str) -> None:
        self.load_map[phys_key] = contractor_number
        self._candidates.pop(phys_key, None)
        _LOGGER.info(
            "Learned load mapping %s -> contractor %s (%s)",
            phys_key,
            contractor_number,
            source,
        )
        self.hass.async_create_task(self._save_map())

    async def async_shutdown(self) -> None:
        self._push_debouncer.async_cancel()
        await super().async_shutdown()
