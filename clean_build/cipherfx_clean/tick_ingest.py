"""Read-only WebSocket ingress for the MT5 bridge tick stream.

This module owns only local transport and raw-tick persistence.  It accepts
the bridge's ``/mt5/ticks`` WebSocket feed, validates each event, and records
the received bid/ask quote.  It has no broker adapter and no decision or order
interface.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from hashlib import sha1
import json
from math import isfinite
from pathlib import Path
import signal
from typing import Mapping, Sequence

from .contracts import RawTick
from .store import EvidenceStore


DEFAULT_SYMBOLS = ("XAUUSD", "UK100", "USA100", "USA500", "USA30")
DEFAULT_PATH = "/mt5/ticks"
WEBSOCKET_ACCEPT_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"


@dataclass(frozen=True)
class TickIngressMetrics:
    started_at: str
    connected_clients: int
    received_events: int
    persisted_ticks: int
    duplicate_ticks: int
    rejected_events: int
    ignored_events: int
    heartbeats: int
    last_event_at: str | None
    last_tick_at: Mapping[str, str]
    last_error: str | None
    mode: str = "READ_ONLY_WEBSOCKET_TICK_INGRESS"


class TickEventError(ValueError):
    """Raised when a bridge event cannot become a factual quote."""


class WebSocketTickIngress:
    """A localhost-only append-only MT5 tick receiver."""

    def __init__(
        self,
        *,
        database: str | Path,
        symbols: Sequence[str] = DEFAULT_SYMBOLS,
        host: str = "127.0.0.1",
        port: int = 8765,
        path: str = DEFAULT_PATH,
        status_path: str | Path | None = None,
        batch_size: int = 256,
        batch_wait_seconds: float = 0.1,
    ):
        if host != "127.0.0.1":
            raise ValueError("tick ingress must bind to localhost only")
        if not 0 <= int(port) <= 65535:
            raise ValueError("port must be in range 0..65535")
        if not path.startswith("/"):
            raise ValueError("WebSocket path must begin with '/'")
        if not symbols or any(not str(symbol).strip() for symbol in symbols):
            raise ValueError("at least one non-empty symbol is required")
        if batch_size <= 0 or batch_wait_seconds <= 0:
            raise ValueError("batch settings must be positive")
        self._database = Path(database)
        self._symbols = frozenset(str(symbol).upper() for symbol in symbols)
        self._host = host
        self._port = int(port)
        self._path = path
        self._status_path = Path(status_path) if status_path else None
        self._batch_size = int(batch_size)
        self._batch_wait_seconds = float(batch_wait_seconds)
        self._store: EvidenceStore | None = None
        self._queue: asyncio.Queue[RawTick] = asyncio.Queue(maxsize=16384)
        self._stop: asyncio.Event | None = None
        self._server = None
        self._writer_task: asyncio.Task[None] | None = None
        self._started_at = ""
        self._connected_clients = 0
        self._received_events = 0
        self._persisted_ticks = 0
        self._duplicate_ticks = 0
        self._rejected_events = 0
        self._ignored_events = 0
        self._heartbeats = 0
        self._last_event_at: str | None = None
        self._last_tick_at: dict[str, str] = {}
        self._last_error: str | None = None

    @property
    def port(self) -> int:
        """The bound port, including an OS-assigned port when configured as 0."""

        return self._port

    def metrics(self) -> TickIngressMetrics:
        return TickIngressMetrics(
            started_at=self._started_at,
            connected_clients=self._connected_clients,
            received_events=self._received_events,
            persisted_ticks=self._persisted_ticks,
            duplicate_ticks=self._duplicate_ticks,
            rejected_events=self._rejected_events,
            ignored_events=self._ignored_events,
            heartbeats=self._heartbeats,
            last_event_at=self._last_event_at,
            last_tick_at=dict(sorted(self._last_tick_at.items())),
            last_error=self._last_error,
        )

    async def serve_forever(self, stop: asyncio.Event | None = None) -> None:
        """Run until ``stop`` is set; this method never performs broker I/O."""

        if self._store is not None:
            raise RuntimeError("tick ingress is already running")
        self._database.parent.mkdir(parents=True, exist_ok=True)
        self._store = EvidenceStore(self._database)
        self._stop = stop or asyncio.Event()
        self._started_at = _utc_now().isoformat()
        self._writer_task = asyncio.create_task(self._writer_loop())
        try:
            self._server = await asyncio.start_server(
                self._handle_connection,
                self._host,
                self._port,
                limit=65536,
            )
            async with self._server as server:
                sockets = tuple(server.sockets or ())
                if not sockets:
                    raise RuntimeError("WebSocket server did not bind a socket")
                self._port = int(sockets[0].getsockname()[1])
                self._write_status()
                await self._stop.wait()
        finally:
            await self._drain_writer()
            self._write_status()
            if self._store is not None:
                self._store.checkpoint()
                self._store.close()
            self._store = None
            self._server = None

    async def stop(self) -> None:
        if self._stop is not None:
            self._stop.set()

    async def _handle_connection(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        try:
            await self._complete_websocket_handshake(reader, writer)
        except (asyncio.IncompleteReadError, TickEventError, ValueError) as exc:
            self._rejected_events += 1
            self._last_error = f"WEBSOCKET_HANDSHAKE_REJECTED:{type(exc).__name__}:{exc}"
            self._write_status()
            writer.close()
            await writer.wait_closed()
            return
        self._connected_clients += 1
        self._write_status()
        try:
            while True:
                message = await _read_websocket_text_frame(reader, writer)
                if message is None:
                    break
                self._received_events += 1
                self._last_event_at = _utc_now().isoformat()
                try:
                    payload = _json_mapping(message)
                    if payload.get("type") == "heartbeat":
                        self._heartbeats += 1
                    elif payload.get("type") == "tick":
                        event_symbol = _event_symbol(payload)
                        if not event_symbol:
                            raise TickEventError("UNCONFIGURED_SYMBOL:EMPTY")
                        if event_symbol not in self._symbols:
                            self._ignored_events += 1
                        else:
                            await self._queue.put(_tick_from_payload(payload, self._symbols))
                    else:
                        raise TickEventError("UNSUPPORTED_EVENT_TYPE")
                except (TickEventError, TypeError, ValueError) as exc:
                    self._rejected_events += 1
                    self._last_error = f"{type(exc).__name__}:{exc}"
                self._write_status()
        except asyncio.IncompleteReadError as exc:
            if exc.partial:
                self._rejected_events += 1
                self._last_error = f"WEBSOCKET_FRAME_REJECTED:{type(exc).__name__}:{exc}"
                self._write_status()
        except (TickEventError, ValueError) as exc:
            self._rejected_events += 1
            self._last_error = f"WEBSOCKET_FRAME_REJECTED:{type(exc).__name__}:{exc}"
            self._write_status()
        finally:
            self._connected_clients = max(0, self._connected_clients - 1)
            self._write_status()
            writer.close()
            try:
                await writer.wait_closed()
            except ConnectionError:
                pass

    async def _complete_websocket_handshake(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
    ) -> None:
        """Accept a local bridge handshake, including MT5's legacy key shape.

        MT5's existing WS2_32 bridge sends a non-standard-length key but otherwise
        uses masked RFC 6455 text frames.  The key is used only to derive the
        acknowledgement; localhost binding and strict request/frame validation
        remain the security boundary.
        """

        request = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=5.0)
        if len(request) > 65536:
            raise TickEventError("HANDSHAKE_TOO_LARGE")
        try:
            text = request.decode("ascii")
        except UnicodeDecodeError as exc:
            raise TickEventError("HANDSHAKE_NOT_ASCII") from exc
        lines = text.split("\r\n")
        if not lines or lines[0] != f"GET {self._path} HTTP/1.1":
            raise TickEventError("UNEXPECTED_WEBSOCKET_PATH")
        headers: dict[str, str] = {}
        for line in lines[1:]:
            if not line:
                continue
            name, separator, value = line.partition(":")
            if not separator:
                raise TickEventError("MALFORMED_HANDSHAKE_HEADER")
            headers[name.strip().lower()] = value.strip()
        if headers.get("upgrade", "").lower() != "websocket":
            raise TickEventError("UPGRADE_REQUIRED")
        if "upgrade" not in headers.get("connection", "").lower():
            raise TickEventError("CONNECTION_UPGRADE_REQUIRED")
        key = headers.get("sec-websocket-key", "")
        if not key or any(character.isspace() for character in key):
            raise TickEventError("WEBSOCKET_KEY_REQUIRED")
        try:
            key.encode("ascii")
        except UnicodeEncodeError as exc:
            raise TickEventError("WEBSOCKET_KEY_NOT_ASCII") from exc
        accept = base64.b64encode(
            sha1((key + WEBSOCKET_ACCEPT_GUID).encode("ascii")).digest()
        ).decode("ascii")
        response = (
            "HTTP/1.1 101 Switching Protocols\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
        )
        writer.write(response.encode("ascii"))
        await writer.drain()

    async def _writer_loop(self) -> None:
        while True:
            first = await self._queue.get()
            batch = [first]
            try:
                while len(batch) < self._batch_size:
                    try:
                        batch.append(
                            await asyncio.wait_for(
                                self._queue.get(),
                                timeout=self._batch_wait_seconds,
                            )
                        )
                    except asyncio.TimeoutError:
                        break
                self._write_batch(batch)
            finally:
                for _ in batch:
                    self._queue.task_done()

    async def _drain_writer(self) -> None:
        if self._writer_task is None:
            return
        await self._queue.join()
        self._writer_task.cancel()
        try:
            await self._writer_task
        except asyncio.CancelledError:
            pass
        self._writer_task = None

    def _write_batch(self, ticks: Sequence[RawTick]) -> None:
        if self._store is None:
            raise RuntimeError("tick ingress store is unavailable")
        try:
            inserted = self._store.write_ticks(tuple(ticks))
        except Exception as exc:
            self._last_error = f"TICK_PERSISTENCE_FAILED:{type(exc).__name__}:{exc}"
            self._rejected_events += len(ticks)
            self._write_status()
            return
        self._persisted_ticks += inserted
        self._duplicate_ticks += len(ticks) - inserted
        for tick in ticks:
            self._last_tick_at[tick.symbol] = tick.timestamp.isoformat()
        self._write_status()

    def _write_status(self) -> None:
        if self._status_path is None:
            return
        self._status_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = self._status_path.with_suffix(self._status_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(asdict(self.metrics()), indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(self._status_path)


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _json_mapping(message: str | bytes) -> Mapping[str, object]:
    if isinstance(message, bytes):
        message = message.decode("utf-8")
    payload = json.loads(message)
    if not isinstance(payload, Mapping):
        raise TickEventError("EVENT_MUST_BE_A_JSON_OBJECT")
    return payload


async def _read_websocket_text_frame(
    reader: asyncio.StreamReader,
    writer: asyncio.StreamWriter,
) -> str | None:
    """Read one masked client frame and return text, or ``None`` on close."""

    prefix = await reader.readexactly(2)
    first, second = prefix[0], prefix[1]
    final = bool(first & 0x80)
    opcode = first & 0x0F
    masked = bool(second & 0x80)
    payload_length = second & 0x7F
    if not final:
        raise TickEventError("FRAGMENTED_FRAME_UNSUPPORTED")
    if not masked:
        raise TickEventError("CLIENT_FRAME_MUST_BE_MASKED")
    if payload_length == 126:
        payload_length = int.from_bytes(await reader.readexactly(2), "big")
    elif payload_length == 127:
        payload_length = int.from_bytes(await reader.readexactly(8), "big")
    if payload_length > 65536:
        raise TickEventError("FRAME_TOO_LARGE")
    mask = await reader.readexactly(4)
    payload = bytearray(await reader.readexactly(payload_length))
    for index in range(payload_length):
        payload[index] ^= mask[index % 4]
    if opcode == 0x8:
        return None
    if opcode == 0x9:
        if payload_length > 125:
            raise TickEventError("PING_FRAME_TOO_LARGE")
        writer.write(bytes((0x8A, payload_length)) + bytes(payload))
        await writer.drain()
        return await _read_websocket_text_frame(reader, writer)
    if opcode != 0x1:
        raise TickEventError("UNSUPPORTED_WEBSOCKET_OPCODE")
    try:
        return payload.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise TickEventError("FRAME_NOT_UTF8") from exc


def _tick_from_payload(payload: Mapping[str, object], allowed_symbols: frozenset[str]) -> RawTick:
    symbol = _event_symbol(payload)
    if symbol not in allowed_symbols:
        raise TickEventError(f"UNCONFIGURED_SYMBOL:{symbol or 'EMPTY'}")
    bid = _positive_float(payload, "bid")
    ask = _positive_float(payload, "ask")
    if ask < bid:
        raise TickEventError("ASK_BELOW_BID")
    timestamp = _utc_tick_timestamp(payload)
    return RawTick(symbol, timestamp, bid, ask)


def _event_symbol(payload: Mapping[str, object]) -> str:
    return str(payload.get("symbol") or "").upper()


def _positive_float(payload: Mapping[str, object], field: str) -> float:
    try:
        value = float(payload[field])
    except (KeyError, TypeError, ValueError) as exc:
        raise TickEventError(f"INVALID_{field.upper()}") from exc
    if not isfinite(value) or value <= 0:
        raise TickEventError(f"INVALID_{field.upper()}")
    return value


def _utc_tick_timestamp(payload: Mapping[str, object]) -> datetime:
    try:
        utc_seconds = int(float(payload["time_utc"]))
    except (KeyError, TypeError, ValueError) as exc:
        raise TickEventError("INVALID_TIME_UTC") from exc
    if utc_seconds <= 0:
        raise TickEventError("INVALID_TIME_UTC")
    raw_milliseconds = payload.get("time_msc")
    if raw_milliseconds is None:
        return datetime.fromtimestamp(utc_seconds, timezone.utc)
    try:
        broker_milliseconds = int(float(raw_milliseconds))
    except (TypeError, ValueError) as exc:
        raise TickEventError("INVALID_TIME_MSC") from exc
    if broker_milliseconds <= 0:
        raise TickEventError("INVALID_TIME_MSC")
    broker_seconds = broker_milliseconds // 1000
    broker_offset_seconds = broker_seconds - utc_seconds
    utc_milliseconds = broker_milliseconds - broker_offset_seconds * 1000
    timestamp = datetime.fromtimestamp(utc_milliseconds / 1000.0, timezone.utc)
    if abs(timestamp.timestamp() - utc_seconds) > 1.0:
        raise TickEventError("INCONSISTENT_TIME_FIELDS")
    return timestamp


async def _run_cli(args: argparse.Namespace) -> int:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for signum in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(signum, stop.set)
        except NotImplementedError:
            pass
    ingress = WebSocketTickIngress(
        database=args.database,
        symbols=tuple(args.symbols),
        host=args.host,
        port=args.port,
        path=args.path,
        status_path=args.status,
        batch_size=args.batch_size,
        batch_wait_seconds=args.batch_wait_seconds,
    )
    await ingress.serve_forever(stop)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", required=True)
    parser.add_argument("--status", required=True)
    parser.add_argument("--symbols", nargs="+", default=DEFAULT_SYMBOLS)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--path", default=DEFAULT_PATH)
    parser.add_argument("--batch-size", type=int, default=256)
    parser.add_argument("--batch-wait-seconds", type=float, default=0.1)
    args = parser.parse_args()
    return asyncio.run(_run_cli(args))


if __name__ == "__main__":
    raise SystemExit(main())
