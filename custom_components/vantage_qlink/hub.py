"""Asyncio protocol engine for a Vantage QLink controller.

Design notes, learned against real hardware (Q-series master behind a
QLink IP Enabler on TCP 10001):

- The IP Enabler accepts exactly ONE TCP client. This hub owns that
  connection for the lifetime of the config entry.
- Replies are CR-terminated (no LF). The reader is delimiter-agnostic.
- Commands are sent with the ``#`` detail suffix wherever supported, so
  replies echo the command and its arguments (``VGL# 1005`` ->
  ``RGL 1005 100``). That makes request/response matching robust even
  when unsolicited push lines (VOS/VOL/VOD reporting) are interleaved.
- Only one command is in flight at a time, with a configurable send gap,
  mirroring how the QLink serial bridge actually behaves.
"""

from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
import logging
import time
from typing import Any

_LOGGER = logging.getLogger(__name__)

# First tokens of unsolicited reporting lines (VOS / VOL / VOD).
PUSH_TOKENS = {"SW", "IR", "LO", "LS", "LV", "LE", "LC", "AON", "AOFF"}

RECONNECT_DELAYS = (5, 10, 20, 40, 60)


class QLinkError(Exception):
    """Base error for QLink communication."""


class QLinkConnectionError(QLinkError):
    """Raised when the connection is down or fails."""


class QLinkTimeoutError(QLinkError):
    """Raised when the controller does not answer a command in time."""


@dataclass
class _Request:
    """A queued command awaiting its reply."""

    line: str
    future: asyncio.Future[list[str]]
    prefixes: tuple[str, ...]  # acceptable reply first-tokens, e.g. ("RGL",)
    accept_bare: bool  # accept a non-push, non-prefixed line as the reply
    timeout: float
    multiline_count_index: int | None = None  # arg index holding line count
    lines: list[str] = field(default_factory=list)
    expected_lines: int = 1


class QLinkHub:
    """Owns the single TCP connection to the QLink controller."""

    def __init__(
        self,
        host: str,
        port: int,
        *,
        send_gap: float = 0.12,
        command_timeout: float = 4.0,
        enable_vos: bool = True,
        enable_vol: bool = True,
    ) -> None:
        self.host = host
        self.port = port
        self.send_gap = send_gap
        self.command_timeout = command_timeout
        self.enable_vos = enable_vos
        self.enable_vol = enable_vol

        self._reader: asyncio.StreamReader | None = None
        self._writer: asyncio.StreamWriter | None = None
        self._reader_task: asyncio.Task | None = None
        self._sender_task: asyncio.Task | None = None
        self._reconnect_task: asyncio.Task | None = None
        self._queue: asyncio.Queue[_Request] = asyncio.Queue()
        self._in_flight: _Request | None = None
        self._last_send = 0.0
        self._closing = False
        self._connected = False

        self._push_callbacks: list[Callable[[str, list[str]], None]] = []
        self._connection_callbacks: list[Callable[[bool], None]] = []

        # Rolling log of recent traffic for diagnostics. Sized so pushes
        # survive a full poll sweep (2 lines per polled load).
        self.recent_lines: deque[str] = deque(maxlen=400)

    # ------------------------------------------------------------- state

    @property
    def connected(self) -> bool:
        return self._connected

    def add_push_callback(self, cb: Callable[[str, list[str]], None]) -> None:
        self._push_callbacks.append(cb)

    def add_connection_callback(self, cb: Callable[[bool], None]) -> None:
        self._connection_callbacks.append(cb)

    def _set_connected(self, value: bool) -> None:
        if self._connected == value:
            return
        self._connected = value
        for cb in self._connection_callbacks:
            try:
                cb(value)
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Connection callback failed")

    # ------------------------------------------------------- connection

    async def async_connect(self) -> None:
        """Open the connection and start the worker tasks."""
        self._closing = False
        await self._open()
        if self._reader_task is None or self._reader_task.done():
            self._reader_task = asyncio.create_task(self._read_loop())
        if self._sender_task is None or self._sender_task.done():
            self._sender_task = asyncio.create_task(self._send_loop())
        await self._configure_reporting()

    async def _open(self) -> None:
        try:
            self._reader, self._writer = await asyncio.wait_for(
                asyncio.open_connection(self.host, self.port), timeout=10
            )
        except (OSError, asyncio.TimeoutError) as err:
            raise QLinkConnectionError(
                f"Cannot connect to {self.host}:{self.port}: {err}"
            ) from err
        self._set_connected(True)
        _LOGGER.info("Connected to Vantage QLink at %s:%s", self.host, self.port)

    async def _configure_reporting(self) -> None:
        """Enable push reporting (persistent on the controller's port)."""
        try:
            if self.enable_vos:
                await self.command("VOS", 0, 1, prefixes=("ROS",), accept_bare=True)
            if self.enable_vol:
                await self.command("VOL", 1, prefixes=("ROL",), accept_bare=True)
        except QLinkError as err:
            _LOGGER.warning("Could not configure push reporting: %s", err)

    async def async_set_push_reporting(self, switches: bool, loads: bool) -> None:
        """Explicitly enable/disable VOS and VOL reporting."""
        await self.command(
            "VOS", 0, 1 if switches else 0, prefixes=("ROS",), accept_bare=True
        )
        await self.command(
            "VOL", 1 if loads else 0, prefixes=("ROL",), accept_bare=True
        )
        self.enable_vos = switches
        self.enable_vol = loads

    async def async_disconnect(self) -> None:
        """Close the connection and stop the worker tasks."""
        self._closing = True
        tasks = [
            t
            for t in (self._reader_task, self._sender_task, self._reconnect_task)
            if t is not None
        ]
        self._reader_task = self._sender_task = self._reconnect_task = None
        for task in tasks:
            task.cancel()
        for task in tasks:
            try:
                await task
            except (asyncio.CancelledError, Exception):  # noqa: BLE001
                pass
        self._teardown_socket()
        self._fail_pending(QLinkConnectionError("Shutting down"))
        self._set_connected(False)

    def _teardown_socket(self) -> None:
        if self._writer is not None:
            try:
                self._writer.close()
            except Exception:  # noqa: BLE001
                pass
        self._reader = None
        self._writer = None

    def _fail_pending(self, err: Exception) -> None:
        if self._in_flight is not None and not self._in_flight.future.done():
            self._in_flight.future.set_exception(err)
        self._in_flight = None
        while not self._queue.empty():
            try:
                req = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            if not req.future.done():
                req.future.set_exception(err)

    def _schedule_reconnect(self) -> None:
        if self._closing or (
            self._reconnect_task is not None and not self._reconnect_task.done()
        ):
            return
        self._reconnect_task = asyncio.create_task(self._reconnect_loop())

    async def _reconnect_loop(self) -> None:
        attempt = 0
        while not self._closing:
            delay = RECONNECT_DELAYS[min(attempt, len(RECONNECT_DELAYS) - 1)]
            _LOGGER.info("Reconnecting to %s:%s in %ss", self.host, self.port, delay)
            await asyncio.sleep(delay)
            try:
                await self._open()
                await self._configure_reporting()
                return
            except QLinkError as err:
                _LOGGER.debug("Reconnect attempt failed: %s", err)
                attempt += 1

    # ---------------------------------------------------------- send path

    async def command(
        self,
        cmd: str,
        *args: Any,
        prefixes: tuple[str, ...] | None = None,
        accept_bare: bool = False,
        detailed: bool = True,
        timeout: float | None = None,
        multiline_count_index: int | None = None,
    ) -> list[str]:
        """Send a command and return its reply line(s), tokenized joined.

        ``prefixes`` defaults to the conventional reply prefix: the command
        word with its leading V replaced by R (VGL -> RGL).
        """
        word = cmd.strip().upper()
        if prefixes is None:
            prefixes = (f"R{word[1:]}",) if word.startswith("V") else ()
            if not prefixes:
                accept_bare = True
        line = word + ("#" if detailed else "")
        if args:
            line += " " + " ".join(str(a) for a in args)

        loop = asyncio.get_running_loop()
        req = _Request(
            line=line,
            future=loop.create_future(),
            prefixes=prefixes,
            accept_bare=accept_bare,
            timeout=timeout or self.command_timeout,
            multiline_count_index=multiline_count_index,
        )
        await self._queue.put(req)
        return await req.future

    async def _send_loop(self) -> None:
        while True:
            req = await self._queue.get()
            if req.future.done():
                continue
            if self._writer is None or not self._connected:
                req.future.set_exception(QLinkConnectionError("Not connected"))
                continue

            # Enforce the on-wire send gap.
            gap = self.send_gap - (time.monotonic() - self._last_send)
            if gap > 0:
                await asyncio.sleep(gap)

            self._in_flight = req
            try:
                self._writer.write((req.line + "\r\n").encode())
                await self._writer.drain()
                self._last_send = time.monotonic()
                self.recent_lines.append(f"TX {req.line}")
            except OSError as err:
                self._in_flight = None
                if not req.future.done():
                    req.future.set_exception(
                        QLinkConnectionError(f"Write failed: {err}")
                    )
                self._on_connection_lost()
                continue

            try:
                await asyncio.wait_for(
                    asyncio.shield(req.future), timeout=req.timeout
                )
            except asyncio.TimeoutError:
                if not req.future.done():
                    req.future.set_exception(
                        QLinkTimeoutError(f"No reply to '{req.line}'")
                    )
            except Exception:  # noqa: BLE001
                # The future's consumer sees the original exception.
                pass
            finally:
                self._in_flight = None

    # ---------------------------------------------------------- read path

    async def _read_loop(self) -> None:
        buffer = ""
        while True:
            if self._reader is None:
                await asyncio.sleep(0.5)
                continue
            try:
                chunk = await self._reader.read(4096)
            except (OSError, asyncio.IncompleteReadError):
                chunk = b""
            except asyncio.CancelledError:
                raise
            if not chunk:
                if self._closing:
                    return
                self._on_connection_lost()
                await asyncio.sleep(1)
                continue
            buffer += chunk.decode(errors="replace")
            while True:
                idx = min(
                    (i for i in (buffer.find("\r"), buffer.find("\n")) if i >= 0),
                    default=-1,
                )
                if idx < 0:
                    break
                line, buffer = buffer[:idx], buffer[idx + 1 :]
                if buffer.startswith("\n"):
                    buffer = buffer[1:]
                line = line.strip()
                if line:
                    self._handle_line(line)

    def _on_connection_lost(self) -> None:
        if not self._connected:
            return
        _LOGGER.warning("Connection to %s:%s lost", self.host, self.port)
        self._teardown_socket()
        self._fail_pending(QLinkConnectionError("Connection lost"))
        self._set_connected(False)
        self._schedule_reconnect()

    def _handle_line(self, line: str) -> None:
        self.recent_lines.append(f"RX {line}")
        tokens = line.split()
        first = tokens[0].rstrip("#").upper() if tokens else ""

        # 1) Unsolicited push reporting.
        if first in PUSH_TOKENS:
            for cb in self._push_callbacks:
                try:
                    cb(first, tokens[1:])
                except Exception:  # noqa: BLE001
                    _LOGGER.exception("Push callback failed")
            return

        # 2) Reply to the in-flight command.
        req = self._in_flight
        if req is not None and not req.future.done():
            if req.prefixes and first in {p.upper() for p in req.prefixes}:
                self._feed_reply(req, tokens)
                return
            if req.accept_bare:
                self._feed_reply(req, tokens)
                return

        _LOGGER.debug("Unmatched line from controller: %s", line)

    def _feed_reply(self, req: _Request, tokens: list[str]) -> None:
        req.lines.append(" ".join(tokens))

        if req.multiline_count_index is not None and len(req.lines) == 1:
            # First line carries the number of detail lines that follow,
            # e.g. "RQP 1 6" -> six more RQP lines.
            try:
                count = int(tokens[req.multiline_count_index])
            except (IndexError, ValueError):
                count = 0
            req.expected_lines = 1 + max(count, 0)

        if len(req.lines) >= req.expected_lines and not req.future.done():
            req.future.set_result(req.lines)

    # ------------------------------------------------------- convenience

    @staticmethod
    def _tail_int(lines: list[str], *, drop_prefix_args: int) -> int:
        """Parse the last token of a single-line reply as an int level."""
        tokens = lines[0].split()
        if not tokens:
            raise QLinkError("Empty reply")
        try:
            return int(round(float(tokens[-1])))
        except ValueError as err:
            raise QLinkError(f"Unparseable reply: {lines[0]}") from err

    @staticmethod
    def _load_args(load_id: int | str) -> tuple[Any, ...]:
        """A load id is a contractor number or a dash-form station address.

        ``"2-33-8"`` becomes three positional args (master station load),
        matching how 0.0.x addressed station-bus loads.
        """
        if isinstance(load_id, str) and "-" in load_id:
            return tuple(load_id.split("-"))
        return (load_id,)

    async def get_load_level(self, load_id: int | str) -> int:
        """VGL# <n> -> RGL <n> <level> (bare <level> tolerated).

        Clamped to 0-100: some station-bus loads (LVRS relays) report raw
        values above 100 for "on".
        """
        lines = await self.command(
            "VGL", *self._load_args(load_id), prefixes=("RGL",), accept_bare=True
        )
        return max(0, min(100, self._tail_int(lines, drop_prefix_args=2)))

    async def set_load_level(
        self, load_id: int | str, level: int, fade: float = 0.0
    ) -> None:
        """VLO# <n> <level> <fade> -> RLO <n> <level> <fade>."""
        level = max(0, min(100, int(level)))
        args: tuple[Any, ...] = (*self._load_args(load_id), level)
        if fade:
            args += (round(float(fade), 1),)
        await self.command("VLO", *args, prefixes=("RLO",), accept_bare=True)

    async def get_switch_state(self, master: int, station: int, switch: int) -> int:
        """VGS# -> RGS <m> <s> <sw> <state>; 0 off, 1 on, 2 unprogrammed."""
        lines = await self.command(
            "VGS", master, station, switch, prefixes=("RGS",), accept_bare=True
        )
        return self._tail_int(lines, drop_prefix_args=4)

    async def get_station_switches(self, master: int, station: int) -> list[int]:
        """VGT# -> RGT <m> <s> <state-1..10>."""
        lines = await self.command(
            "VGT", master, station, prefixes=("RGT",), accept_bare=True
        )
        tokens = lines[0].split()
        states = tokens[-10:] if len(tokens) >= 10 else []
        out = []
        for tok in states:
            try:
                out.append(int(tok))
            except ValueError:
                out.append(2)
        return out

    async def execute_switch(
        self, master: int, station: int, switch: int, state: int
    ) -> None:
        """VSW# <m> <s> <sw> <state> — run a Vantage switch function."""
        await self.command(
            "VSW", master, station, switch, state, prefixes=("RSW",), accept_bare=True
        )

    async def set_led(self, master: int, station: int, led: int, state: int) -> None:
        """VLD# <m> <s> <led> <state> — 0 off, 1 on, 2 blink."""
        await self.command(
            "VLD", master, station, led, state, prefixes=("RLD",), accept_bare=True
        )

    async def execute_time_function(
        self, master: int, function: int, state: int
    ) -> None:
        """VET# <m> <function> <state> — run a controller time function."""
        await self.command(
            "VET", master, function, state, prefixes=("RET",), accept_bare=True
        )

    async def get_time_function(self, master: int, function: int) -> str:
        """VQT# <m> <function> -> schedule parameters."""
        lines = await self.command(
            "VQT", master, function, prefixes=("RQT",), accept_bare=True
        )
        return lines[0]

    async def probe(self) -> str:
        """VQA# — cheap connectivity check; returns master/port info."""
        lines = await self.command(
            "VQA", prefixes=("RQA", "RQM"), accept_bare=True, timeout=5
        )
        return lines[0]

    async def query_masters(self) -> list[int]:
        """VQM# -> RQM <count> <m1> <m2> ..."""
        lines = await self.command("VQM", prefixes=("RQM",), accept_bare=True)
        tokens = lines[0].split()
        nums = [t for t in tokens if t.isdigit()]
        if not nums:
            return []
        count = int(nums[0])
        return [int(t) for t in nums[1 : 1 + count]]

    async def query_modules(self, master: int) -> list[dict[str, Any]]:
        """VQP# <m> — multi-line: header with count, then one line/module."""
        lines = await self.command(
            "VQP",
            master,
            prefixes=("RQP",),
            accept_bare=True,
            multiline_count_index=2,
            timeout=10,
        )
        modules = []
        for line in lines[1:]:
            tokens = [t for t in line.split() if t not in ("RQP", "RQP#")]
            if len(tokens) >= 5:
                modules.append(
                    {
                        "master": int(tokens[0]),
                        "enclosure": int(tokens[1]),
                        "module": int(tokens[2]),
                        "type": int(tokens[3]),
                        "version": tokens[4],
                    }
                )
        return modules

    async def query_stations(self, master: int) -> list[dict[str, Any]]:
        """VQS# <m> — multi-line: header with count, then one line/station."""
        lines = await self.command(
            "VQS",
            master,
            prefixes=("RQS",),
            accept_bare=True,
            multiline_count_index=2,
            timeout=15,
        )
        stations = []
        for line in lines[1:]:
            tokens = [t for t in line.split() if t not in ("RQS", "RQS#")]
            if len(tokens) >= 7:
                stations.append(
                    {
                        "master": int(tokens[0]),
                        "station": int(tokens[1]),
                        "type": int(tokens[2]),
                        "cfg": tokens[3],
                        "version": tokens[4],
                        "sixbit": tokens[5],
                        "serial": tokens[6],
                    }
                )
        return stations

    async def get_name(self, master: int, address: int, pos: int) -> str:
        """VGN# <m> <address> <pos> -> 'Name|Room|Floor|Type'."""
        lines = await self.command(
            "VGN", master, address, pos, prefixes=("RGN",), accept_bare=True
        )
        line = lines[0]
        if line.split()[0].rstrip("#").upper() == "RGN":
            tokens = line.split(None, 4)
            return tokens[4] if len(tokens) >= 5 else ""
        return line  # regular-format reply is the bare name string

    async def raw(self, command_line: str, timeout: float | None = None) -> list[str]:
        """Send a raw command line; returns whatever single reply arrives."""
        word = command_line.strip().split()[0].rstrip("#$").upper()
        prefix = f"R{word[1:]}" if word.startswith("V") and len(word) > 1 else ""
        loop = asyncio.get_running_loop()
        req = _Request(
            line=command_line.strip(),
            future=loop.create_future(),
            prefixes=(prefix,) if prefix else (),
            accept_bare=True,
            timeout=timeout or self.command_timeout,
        )
        await self._queue.put(req)
        return await req.future
