"""Async localhost MT5-to-Python WebSocket event transport."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import socket
import threading
from datetime import datetime
from typing import Callable


class BridgeTickFileRecovery:
    """Recover factual ticks from MT5 bridge exports when the MQL socket is unavailable.

    The bridge files are written by the live MT5 EA. This path never synthesizes
    prices: it emits only a newly observed, timestamped tick from a fresh export.
    """

    def __init__(
        self,
        bridge_dir,
        on_message: Callable[[dict], None],
        *,
        symbol_normalizer: Callable[[str], str] | None = None,
        interval_seconds: float = 0.10,
    ):
        from pathlib import Path

        self.bridge_dir = Path(bridge_dir) if bridge_dir else Path("")
        self.on_message = on_message
        self.symbol_normalizer = symbol_normalizer
        self.interval_seconds = max(0.05, min(float(interval_seconds), 2.0))
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._last_mtime_ns: dict[str, int] = {}
        self._last_tick_epoch: dict[str, float] = {}
        self.event_count = 0
        self.last_event_at = ""
        self.last_error = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> bool:
        if self.running or not self.bridge_dir.is_dir():
            return False
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run,
            name="mt5-bridge-tick-file-recovery",
            daemon=True,
        )
        self._thread.start()
        return True

    def stop(self) -> None:
        self._stop.set()
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None

    def _normalize_symbol(self, raw_symbol: str) -> str:
        raw = str(raw_symbol or "").strip().upper()
        if self.symbol_normalizer:
            try:
                normalized = str(self.symbol_normalizer(raw) or "").strip().upper()
                if normalized:
                    return normalized
            except Exception:
                pass
        return raw

    @staticmethod
    def _read_values(path) -> dict[str, str]:
        values: dict[str, str] = {}
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                key, separator, value = line.strip().partition("=")
                if separator and key:
                    values[key.strip()] = value.strip()
        return values

    def _process_file(self, path) -> None:
        stat = path.stat()
        previous_mtime = self._last_mtime_ns.get(str(path), 0)
        if stat.st_mtime_ns <= previous_mtime:
            return
        self._last_mtime_ns[str(path)] = stat.st_mtime_ns
        values = self._read_values(path)
        tick_epoch = float(values.get("time_utc") or 0.0)
        if tick_epoch <= 0.0:
            return
        raw_symbol = path.stem[5:] if path.stem.lower().startswith("tick_") else path.stem
        symbol = self._normalize_symbol(raw_symbol)
        if not symbol or tick_epoch <= self._last_tick_epoch.get(symbol, 0.0):
            return
        bid = float(values.get("bid") or 0.0)
        ask = float(values.get("ask") or 0.0)
        if bid <= 0.0 or ask <= 0.0:
            return
        self._last_tick_epoch[symbol] = tick_epoch
        event = {
            "type": "tick",
            "symbol": symbol,
            "time_utc": tick_epoch,
            "bid": bid,
            "ask": ask,
            "last": float(values.get("last") or 0.0),
            "m5_bar_open_utc": int(tick_epoch // 300.0 * 300.0),
            "_feed_source": "bridge_tick_file_recovery",
        }
        self.event_count += 1
        self.last_event_at = datetime.utcnow().isoformat()
        self.on_message(event)

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                for path in sorted(self.bridge_dir.glob("tick_*.txt")):
                    try:
                        self._process_file(path)
                    except (OSError, ValueError) as exc:
                        self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self._stop.wait(self.interval_seconds)
            except Exception as exc:
                self.last_error = f"{type(exc).__name__}: {str(exc)[:160]}"
                self._stop.wait(self.interval_seconds)


class TickWebSocketServer:
    """Async WebSocket server running in a dedicated asyncio loop thread."""

    def __init__(self, host: str, port: int, on_message: Callable[[dict], None], *, enabled: bool = True):
        self.host = str(host)
        self.port = int(port)
        self.on_message = on_message
        self.enabled = bool(enabled)
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._server: asyncio.AbstractServer | None = None
        self._ready = threading.Event()
        self._stopped = threading.Event()
        self._stop_event: asyncio.Event | None = None
        self.connected_clients = 0
        self.received_events = 0
        self.last_event_at = ""

    @property
    def running(self) -> bool:
        return bool(self._thread and self._thread.is_alive() and self._ready.is_set() and not self._stopped.is_set())

    def start(self) -> bool:
        if not self.enabled or self.running:
            return False
        self._ready.clear()
        self._stopped.clear()
        self._thread = threading.Thread(target=self._run_loop, name="mt5-tick-websocket-async", daemon=True)
        self._thread.start()
        return bool(self._ready.wait(2.0) and self.running)

    def stop(self) -> None:
        loop = self._loop
        stop_event = self._stop_event
        if loop and stop_event and not self._stopped.is_set():
            loop.call_soon_threadsafe(stop_event.set)
        thread = self._thread
        if thread and thread.is_alive() and thread is not threading.current_thread():
            thread.join(timeout=3.0)
        self._thread = None
        self._loop = None
        self._server = None
        self._ready.clear()
        self._stopped.set()
        self.connected_clients = 0

    def _run_loop(self) -> None:
        loop = asyncio.new_event_loop()
        self._loop = loop
        asyncio.set_event_loop(loop)
        self._stop_event = asyncio.Event()
        try:
            self._server = loop.run_until_complete(
                asyncio.start_server(self._client_loop, self.host, self.port, limit=1_048_576)
            )
            self._ready.set()
            loop.run_until_complete(self._stop_event.wait())
            if self._server is not None:
                self._server.close()
                loop.run_until_complete(self._server.wait_closed())
        except Exception:
            self._ready.set()
        finally:
            try:
                pending = asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            except Exception:
                pass
            loop.close()
            self._stopped.set()

    async def _client_loop(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        self.connected_clients += 1
        try:
            header = await reader.readuntil(b"\r\n\r\n")
            if len(header) > 16384:
                return
            headers = {}
            decoded = header.decode("latin1", errors="replace")
            for line in decoded.split("\r\n")[1:]:
                if ":" in line:
                    key, value = line.split(":", 1)
                    headers[key.strip().lower()] = value.strip()
            key = headers.get("sec-websocket-key")
            if not key:
                return
            accept = base64.b64encode(
                hashlib.sha1((key + "258EAFA5-E914-47DA-95CA-C5AB0DC85B11").encode("ascii")).digest()
            ).decode("ascii")
            writer.write((
                "HTTP/1.1 101 Switching Protocols\r\n"
                "Upgrade: websocket\r\n"
                "Connection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("ascii"))
            await writer.drain()
            while not self._stopped.is_set():
                frame = await self._read_frame(reader)
                if frame is None:
                    return
                payload, opcode = frame
                if opcode == 8:
                    return
                if opcode == 9:
                    await self._send_frame(writer, payload, 10)
                    continue
                if opcode != 1:
                    continue
                try:
                    event = json.loads(payload.decode("utf-8"))
                except Exception:
                    continue
                if not isinstance(event, dict):
                    continue
                event["_ws_received_at"] = datetime.utcnow().isoformat()
                self.received_events += 1
                self.last_event_at = event["_ws_received_at"]
                try:
                    self.on_message(event)
                except Exception:
                    pass
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError, OSError, asyncio.TimeoutError):
            return
        finally:
            self.connected_clients = max(0, self.connected_clients - 1)
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:
                pass

    async def _read_frame(self, reader: asyncio.StreamReader):
        try:
            first, second = await reader.readexactly(2)
            opcode = first & 0x0F
            masked = bool(second & 0x80)
            length = second & 0x7F
            if length == 126:
                length = int.from_bytes(await reader.readexactly(2), "big")
            elif length == 127:
                length = int.from_bytes(await reader.readexactly(8), "big")
            if length > 1_000_000:
                return None
            mask = await reader.readexactly(4) if masked else b""
            payload = await reader.readexactly(length)
            if masked:
                payload = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
            return payload, opcode
        except (asyncio.IncompleteReadError, ConnectionError, OSError):
            return None

    @staticmethod
    async def _send_frame(writer: asyncio.StreamWriter, payload: bytes, opcode: int) -> None:
        length = len(payload)
        if length < 126:
            header = bytes([0x80 | (opcode & 0x0F), length])
        elif length < 65536:
            header = bytes([0x80 | (opcode & 0x0F), 126]) + length.to_bytes(2, "big")
        else:
            header = bytes([0x80 | (opcode & 0x0F), 127]) + length.to_bytes(8, "big")
        writer.write(header + payload)
        await writer.drain()
