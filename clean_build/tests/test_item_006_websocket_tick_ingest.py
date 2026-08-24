import asyncio
from datetime import datetime, timezone
import json
from pathlib import Path
import sqlite3

from websockets.asyncio.client import connect

from cipherfx_clean.tick_ingest import WebSocketTickIngress


UTC = timezone.utc


async def _wait_for(predicate, *, timeout_seconds=3.0):
    deadline = asyncio.get_running_loop().time() + timeout_seconds
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise AssertionError("timed out waiting for condition")
        await asyncio.sleep(0.01)


def test_tick_ingress_accepts_only_valid_local_websocket_ticks_and_persists_milliseconds(tmp_path):
    async def exercise():
        database = tmp_path / "ticks.sqlite3"
        status = tmp_path / "tick_ingest_status.json"
        stop = asyncio.Event()
        ingress = WebSocketTickIngress(
            database=database,
            symbols=("XAUUSD",),
            port=0,
            status_path=status,
            batch_wait_seconds=0.01,
        )
        task = asyncio.create_task(ingress.serve_forever(stop))
        await _wait_for(lambda: ingress.port != 0)
        broker_utc_offset = 10800
        utc_seconds = int(datetime(2026, 8, 24, 10, tzinfo=UTC).timestamp())
        payload = {
            "type": "tick",
            "symbol": "XAUUSD",
            "time_utc": utc_seconds,
            "time_msc": (utc_seconds + broker_utc_offset) * 1000 + 321,
            "bid": 2000.1,
            "ask": 2000.3,
        }
        async with connect(f"ws://127.0.0.1:{ingress.port}/mt5/ticks") as socket:
            await socket.send(json.dumps(payload))
            await socket.send(json.dumps(payload))
            await socket.send(json.dumps({"type": "heartbeat", "source": "test"}))
            await socket.send(json.dumps({**payload, "symbol": "UNCONFIGURED"}))
            await _wait_for(lambda: ingress.metrics().persisted_ticks == 1)

        stop.set()
        await task
        metrics = ingress.metrics()
        assert metrics.persisted_ticks == 1
        assert metrics.duplicate_ticks == 1
        assert metrics.heartbeats == 1
        assert metrics.rejected_events == 0
        assert metrics.ignored_events == 1
        assert metrics.last_tick_at["XAUUSD"] == "2026-08-24T10:00:00.321000+00:00"
        saved = json.loads(status.read_text())
        assert saved["mode"] == "READ_ONLY_WEBSOCKET_TICK_INGRESS"
        connection = sqlite3.connect(database)
        try:
            rows = connection.execute(
                "SELECT symbol,timestamp,bid,ask FROM raw_ticks"
            ).fetchall()
        finally:
            connection.close()
        assert rows == [("XAUUSD", "2026-08-24T10:00:00.321000+00:00", 2000.1, 2000.3)]

    asyncio.run(exercise())


def test_tick_ingress_source_has_no_execution_or_broker_order_surface():
    source = Path(__import__("cipherfx_clean.tick_ingest").tick_ingest.__file__).read_text()

    assert "order_send" not in source
    assert "MetaTrader5" not in source
    assert "ExecutionPort" not in source


def test_tick_ingress_accepts_the_existing_mt5_bridge_handshake_shape(tmp_path):
    async def exercise():
        database = tmp_path / "bridge-shape.sqlite3"
        stop = asyncio.Event()
        ingress = WebSocketTickIngress(
            database=database,
            symbols=("XAUUSD",),
            port=0,
            batch_wait_seconds=0.01,
        )
        task = asyncio.create_task(ingress.serve_forever(stop))
        await _wait_for(lambda: ingress.port != 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", ingress.port)
        # This exact key is what the existing MQL bridge uses.  It is not the
        # standard decoded length, but its masked frame transport is otherwise
        # valid and is constrained to localhost plus the configured symbols.
        request = (
            "GET /mt5/ticks HTTP/1.1\r\n"
            "Host: 127.0.0.1:8765\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: Q2lwaGVyRlhUcmFkZVdTS2V5MTIzNA==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        response = await reader.readuntil(b"\r\n\r\n")
        assert response.startswith(b"HTTP/1.1 101")

        payload = json.dumps(
            {
                "type": "tick",
                "symbol": "XAUUSD",
                "time_utc": 1787565600,
                "time_msc": 1787576400123,
                "bid": 2000.1,
                "ask": 2000.3,
            }
        ).encode("utf-8")
        mask = b"\x01\x02\x03\x04"
        masked = bytes(value ^ mask[index % 4] for index, value in enumerate(payload))
        writer.write(bytes((0x81, 0x80 | len(payload))) + mask + masked)
        await writer.drain()
        await _wait_for(lambda: ingress.metrics().persisted_ticks == 1)
        writer.close()
        await writer.wait_closed()
        stop.set()
        await task
        assert ingress.metrics().persisted_ticks == 1
        assert ingress.metrics().last_error is None

    asyncio.run(exercise())


def test_tick_ingress_stops_promptly_with_an_active_bridge_connection(tmp_path):
    async def exercise():
        stop = asyncio.Event()
        ingress = WebSocketTickIngress(
            database=tmp_path / "shutdown.sqlite3",
            symbols=("XAUUSD",),
            port=0,
            batch_wait_seconds=0.01,
        )
        task = asyncio.create_task(ingress.serve_forever(stop))
        await _wait_for(lambda: ingress.port != 0)
        reader, writer = await asyncio.open_connection("127.0.0.1", ingress.port)
        request = (
            "GET /mt5/ticks HTTP/1.1\r\n"
            "Host: 127.0.0.1:8765\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            "Sec-WebSocket-Key: Q2lwaGVyRlhUcmFkZVdTS2V5MTIzNA==\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n"
        )
        writer.write(request.encode("ascii"))
        await writer.drain()
        assert (await reader.readuntil(b"\r\n\r\n")).startswith(b"HTTP/1.1 101")

        stop.set()
        await asyncio.wait_for(task, timeout=1.0)
        writer.close()
        await writer.wait_closed()

    asyncio.run(exercise())
