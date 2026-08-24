from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from cipherfx_clean.hfm_data import HfmMarketDataAdapter, ensure_utc
from cipherfx_clean.hfm_data import (
    HfmCsvMarketDataAdapter,
    derive_timeframe,
    hfm_server_bar_times,
    hfm_server_epoch_to_utc,
    hfm_server_utc_offset_seconds,
    hfm_utc_bar_end,
    valid_hfm_normalized_bar_duration,
)
from cipherfx_clean.snapshot import RESEARCH_TIMEFRAMES, snapshot_from_dataset


UTC = timezone.utc


class FakeTerminal:
    COPY_TICKS_ALL = 0
    TIMEFRAME_M1 = 1
    TIMEFRAME_M3 = 3
    TIMEFRAME_M5 = 5
    TIMEFRAME_M15 = 15
    TIMEFRAME_M30 = 30
    TIMEFRAME_H1 = 60
    TIMEFRAME_H4 = 240
    TIMEFRAME_D1 = 1440
    TIMEFRAME_W1 = 10080
    TIMEFRAME_MN1 = 43200

    def __init__(self, now):
        self.now = now
        self.calls = []

    def symbols_get(self):
        return (SimpleNamespace(name="XAUUSD", visible=True, select=True, trade_mode=4, digits=2, point=.01, trade_tick_size=.01, trade_tick_value=1.0, trade_contract_size=100.0, volume_min=.01, volume_max=100.0, volume_step=.01, currency_profit="USD", path="Metals\\XAUUSD"),)

    def symbol_info(self, symbol):
        return self.symbols_get()[0]

    def symbol_info_tick(self, symbol):
        return SimpleNamespace(time=int(self.now.timestamp()), time_msc=int(self.now.timestamp() * 1000), bid=2000.0, ask=2000.2)

    def copy_ticks_range(self, symbol, start, end, flags):
        self.calls.append(("ticks", start, end))
        return [
            {"time_msc": int((self.now - timedelta(seconds=3)).timestamp() * 1000), "bid": 2000.0, "ask": 2000.2},
            {"time_msc": int((self.now - timedelta(seconds=2)).timestamp() * 1000), "bid": 2000.1, "ask": 2000.3},
        ]

    def copy_rates_range(self, symbol, timeframe, start, end):
        self.calls.append(("bars", timeframe, start, end))
        seconds = {1: 60, 3: 180, 5: 300, 15: 900, 30: 1800, 60: 3600, 240: 14400, 1440: 86400, 10080: 604800, 43200: 2678400}[timeframe]
        bar = int((self.now - timedelta(seconds=seconds * 2)).timestamp())
        return [
            {"time": bar, "open": 1999.0, "high": 2001.0, "low": 1998.0, "close": 2000.0, "tick_volume": 50, "spread": 20, "real_volume": 0},
            {"time": int(self.now.timestamp()), "open": 2000.0, "high": 2002.0, "low": 1999.0, "close": 2001.0, "tick_volume": 5, "spread": 20, "real_volume": 0},
        ]

    def terminal_info(self):
        return SimpleNamespace(build=5836, connected=True, trade_allowed=True)

    def account_info(self):
        return SimpleNamespace(login=123, server="HFMarketsSA-Demo", currency="ZAR")

    def last_error(self):
        return (1, "Success")


def test_hfm_adapter_is_read_only_and_preserves_broker_contract_metadata():
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    adapter = HfmMarketDataAdapter(FakeTerminal(now))
    symbol = adapter.symbols()[0]

    assert symbol.symbol == "XAUUSD"
    assert symbol.contract_size == 100.0
    assert symbol.volume_step == .01
    assert not hasattr(adapter, "order_send")
    assert not hasattr(adapter, "initialize")


def test_hfm_dataset_uses_utc_and_removes_forming_native_bars():
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    terminal = FakeTerminal(now)
    adapter = HfmMarketDataAdapter(terminal)
    dataset = adapter.dataset("XAUUSD", now - timedelta(days=40), now, observed_at=now)

    assert dataset.valid
    assert set(dataset.bars) == set(RESEARCH_TIMEFRAMES)
    assert all(len(rows) == 1 for rows in dataset.bars.values())
    assert all(row.source == "HFM_MT5_NATIVE" for rows in dataset.bars.values() for row in rows)
    assert all(call[-2].utcoffset() == timedelta(0) for call in terminal.calls)
    assert dataset.terminal["build"] == 5836
    assert dataset.account["currency"] == "ZAR"
    assert dataset.contract["trade_contract_size"] == 100.0


def test_snapshot_contains_all_ten_research_frames_and_micro_provenance():
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    dataset = HfmMarketDataAdapter(FakeTerminal(now)).dataset("XAUUSD", now - timedelta(days=40), now, observed_at=now)
    snapshot = snapshot_from_dataset(dataset, observed_at=now)

    assert tuple(frame for frame in RESEARCH_TIMEFRAMES if snapshot.frame_status[frame] == "COMPLETED") == RESEARCH_TIMEFRAMES
    assert set(("1s", "5s", "15s")).issubset(snapshot.candles)
    assert snapshot.candles["1s"][0].source == "DERIVED_FROM_HFM_TICKS"
    assert dataset.dataset_id in snapshot.source_ids


def test_naive_terminal_dates_are_rejected_instead_of_guessing_timezone():
    with pytest.raises(ValueError, match="timezone-aware"):
        ensure_utc(datetime(2026, 8, 21, 12))


def test_csv_adapter_combines_hfm_m1_history_and_preserves_source_ids(tmp_path):
    (tmp_path / "symbols.csv").write_text(
        "symbol,visible,description,path,digits,point,volume_min,volume_max,volume_step,contract_size,tick_value,tick_size,trade_mode,currency_profit\n"
        "XAUUSD,1,Gold,Metals,2,0.01,0.01,60,0.01,100,1,0.01,4,USD\n"
    )
    header = "time,open,high,low,close,volume,spread_points,spread_price\n"
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    old = int((now - timedelta(minutes=3)).timestamp()) + 10800
    recent = int((now - timedelta(minutes=2)).timestamp()) + 10800
    (tmp_path / "deephistory_XAUUSD_M1.csv").write_text(header + f"{old},1,2,0.5,1.5,10,2,0.02\n")
    (tmp_path / "rates_XAUUSD_M1.csv").write_text(header + f"{recent},1.5,2.5,1,2,20,3,0.03\n")
    for timeframe in ("M3", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1"):
        seconds = {"M3": 180, "M5": 300, "M15": 900, "M30": 1800, "H1": 3600, "H4": 14400, "D1": 86400, "W1": 604800, "MN1": 5356800}[timeframe]
        stamp = int((now - timedelta(seconds=seconds)).timestamp()) + 10800
        (tmp_path / f"rates_XAUUSD_{timeframe}.csv").write_text(header + f"{stamp},1,2,0.5,1.5,10,2,0.02\n")

    adapter = HfmCsvMarketDataAdapter(tmp_path)
    dataset = adapter.dataset("XAUUSD", observed_at=now)

    assert len(dataset.bars["M1"]) == 2
    assert dataset.bars["M1"][0].source == "HFM_MT5_CSV_UTC_NORMALIZED"
    assert dataset.bars["M1"][0].start == now - timedelta(minutes=3)
    assert dataset.bars["M1"][0].source_id
    assert adapter.symbols()[0].symbol == "XAUUSD"
    assert dataset.contract["contract_size"] == 100.0


def test_csv_live_tick_uses_exported_utc_not_broker_time(tmp_path):
    utc_epoch = 1_787_345_939
    (tmp_path / "tick_XAUUSD.txt").write_text(
        "time_broker=1787356739\n"
        f"time_utc={utc_epoch}\n"
        "broker_utc_offset_seconds=10800\n"
        "export_receipt_local_epoch=1787345940\n"
        "bid=4603.76\nask=4604.19\n"
    )
    export = HfmCsvMarketDataAdapter(tmp_path).latest_tick_export("XAUUSD")
    tick = export.tick

    assert tick.timestamp == datetime.fromtimestamp(utc_epoch, UTC)
    assert tick.ask - tick.bid == pytest.approx(.43)
    assert export.broker_utc_offset_seconds == 10800
    assert int((export.broker_timestamp - export.utc_timestamp).total_seconds()) == 10800


def test_csv_dataset_rejects_tick_after_requested_observation(tmp_path):
    observed = datetime(2026, 8, 24, 6, tzinfo=UTC)
    future = int((observed + timedelta(seconds=1)).timestamp())
    (tmp_path / "symbols.csv").write_text(
        "symbol,visible,description,path,digits,point,volume_min,volume_max,volume_step,contract_size,tick_value,tick_size,trade_mode,currency_profit\n"
        "XAUUSD,1,Gold,Metals,2,0.01,0.01,60,0.01,100,1,0.01,4,USD\n"
    )
    (tmp_path / "tick_XAUUSD.txt").write_text(
        f"time_broker={future + 10800}\n"
        f"time_utc={future}\n"
        "broker_utc_offset_seconds=10800\n"
        "bid=4603.76\nask=4604.19\n"
    )

    dataset = HfmCsvMarketDataAdapter(tmp_path).dataset(
        "XAUUSD",
        observed_at=observed,
        timeframes=(),
        include_latest_tick=True,
    )

    assert dataset.ticks == ()
    assert tuple(issue.code for issue in dataset.issues) == ("TICK_AFTER_OBSERVATION",)


def test_hfm_server_bar_time_uses_documented_winter_and_summer_offsets():
    winter_wall = datetime(2026, 1, 15, 12, tzinfo=UTC)
    summer_wall = datetime(2026, 8, 21, 12, tzinfo=UTC)

    assert hfm_server_utc_offset_seconds(winter_wall) == 7200
    assert hfm_server_utc_offset_seconds(summer_wall) == 10800
    assert hfm_server_epoch_to_utc(int(winter_wall.timestamp())) == datetime(2026, 1, 15, 10, tzinfo=UTC)
    assert hfm_server_epoch_to_utc(int(summer_wall.timestamp())) == datetime(2026, 8, 21, 9, tzinfo=UTC)
    start, end = hfm_server_bar_times(int(summer_wall.timestamp()), "H1")
    assert start == datetime(2026, 8, 21, 9, tzinfo=UTC)
    assert end == datetime(2026, 8, 21, 10, tzinfo=UTC)


def test_hfm_month_bar_end_is_next_broker_month_not_next_utc_month():
    server_july = int(datetime(2026, 7, 1, tzinfo=UTC).timestamp())
    start, end = hfm_server_bar_times(server_july, "MN1")

    assert start == datetime(2026, 6, 30, 21, tzinfo=UTC)
    assert end == datetime(2026, 7, 31, 21, tzinfo=UTC)
    assert hfm_utc_bar_end(start, "MN1") == end
    assert valid_hfm_normalized_bar_duration(start, end, "MN1")


def test_normalized_hfm_calendar_bars_allow_only_dst_sized_boundaries():
    start = datetime(2026, 3, 22, 22, tzinfo=UTC)
    assert valid_hfm_normalized_bar_duration(start, start + timedelta(hours=167), "W1")
    assert valid_hfm_normalized_bar_duration(start, start + timedelta(hours=168), "W1")
    assert valid_hfm_normalized_bar_duration(start, start + timedelta(hours=169), "W1")
    assert not valid_hfm_normalized_bar_duration(start, start + timedelta(hours=166), "W1")


def test_live_rolling_mode_never_reads_deep_history(tmp_path):
    header = "time,open,high,low,close,volume,spread_points,spread_price\n"
    now = datetime(2026, 8, 21, 12, tzinfo=UTC)
    server_epoch = int((now - timedelta(minutes=2)).timestamp()) + 10800
    (tmp_path / "rates_XAUUSD_M1.csv").write_text(
        header + f"{server_epoch},1,2,0.5,1.5,10,2,0.02\n"
    )
    (tmp_path / "deephistory_XAUUSD_M1.csv").write_text(
        header + f"{server_epoch - 60},9,10,8,9.5,10,2,0.02\n"
    )

    rows = HfmCsvMarketDataAdapter(tmp_path).read_bars(
        "XAUUSD", "M1", observed_at=now, history_mode="rolling"
    )

    assert len(rows) == 1
    assert rows[0].open == 1


def test_unknown_history_mode_is_rejected_explicitly(tmp_path):
    with pytest.raises(ValueError, match="history_mode"):
        HfmCsvMarketDataAdapter(tmp_path).read_bars(
            "XAUUSD",
            "M1",
            observed_at=datetime(2026, 8, 21, 12, tzinfo=UTC),
            history_mode="guess",
        )


def test_m3_derivation_requires_three_contiguous_m1_bars_and_keeps_provenance():
    start = datetime(2026, 8, 21, 10, tzinfo=UTC)
    rows = tuple(
        __import__("cipherfx_clean.contracts", fromlist=["Candle"]).Candle(
            "XAUUSD", "M1", start + timedelta(minutes=index), start + timedelta(minutes=index + 1),
            100 + index, 101 + index, 99 + index, 100.5 + index, source="HFM_MT5_CSV", source_id=f"m1:{index}"
        )
        for index in range(3)
    )
    derived = derive_timeframe(rows, "M3", 3)

    assert len(derived) == 1
    assert derived[0].open == 100
    assert derived[0].close == 102.5
    assert derived[0].source == "DERIVED_M3_FROM_M1"
    assert derived[0].source_id
