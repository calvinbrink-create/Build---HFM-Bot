"""Read-only HFM/MetaTrader 5 market-data boundary.

The adapter accepts an already connected terminal API object. It deliberately
does not expose initialize, login, symbol_select, order_check, order_send,
position modification, or position close. The existing bot owns all broker
side effects; this module can only inventory and read market observations.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import csv
from datetime import date, datetime, timedelta, timezone
import calendar as month_calendar
from hashlib import sha256
import json
from pathlib import Path
from typing import Any, Iterable, Mapping, Protocol, Sequence

from .contracts import Candle, RawTick


NATIVE_TIMEFRAMES = ("M1", "M3", "M5", "M15", "M30", "H1", "H4", "D1", "W1", "MN1")
HFM_SERVER_TIME_POLICY = "HFM_OFFICIAL_GMT2_WINTER_GMT3_LAST_SUNDAY_DST_V1"


class HfmReadPort(Protocol):
    COPY_TICKS_ALL: int

    def symbols_get(self, *args: object, **kwargs: object) -> object: ...
    def symbol_info(self, symbol: str) -> object: ...
    def symbol_info_tick(self, symbol: str) -> object: ...
    def copy_rates_range(self, symbol: str, timeframe: object, start: datetime, end: datetime) -> object: ...
    def copy_ticks_range(self, symbol: str, start: datetime, end: datetime, flags: int) -> object: ...
    def terminal_info(self) -> object: ...
    def account_info(self) -> object: ...
    def last_error(self) -> object: ...


@dataclass(frozen=True)
class SymbolRecord:
    symbol: str
    visible: bool
    selected: bool
    trade_mode: int
    digits: int
    point: float
    tick_size: float
    tick_value: float
    contract_size: float
    volume_min: float
    volume_max: float
    volume_step: float
    currency_profit: str
    path: str


@dataclass(frozen=True)
class DataIssue:
    code: str
    detail: str
    symbol: str
    timeframe: str | None = None


@dataclass(frozen=True)
class TickExport:
    tick: RawTick
    broker_timestamp: datetime
    utc_timestamp: datetime
    broker_utc_offset_seconds: int
    export_receipt_epoch: int | None
    source_path: str


@dataclass(frozen=True)
class SourceDataset:
    dataset_id: str
    symbol: str
    start: datetime
    end: datetime
    bars: Mapping[str, tuple[Candle, ...]]
    ticks: tuple[RawTick, ...]
    source_hashes: Mapping[str, str]
    issues: tuple[DataIssue, ...]
    terminal: Mapping[str, object]
    account: Mapping[str, object]
    revision: int = 1
    parent_dataset_id: str | None = None
    contract: Mapping[str, object] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.issues


def ensure_utc(value: datetime) -> datetime:
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone-aware datetime required")
    return value.astimezone(timezone.utc)


def timeframe_end(start: datetime, timeframe: str) -> datetime:
    fixed = {
        "1s": 1,
        "5s": 5,
        "15s": 15,
        "M1": 60,
        "M3": 180,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
        "D1": 86400,
        "W1": 604800,
    }
    utc = ensure_utc(start)
    if timeframe in fixed:
        return utc + timedelta(seconds=fixed[timeframe])
    if timeframe == "MN1":
        return datetime(utc.year + (1 if utc.month == 12 else 0), 1 if utc.month == 12 else utc.month + 1, 1, tzinfo=timezone.utc)
    raise ValueError(f"unsupported timeframe: {timeframe}")


def hfm_server_utc_offset_seconds(server_wall_time: datetime) -> int:
    """Return HFM's documented server offset for a server-wall timestamp."""

    wall = server_wall_time.replace(tzinfo=None)
    dst_start = _last_sunday(wall.year, 3)
    dst_end = _last_sunday(wall.year, 10)
    return 10800 if dst_start <= wall.date() < dst_end else 7200


def hfm_server_epoch_to_utc(server_epoch: int) -> datetime:
    """Convert an MT server-wall epoch to UTC without treating it as UTC."""

    wall = datetime.fromtimestamp(server_epoch, timezone.utc).replace(tzinfo=None)
    offset = hfm_server_utc_offset_seconds(wall)
    return wall.replace(tzinfo=timezone.utc) - timedelta(seconds=offset)


def hfm_server_bar_times(server_epoch: int, timeframe: str) -> tuple[datetime, datetime]:
    """Convert both bar boundaries so DST and calendar frames retain lineage."""

    wall_start = datetime.fromtimestamp(server_epoch, timezone.utc).replace(tzinfo=None)
    wall_end = _advance_server_wall_time(wall_start, timeframe)
    start = wall_start.replace(tzinfo=timezone.utc) - timedelta(
        seconds=hfm_server_utc_offset_seconds(wall_start)
    )
    end = wall_end.replace(tzinfo=timezone.utc) - timedelta(
        seconds=hfm_server_utc_offset_seconds(wall_end)
    )
    return start, end


def hfm_utc_bar_end(start_utc: datetime, timeframe: str) -> datetime:
    """Return the expected UTC end for a normalized HFM server-time bar."""

    utc = ensure_utc(start_utc)
    wall_start = None
    for offset in (7200, 10800):
        candidate = (utc + timedelta(seconds=offset)).replace(tzinfo=None)
        if hfm_server_utc_offset_seconds(candidate) == offset:
            wall_start = candidate
            break
    if wall_start is None:
        raise ValueError("UTC timestamp cannot be reconciled to HFM server time")
    wall_end = _advance_server_wall_time(wall_start, timeframe)
    return wall_end.replace(tzinfo=timezone.utc) - timedelta(
        seconds=hfm_server_utc_offset_seconds(wall_end)
    )


def valid_hfm_normalized_bar_duration(start: datetime, end: datetime, timeframe: str) -> bool:
    """Validate normalized bar duration without guessing an ambiguous DST wall time."""

    seconds = (ensure_utc(end) - ensure_utc(start)).total_seconds()
    exact = {
        "M1": 60,
        "M3": 180,
        "M5": 300,
        "M15": 900,
        "M30": 1800,
        "H1": 3600,
        "H4": 14400,
    }
    if timeframe in exact:
        return seconds == exact[timeframe]
    if timeframe == "D1":
        return 23 * 3600 <= seconds <= 25 * 3600
    if timeframe == "W1":
        return 167 * 3600 <= seconds <= 169 * 3600
    if timeframe == "MN1":
        return 27 * 86400 <= seconds <= 32 * 86400
    return False


def _last_sunday(year: int, month: int) -> date:
    last_day = month_calendar.monthrange(year, month)[1]
    candidate = date(year, month, last_day)
    return candidate - timedelta(days=(candidate.weekday() + 1) % 7)


def _advance_server_wall_time(start: datetime, timeframe: str) -> datetime:
    fixed = {
        "M1": timedelta(minutes=1),
        "M3": timedelta(minutes=3),
        "M5": timedelta(minutes=5),
        "M15": timedelta(minutes=15),
        "M30": timedelta(minutes=30),
        "H1": timedelta(hours=1),
        "H4": timedelta(hours=4),
        "D1": timedelta(days=1),
        "W1": timedelta(days=7),
    }
    if timeframe in fixed:
        return start + fixed[timeframe]
    if timeframe == "MN1":
        return datetime(
            start.year + (1 if start.month == 12 else 0),
            1 if start.month == 12 else start.month + 1,
            1,
        )
    raise ValueError(f"unsupported HFM timeframe: {timeframe}")


def _mapping(row: object) -> Mapping[str, Any]:
    if isinstance(row, Mapping):
        return row
    asdict = getattr(row, "_asdict", None)
    if callable(asdict):
        return asdict()
    dtype = getattr(row, "dtype", None)
    if dtype is not None and getattr(dtype, "names", None):
        return {name: row[name].item() if hasattr(row[name], "item") else row[name] for name in dtype.names}
    if hasattr(row, "__dict__"):
        return {key: value for key, value in vars(row).items() if not key.startswith("_")}
    raise TypeError(f"unsupported MT5 record: {type(row)!r}")


def _object_mapping(value: object | None) -> Mapping[str, object]:
    if value is None:
        return {}
    try:
        return dict(_mapping(value))
    except TypeError:
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}


def _stable_hash(payload: object) -> str:
    raw = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str).encode("utf-8")
    return sha256(raw).hexdigest()


class HfmMarketDataAdapter:
    """Translate MetaTrader records into immutable clean contracts."""

    def __init__(self, terminal: HfmReadPort):
        self._terminal = terminal

    def symbols(self) -> tuple[SymbolRecord, ...]:
        rows = self._terminal.symbols_get()
        if rows is None:
            raise RuntimeError(f"symbols_get failed: {self._terminal.last_error()}")
        result: list[SymbolRecord] = []
        for raw in rows:
            item = _mapping(raw)
            result.append(
                SymbolRecord(
                    symbol=str(item.get("name", "")),
                    visible=bool(item.get("visible", False)),
                    selected=bool(item.get("select", item.get("selected", False))),
                    trade_mode=int(item.get("trade_mode", 0)),
                    digits=int(item.get("digits", 0)),
                    point=float(item.get("point", 0.0)),
                    tick_size=float(item.get("trade_tick_size", 0.0)),
                    tick_value=float(item.get("trade_tick_value", 0.0)),
                    contract_size=float(item.get("trade_contract_size", 0.0)),
                    volume_min=float(item.get("volume_min", 0.0)),
                    volume_max=float(item.get("volume_max", 0.0)),
                    volume_step=float(item.get("volume_step", 0.0)),
                    currency_profit=str(item.get("currency_profit", "")),
                    path=str(item.get("path", "")),
                )
            )
        return tuple(result)

    def latest_tick(self, symbol: str) -> RawTick:
        row = self._terminal.symbol_info_tick(symbol)
        if row is None:
            raise RuntimeError(f"symbol_info_tick failed for {symbol}: {self._terminal.last_error()}")
        item = _mapping(row)
        epoch_ms = int(item.get("time_msc", 0))
        timestamp = datetime.fromtimestamp(epoch_ms / 1000.0 if epoch_ms else int(item["time"]), timezone.utc)
        return RawTick(symbol, timestamp, float(item["bid"]), float(item["ask"]))

    def read_ticks(self, symbol: str, start: datetime, end: datetime) -> tuple[RawTick, ...]:
        utc_start, utc_end = ensure_utc(start), ensure_utc(end)
        rows = self._terminal.copy_ticks_range(symbol, utc_start, utc_end, self._terminal.COPY_TICKS_ALL)
        if rows is None:
            raise RuntimeError(f"copy_ticks_range failed for {symbol}: {self._terminal.last_error()}")
        output: list[RawTick] = []
        for raw in rows:
            item = _mapping(raw)
            epoch_ms = int(item.get("time_msc", 0))
            timestamp = datetime.fromtimestamp(epoch_ms / 1000.0 if epoch_ms else int(item["time"]), timezone.utc)
            output.append(RawTick(symbol, timestamp, float(item["bid"]), float(item["ask"])))
        return tuple(output)

    def read_bars(self, symbol: str, timeframe: str, start: datetime, end: datetime, *, observed_at: datetime) -> tuple[Candle, ...]:
        if timeframe not in NATIVE_TIMEFRAMES:
            raise ValueError(f"unsupported native HFM timeframe: {timeframe}")
        constant = getattr(self._terminal, f"TIMEFRAME_{timeframe}", None)
        if constant is None:
            raise RuntimeError(f"terminal has no TIMEFRAME_{timeframe}")
        utc_start, utc_end, observed = ensure_utc(start), ensure_utc(end), ensure_utc(observed_at)
        rows = self._terminal.copy_rates_range(symbol, constant, utc_start, utc_end)
        if rows is None:
            raise RuntimeError(f"copy_rates_range failed for {symbol} {timeframe}: {self._terminal.last_error()}")
        output: list[Candle] = []
        for raw in rows:
            item = _mapping(raw)
            bar_start = datetime.fromtimestamp(int(item["time"]), timezone.utc)
            bar_end = timeframe_end(bar_start, timeframe)
            if bar_end > observed:
                continue
            source_id = _stable_hash((symbol, timeframe, int(item["time"]), float(item["open"]), float(item["high"]), float(item["low"]), float(item["close"])))
            output.append(
                Candle(
                    symbol=symbol,
                    timeframe=timeframe,
                    start=bar_start,
                    end=bar_end,
                    open=float(item["open"]),
                    high=float(item["high"]),
                    low=float(item["low"]),
                    close=float(item["close"]),
                    tick_count=int(item.get("tick_volume", 0)),
                    tick_volume=float(item.get("tick_volume", 0.0)),
                    real_volume=float(item.get("real_volume", 0.0)),
                    spread_points=float(item.get("spread", 0.0)),
                    source="HFM_MT5_NATIVE",
                    source_id=source_id,
                )
            )
        return tuple(output)

    def dataset(
        self,
        symbol: str,
        start: datetime,
        end: datetime,
        *,
        observed_at: datetime,
        timeframes: Sequence[str] = NATIVE_TIMEFRAMES,
        include_ticks: bool = True,
    ) -> SourceDataset:
        utc_start, utc_end, observed = ensure_utc(start), ensure_utc(end), ensure_utc(observed_at)
        bars: dict[str, tuple[Candle, ...]] = {}
        issues: list[DataIssue] = []
        hashes: dict[str, str] = {}
        for timeframe in timeframes:
            try:
                rows = self.read_bars(symbol, timeframe, utc_start, utc_end, observed_at=observed)
            except (RuntimeError, ValueError) as error:
                issues.append(DataIssue("IMPORT_ERROR", str(error), symbol, timeframe))
                rows = ()
            bars[timeframe] = rows
            hashes[timeframe] = _stable_hash([candle.source_id for candle in rows])
            issues.extend(_bar_issues(symbol, timeframe, rows))
        try:
            ticks = self.read_ticks(symbol, utc_start, utc_end) if include_ticks else ()
        except RuntimeError as error:
            issues.append(DataIssue("IMPORT_ERROR", str(error), symbol, "TICK"))
            ticks = ()
        issues.extend(_tick_issues(symbol, ticks))
        hashes["TICK"] = _stable_hash([(row.timestamp.isoformat(), row.bid, row.ask) for row in ticks])
        terminal = {
            **_object_mapping(self._terminal.terminal_info()),
            "timestamp_source": "MT5_EPOCH_UTC",
        }
        account = _object_mapping(self._terminal.account_info())
        contract = _object_mapping(self._terminal.symbol_info(symbol))
        dataset_payload = {
            "symbol": symbol,
            "start": utc_start.isoformat(),
            "end": utc_end.isoformat(),
            "bars": hashes,
            "terminal_build": terminal.get("build"),
            "account_server": account.get("server"),
            "contract": contract,
        }
        return SourceDataset(
            _stable_hash(dataset_payload), symbol, utc_start, utc_end, bars, ticks,
            hashes, tuple(issues), terminal, account, contract=contract,
        )


def _bar_issues(symbol: str, timeframe: str, rows: Sequence[Candle]) -> tuple[DataIssue, ...]:
    issues: list[DataIssue] = []
    seen: set[datetime] = set()
    previous: datetime | None = None
    for row in rows:
        if row.start in seen:
            issues.append(DataIssue("DUPLICATE_BAR", row.start.isoformat(), symbol, timeframe))
        seen.add(row.start)
        if previous is not None and row.start <= previous:
            issues.append(DataIssue("NON_INCREASING_BAR", row.start.isoformat(), symbol, timeframe))
        previous = row.start
        if min(row.open, row.high, row.low, row.close) <= 0 or row.high < max(row.open, row.close) or row.low > min(row.open, row.close) or row.high < row.low:
            issues.append(DataIssue("INVALID_OHLC", row.start.isoformat(), symbol, timeframe))
        if row.start.tzinfo is None or row.start.utcoffset() != timedelta(0):
            issues.append(DataIssue("NON_UTC_BAR", row.start.isoformat(), symbol, timeframe))
    if not rows:
        issues.append(DataIssue("MISSING_BARS", "no completed bars returned", symbol, timeframe))
    return tuple(issues)


def _tick_issues(symbol: str, rows: Sequence[RawTick]) -> tuple[DataIssue, ...]:
    issues: list[DataIssue] = []
    previous: datetime | None = None
    for row in rows:
        if row.bid <= 0 or row.ask < row.bid:
            issues.append(DataIssue("INVALID_TICK", row.timestamp.isoformat(), symbol, "TICK"))
        if previous is not None and row.timestamp <= previous:
            issues.append(DataIssue("NON_INCREASING_TICK", row.timestamp.isoformat(), symbol, "TICK"))
        previous = row.timestamp
    return tuple(issues)


def with_tick_window(dataset: SourceDataset, ticks: Sequence[RawTick]) -> SourceDataset:
    """Return a new immutable dataset revision containing a real tick window."""

    ordered = tuple(
        sorted(
            {
                (item.timestamp, item.bid, item.ask): item
                for item in ticks
                if item.symbol == dataset.symbol
            }.values(),
            key=lambda item: (item.timestamp, item.bid, item.ask),
        )
    )
    hashes = dict(dataset.source_hashes)
    hashes["TICK"] = _stable_hash(
        [(row.timestamp.isoformat(), row.bid, row.ask) for row in ordered]
    )
    issues = tuple(item for item in dataset.issues if item.timeframe != "TICK") + _tick_issues(
        dataset.symbol,
        ordered,
    )
    terminal = dict(dataset.terminal)
    terminal["tick_window_count"] = len(ordered)
    terminal["tick_window_source"] = "PERSISTED_RAW_TICKS"
    revision = dataset.revision + 1
    start = min(dataset.start, ordered[0].timestamp) if ordered else dataset.start
    end = max(dataset.end, ordered[-1].timestamp) if ordered else dataset.end
    manifest = {
        "parent_dataset_id": dataset.dataset_id,
        "revision": revision,
        "symbol": dataset.symbol,
        "start": start.isoformat(),
        "end": end.isoformat(),
        "hashes": hashes,
        "contract": dict(dataset.contract),
        "terminal": terminal,
    }
    return SourceDataset(
        dataset_id=_stable_hash(manifest),
        symbol=dataset.symbol,
        start=start,
        end=end,
        bars=dict(dataset.bars),
        ticks=ordered,
        source_hashes=hashes,
        issues=issues,
        terminal=terminal,
        account=dict(dataset.account),
        revision=revision,
        parent_dataset_id=dataset.dataset_id,
        contract=dict(dataset.contract),
    )


class HfmCsvMarketDataAdapter:
    """Read terminal-authored CSV exports without importing a legacy runtime."""

    def __init__(self, root: str | Path):
        self._root = Path(root)

    def symbols(self) -> tuple[SymbolRecord, ...]:
        path = self._root / "symbols.csv"
        if not path.is_file():
            raise FileNotFoundError(path)
        result: list[SymbolRecord] = []
        with path.open(newline="", encoding="utf-8-sig") as handle:
            for item in csv.DictReader(handle):
                result.append(
                    SymbolRecord(
                        symbol=item["symbol"],
                        visible=item.get("visible", "0") == "1",
                        selected=item.get("visible", "0") == "1",
                        trade_mode=int(item.get("trade_mode", 0)),
                        digits=int(item.get("digits", 0)),
                        point=float(item.get("point", 0.0)),
                        tick_size=float(item.get("tick_size", 0.0)),
                        tick_value=float(item.get("tick_value", 0.0)),
                        contract_size=float(item.get("contract_size", 0.0)),
                        volume_min=float(item.get("volume_min", 0.0)),
                        volume_max=float(item.get("volume_max", 0.0)),
                        volume_step=float(item.get("volume_step", 0.0)),
                        currency_profit=item.get("currency_profit", ""),
                        path=item.get("path", ""),
                    )
                )
        return tuple(result)

    def read_bars(
        self,
        symbol: str,
        timeframe: str,
        *,
        observed_at: datetime,
        history_mode: str = "combined",
    ) -> tuple[Candle, ...]:
        observed = ensure_utc(observed_at)
        paths = self._bar_paths(symbol, timeframe, history_mode=history_mode)
        rows_by_start: dict[datetime, Candle] = {}
        for path in paths:
            with path.open(newline="", encoding="utf-8-sig") as handle:
                for item in csv.DictReader(handle):
                    server_epoch = int(item["time"])
                    start, end = hfm_server_bar_times(server_epoch, timeframe)
                    if end > observed:
                        continue
                    source_id = _stable_hash((path.name, item, HFM_SERVER_TIME_POLICY, start.isoformat(), end.isoformat()))
                    rows_by_start[start] = Candle(
                        symbol=symbol,
                        timeframe=timeframe,
                        start=start,
                        end=end,
                        open=float(item["open"]),
                        high=float(item["high"]),
                        low=float(item["low"]),
                        close=float(item["close"]),
                        tick_count=int(float(item.get("volume", item.get("tick_volume", 0)))),
                        tick_volume=float(item.get("volume", item.get("tick_volume", 0.0))),
                        real_volume=float(item.get("real_volume", 0.0)),
                        spread_points=float(item.get("spread_points", item.get("spread", 0.0))),
                        source="HFM_MT5_CSV_UTC_NORMALIZED",
                        source_id=source_id,
                    )
        return tuple(rows_by_start[key] for key in sorted(rows_by_start))

    def latest_tick(self, symbol: str) -> RawTick:
        return self.latest_tick_export(symbol).tick

    def tick_export_path(self, symbol: str) -> Path:
        return self._root / f"tick_{symbol}.txt"

    def latest_tick_export(self, symbol: str) -> TickExport:
        path = self.tick_export_path(symbol)
        if not path.is_file():
            raise FileNotFoundError(path)
        values: dict[str, str] = {}
        for line in path.read_text(encoding="utf-8-sig").splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
        required = ("time_broker", "time_utc", "broker_utc_offset_seconds", "bid", "ask")
        if any(key not in values for key in required):
            raise ValueError(f"tick export is missing {required}: {path}")
        tick = RawTick(
            symbol,
            datetime.fromtimestamp(int(values["time_utc"]), timezone.utc),
            float(values["bid"]),
            float(values["ask"]),
        )
        return TickExport(
            tick,
            datetime.fromtimestamp(int(values["time_broker"]), timezone.utc),
            tick.timestamp,
            int(values["broker_utc_offset_seconds"]),
            int(values["export_receipt_local_epoch"]) if "export_receipt_local_epoch" in values else None,
            str(path),
        )

    def dataset(
        self,
        symbol: str,
        *,
        observed_at: datetime,
        timeframes: Sequence[str] = NATIVE_TIMEFRAMES,
        include_latest_tick: bool = False,
        derive_m3_history: bool = True,
        history_mode: str = "combined",
    ) -> SourceDataset:
        observed = ensure_utc(observed_at)
        bars: dict[str, tuple[Candle, ...]] = {}
        issues: list[DataIssue] = []
        hashes: dict[str, str] = {}
        starts: list[datetime] = []
        ends: list[datetime] = []
        ticks: tuple[RawTick, ...] = ()
        if include_latest_tick:
            try:
                tick = self.latest_tick(symbol)
                if tick.timestamp > observed:
                    issues.append(
                        DataIssue(
                            "TICK_AFTER_OBSERVATION",
                            tick.timestamp.isoformat(),
                            symbol,
                            "TICK",
                        )
                    )
                else:
                    ticks = (tick,)
            except (FileNotFoundError, ValueError) as error:
                issues.append(DataIssue("MISSING_LIVE_TICK", str(error), symbol, "TICK"))
        for timeframe in timeframes:
            try:
                rows = self.read_bars(
                    symbol,
                    timeframe,
                    observed_at=observed,
                    history_mode=history_mode,
                )
            except FileNotFoundError as error:
                issues.append(DataIssue("MISSING_EXPORT", str(error), symbol, timeframe))
                rows = ()
            bars[timeframe] = rows
            issues.extend(_bar_issues(symbol, timeframe, rows))
            hashes[timeframe] = _stable_hash([row.source_id for row in rows])
            if rows:
                starts.append(rows[0].start)
                ends.append(rows[-1].end)
        if derive_m3_history and bars.get("M1"):
            derived = derive_timeframe(bars["M1"], "M3", 3)
            native = {row.start: row for row in bars.get("M3", ())}
            combined = {row.start: row for row in derived}
            combined.update(native)
            bars["M3"] = tuple(combined[key] for key in sorted(combined))
            hashes["M3"] = _stable_hash([row.source_id for row in bars["M3"]])
        hashes["TICK"] = _stable_hash([(row.timestamp.isoformat(), row.bid, row.ask) for row in ticks])
        start = min(starts, default=observed)
        end = max(ends, default=observed)
        contract = next((item for item in self.symbols() if item.symbol == symbol), None)
        contract_payload = contract.__dict__ if contract is not None else {}
        manifest = {
            "symbol": symbol,
            "start": start.isoformat(),
            "end": end.isoformat(),
            "hashes": hashes,
            "contract": contract_payload,
            "history_mode": history_mode,
        }
        return SourceDataset(
            _stable_hash(manifest), symbol, start, end, bars, ticks, hashes, tuple(issues),
            {
                "source": "HFM_MT5_CSV",
                "timestamp_source": "MT_SERVER_WALL_EPOCH",
                "timestamp_policy": HFM_SERVER_TIME_POLICY,
                "history_mode": history_mode,
            }, {},
            contract=contract_payload,
        )

    def _bar_paths(
        self,
        symbol: str,
        timeframe: str,
        *,
        history_mode: str,
    ) -> tuple[Path, ...]:
        if history_mode not in ("combined", "rolling"):
            raise ValueError("history_mode must be combined or rolling")
        candidates: list[Path] = []
        rolling = self._root / f"rates_{symbol}_{timeframe}.csv"
        if history_mode == "rolling":
            if not rolling.is_file():
                raise FileNotFoundError(rolling)
            return (rolling,)
        deep = self._root / f"deephistory_{symbol}_{timeframe}.csv"
        if deep.is_file():
            candidates.append(deep)
        if timeframe == "M1":
            if rolling.is_file():
                candidates.append(rolling)
            history = self._root / f"rates_{symbol}_{timeframe}_HISTORY.csv"
            if history.is_file():
                candidates.append(history)
        else:
            if rolling.is_file():
                candidates.append(rolling)
        if not candidates:
            raise FileNotFoundError(self._root / f"rates_{symbol}_{timeframe}.csv")
        return tuple(candidates)


def derive_timeframe(source: Sequence[Candle], timeframe: str, source_count: int) -> tuple[Candle, ...]:
    """Aggregate only complete contiguous source bars and retain every source ID."""

    if not source or source_count <= 0:
        return ()
    duration = int((source[0].end - source[0].start).total_seconds())
    target_seconds = duration * source_count
    buckets: dict[datetime, list[Candle]] = {}
    for row in sorted(source, key=lambda item: item.start):
        epoch = int(row.start.timestamp())
        start = datetime.fromtimestamp(epoch - epoch % target_seconds, timezone.utc)
        buckets.setdefault(start, []).append(row)
    output: list[Candle] = []
    for start, rows in sorted(buckets.items()):
        rows = sorted(rows, key=lambda item: item.start)
        if len(rows) != source_count:
            continue
        if any(current.start != previous.end for previous, current in zip(rows, rows[1:])):
            continue
        source_id = _stable_hash((timeframe, tuple(row.source_id for row in rows)))
        output.append(
            Candle(
                symbol=rows[0].symbol,
                timeframe=timeframe,
                start=start,
                end=rows[-1].end,
                open=rows[0].open,
                high=max(row.high for row in rows),
                low=min(row.low for row in rows),
                close=rows[-1].close,
                tick_count=sum(row.tick_count for row in rows),
                tick_volume=sum(row.tick_volume for row in rows),
                real_volume=sum(row.real_volume for row in rows),
                spread_points=rows[-1].spread_points,
                source=f"DERIVED_{timeframe}_FROM_{rows[0].timeframe}",
                source_id=source_id,
            )
        )
    return tuple(output)
