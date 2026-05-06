from __future__ import annotations

import copy
import concurrent.futures
import json
import logging
import re
import threading
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import pandas as pd
import requests


LOCAL_TZ = ZoneInfo("Asia/Shanghai")
SCHEDULE_HOUR = 8
DISPLAY_INTERVAL = "15m"
MIN_HOLD_DAYS = 10
BREAKOUT_RECENT_DAYS = 3
BREAKOUT_REFERENCE_DAYS = 8
MAX_CONSECUTIVE_FAILURES = 3
DEFAULT_BACKFILL_DAYS = 20
KLINE_BACKFILL_WORKERS = 1
KLINE_REQUEST_DELAY_SECONDS = 0.12
ASCII_SYMBOL_RE = re.compile(r"^[a-z0-9]+$")
BINANCE_FUTURES_KLINES_URL = "https://fapi.binance.com/fapi/v1/klines"
KLINE_COLUMNS = [
    "time",
    "open",
    "high",
    "low",
    "close",
    "volume",
    "end_time",
    "amount",
    "count",
    "buy_volume",
    "buy_amount",
    "null",
]
NUMERIC_COLUMNS = ["open", "high", "low", "close", "volume", "amount"]


def now_local() -> datetime:
    return datetime.now(LOCAL_TZ)


def ensure_local(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=LOCAL_TZ)
    return dt.astimezone(LOCAL_TZ)


def to_iso(dt: datetime | None) -> str | None:
    dt = ensure_local(dt)
    return dt.isoformat() if dt else None


def from_iso(value: str | None) -> datetime | None:
    if not value:
        return None
    return ensure_local(datetime.fromisoformat(value))


@dataclass
class CandidateFilterResult:
    passed: bool
    latest_close: float | None
    recent_high: float | None
    reference_high: float | None
    latest_at: datetime | None
    reason: str


class MarketDataClient:
    def __init__(self, cache_dir: Path, logger: logging.Logger) -> None:
        self.cache_dir = cache_dir
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logger

    def _cache_path(self, symbol: str, interval: str) -> Path:
        return self.cache_dir / f"{symbol.lower()}_{interval}.csv"

    def _normalize_time_column(self, values: pd.Series) -> pd.Series:
        def parse_one(value: Any) -> pd.Timestamp:
            if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
                numeric_value = float(value)
                if numeric_value >= 10**17:
                    return pd.to_datetime(numeric_value, unit="ns", utc=True, errors="coerce")
                if numeric_value >= 10**14:
                    return pd.to_datetime(numeric_value, unit="us", utc=True, errors="coerce")
                return pd.to_datetime(numeric_value, unit="ms", utc=True, errors="coerce")
            return pd.to_datetime(value, utc=True, errors="coerce")

        parsed = values.map(parse_one)
        return parsed.dt.tz_convert(LOCAL_TZ.key)

    def _normalize_frame(self, df: pd.DataFrame) -> pd.DataFrame:
        if df is None or df.empty:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        frame = df.copy()
        if frame.index.name == "time":
            frame = frame.reset_index(drop=True)
        for column in NUMERIC_COLUMNS:
            if column in frame.columns:
                frame[column] = pd.to_numeric(frame[column], errors="coerce")
        if "time" in frame.columns:
            frame["time"] = self._normalize_time_column(frame["time"])
        if "end_time" in frame.columns:
            frame["end_time"] = self._normalize_time_column(frame["end_time"])
        frame = frame.dropna(subset=["time", "close", "high"])
        frame = frame[frame["time"] >= pd.Timestamp("2017-01-01", tz=LOCAL_TZ.key)]
        frame = frame.sort_values("time")
        frame = frame.drop_duplicates(subset=["time"], keep="last")
        frame = frame.set_index("time", drop=False)
        return frame

    def _read_cache(self, symbol: str, interval: str) -> pd.DataFrame:
        path = self._cache_path(symbol, interval)
        if not path.exists():
            return pd.DataFrame(columns=KLINE_COLUMNS)
        try:
            df = pd.read_csv(path)
        except Exception:
            self.logger.warning("failed to read cache %s", path, exc_info=True)
            return pd.DataFrame(columns=KLINE_COLUMNS)
        return self._normalize_frame(df)

    def _write_cache(self, symbol: str, interval: str, df: pd.DataFrame) -> None:
        path = self._cache_path(symbol, interval)
        frame = self._normalize_frame(df)
        if frame.empty:
            return
        out = frame.reset_index(drop=True).copy()
        out["time"] = out["time"].dt.tz_convert("UTC").dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        out["end_time"] = out["end_time"].dt.tz_convert("UTC").dt.strftime("%Y-%m-%dT%H:%M:%S%z")
        out.to_csv(path, index=False)

    def _fetch_klines(self, symbol: str, interval: str, start: datetime, end: datetime) -> pd.DataFrame:
        start = ensure_local(start)
        end = ensure_local(end)
        start_ms = int(start.timestamp() * 1000)
        end_ms = int(end.timestamp() * 1000)
        rows: list[list[Any]] = []
        current_ms = start_ms

        for attempt_block in range(200):
            if current_ms > end_ms:
                break
            params = {
                "symbol": f"{symbol.upper()}USDT",
                "interval": interval,
                "startTime": current_ms,
                "endTime": end_ms,
                "limit": 1000,
            }
            payload: Any = None
            for attempt in range(5):
                try:
                    time.sleep(KLINE_REQUEST_DELAY_SECONDS)
                    response = requests.get(BINANCE_FUTURES_KLINES_URL, params=params, timeout=20)
                    response.raise_for_status()
                    payload = response.json()
                except requests.HTTPError as exc:
                    status_code = exc.response.status_code if exc.response is not None else None
                    if status_code == 429:
                        retry_after = exc.response.headers.get("Retry-After") if exc.response is not None else None
                        wait_seconds = float(retry_after) if retry_after else 30 * (attempt + 1)
                        self.logger.warning(
                            "kline request rate limited symbol=%s interval=%s attempt=%s sleep=%ss",
                            symbol,
                            interval,
                            attempt + 1,
                            wait_seconds,
                        )
                        time.sleep(wait_seconds)
                    else:
                        self.logger.warning(
                            "kline request failed symbol=%s interval=%s attempt=%s",
                            symbol,
                            interval,
                            attempt + 1,
                            exc_info=True,
                        )
                        time.sleep(min(4, attempt + 1))
                    continue
                except Exception:
                    self.logger.warning(
                        "kline request failed symbol=%s interval=%s attempt=%s",
                        symbol,
                        interval,
                        attempt + 1,
                        exc_info=True,
                    )
                    time.sleep(min(4, attempt + 1))
                    continue
                if isinstance(payload, list):
                    break
                self.logger.warning(
                    "kline request returned non-list symbol=%s interval=%s attempt=%s payload=%s",
                    symbol,
                    interval,
                    attempt + 1,
                    payload,
                )
                time.sleep(min(4, attempt + 1))
            if not isinstance(payload, list) or not payload:
                break
            rows.extend(payload)
            last_open_time = int(payload[-1][0])
            if last_open_time < current_ms:
                break
            current_ms = last_open_time + 1

        if not rows:
            return pd.DataFrame(columns=KLINE_COLUMNS)
        frame = pd.DataFrame(rows, columns=KLINE_COLUMNS)
        return self._normalize_frame(frame)

    def get_data(
        self,
        symbol: str,
        interval: str,
        from_datetime: datetime,
        to_datetime: datetime,
        silent: bool = True,
    ) -> pd.DataFrame:
        from_datetime = ensure_local(from_datetime)
        to_datetime = ensure_local(to_datetime)
        cached = self._read_cache(symbol, interval)
        need_fetch = cached.empty

        if not cached.empty:
            cache_min = ensure_local(cached.index.min().to_pydatetime())
            cache_max = ensure_local(cached.index.max().to_pydatetime())
            if from_datetime < cache_min or to_datetime > cache_max:
                need_fetch = True

        if need_fetch:
            fetched = self._fetch_klines(symbol, interval, from_datetime, to_datetime)
            merged = fetched if cached.empty else pd.concat([cached, fetched], ignore_index=False)
            merged = self._normalize_frame(merged.reset_index(drop=True))
            self._write_cache(symbol, interval, merged)
            cached = merged

        if cached.empty:
            return cached
        return cached[(cached.index >= from_datetime) & (cached.index <= to_datetime)].copy()


class MarketPoolService:
    def __init__(self, base_dir: Path) -> None:
        self.base_dir = base_dir
        self.storage_dir = self.base_dir / "storage"
        self.data_dir = self.base_dir / "data"
        self.static_dir = self.base_dir / "static"
        self.state_path = self.storage_dir / "state.json"
        self.market_cache_dir = self.data_dir / "market_cache"
        self.storage_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self.market_cache_dir.mkdir(parents=True, exist_ok=True)
        self.logger = logging.getLogger("market_pool")
        self.market_data = MarketDataClient(self.market_cache_dir, self.logger)
        self.lock = threading.RLock()
        self.state = self._load_state()
        self._scheduler_thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self._exchange_info_cache: dict[str, Any] = {"symbols": set(), "fetched_at": None}
        self._ticker_cache: dict[str, Any] = {"tickers": {}, "fetched_at": None}

    def _default_state(self) -> dict[str, Any]:
        return {
            "last_run_at": None,
            "last_run_trigger": None,
            "next_run_at": to_iso(self._next_run_time(now_local())),
            "pool": {},
            "history": [],
        }

    def _load_state(self) -> dict[str, Any]:
        if not self.state_path.exists():
            state = self._default_state()
            self._save_state(state)
            return state
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            self.logger.exception("state file is invalid, rebuilding")
            state = self._default_state()
            self._save_state(state)
            return state

        default_state = self._default_state()
        default_state.update(state)
        default_state["pool"] = state.get("pool", {})
        default_state["history"] = state.get("history", [])
        default_state["next_run_at"] = to_iso(self._next_run_time(now_local()))
        return default_state

    def _save_state(self, state: dict[str, Any] | None = None) -> None:
        payload = state if state is not None else self.state
        tmp_path = self.state_path.with_suffix(".tmp")
        with tmp_path.open("w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        tmp_path.replace(self.state_path)

    def start(self) -> None:
        self._bootstrap_if_needed()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            return
        self._scheduler_thread = threading.Thread(target=self._scheduler_loop, daemon=True)
        self._scheduler_thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._scheduler_thread and self._scheduler_thread.is_alive():
            self._scheduler_thread.join(timeout=2)

    def _scheduler_loop(self) -> None:
        while not self._stop_event.is_set():
            next_run = self._next_run_time(now_local())
            with self.lock:
                self.state["next_run_at"] = to_iso(next_run)
                self._save_state()

            sleep_seconds = max(1.0, (next_run - now_local()).total_seconds())
            if self._stop_event.wait(timeout=sleep_seconds):
                break

            try:
                self.run_update(trigger="scheduler", run_at=next_run)
            except Exception:
                self.logger.exception("scheduled update failed")

    def _bootstrap_if_needed(self) -> None:
        with self.lock:
            has_pool = bool(self.state.get("pool"))
            last_run_at = from_iso(self.state.get("last_run_at"))

        latest_schedule = self._latest_schedule_time(now_local())
        if not has_pool:
            self.backfill_days(days=DEFAULT_BACKFILL_DAYS, include_now=True)
            return
        if last_run_at is None or last_run_at < latest_schedule:
            self.run_update(trigger="catchup", run_at=latest_schedule)

    def _latest_schedule_time(self, current: datetime) -> datetime:
        current = ensure_local(current)
        candidate = current.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
        if current < candidate:
            return candidate - timedelta(days=1)
        return candidate

    def _next_run_time(self, current: datetime) -> datetime:
        current = ensure_local(current)
        candidate = current.replace(hour=SCHEDULE_HOUR, minute=0, second=0, microsecond=0)
        if current >= candidate:
            return candidate + timedelta(days=1)
        return candidate

    def _valid_symbols(self) -> set[str]:
        cached_at = self._exchange_info_cache.get("fetched_at")
        if cached_at and now_local() - cached_at < timedelta(hours=6):
            return self._exchange_info_cache["symbols"]

        response = requests.get("https://fapi.binance.com/fapi/v1/exchangeInfo", timeout=15)
        response.raise_for_status()
        payload = response.json()
        symbols = {
            item["symbol"]
            for item in payload.get("symbols", [])
            if item.get("status") == "TRADING"
            and item.get("quoteAsset") == "USDT"
            and item.get("contractType") == "PERPETUAL"
        }
        self._exchange_info_cache = {"symbols": symbols, "fetched_at": now_local()}
        return symbols

    def _valid_base_symbols(self) -> list[str]:
        base_symbols = []
        for symbol in self._valid_symbols():
            if not symbol.endswith("USDT"):
                continue
            base_symbol = symbol[:-4].lower()
            if ASCII_SYMBOL_RE.fullmatch(base_symbol):
                base_symbols.append(base_symbol)
        return sorted(set(base_symbols))

    def _ticker_24hr_map(self) -> dict[str, dict[str, float]]:
        cached_at = self._ticker_cache.get("fetched_at")
        if cached_at and now_local() - cached_at < timedelta(seconds=30):
            return self._ticker_cache["tickers"]

        valid_symbols = self._valid_symbols()
        response = requests.get("https://fapi.binance.com/fapi/v1/ticker/24hr", timeout=15)
        response.raise_for_status()

        tickers: dict[str, dict[str, float]] = {}
        for item in response.json():
            symbol = item.get("symbol")
            if symbol not in valid_symbols:
                continue
            base_symbol = symbol.replace("USDT", "").lower()
            if not ASCII_SYMBOL_RE.fullmatch(base_symbol):
                continue
            try:
                tickers[base_symbol] = {
                    "change_pct": float(item.get("priceChangePercent", 0)),
                    "last_price": float(item.get("lastPrice", 0)),
                    "quote_volume": float(item.get("quoteVolume", 0)),
                }
            except (TypeError, ValueError):
                continue

        self._ticker_cache = {"tickers": tickers, "fetched_at": now_local()}
        return tickers

    def fetch_top_movers(self, limit: int = 10) -> list[dict[str, Any]]:
        movers = []
        for base_symbol, ticker in self._ticker_24hr_map().items():
            movers.append(
                {
                    "symbol": base_symbol,
                    "contract_symbol": f"{base_symbol.upper()}USDT",
                    "change_pct": ticker["change_pct"],
                    "last_price": ticker["last_price"],
                    "quote_volume": ticker["quote_volume"],
                }
            )

        movers.sort(key=lambda item: (item["change_pct"], item["quote_volume"]), reverse=True)
        top = movers[:limit]
        for idx, item in enumerate(top, start=1):
            item["rank"] = idx
        return top

    def _price_at_or_before(self, df: pd.DataFrame, target: datetime) -> tuple[datetime | None, float | None]:
        if df.empty:
            return None, None
        target = ensure_local(target)
        index = df.index[df.index <= target]
        if len(index) == 0:
            return None, None
        ts = index[-1]
        return ensure_local(ts.to_pydatetime()), float(df.loc[ts, "close"])

    def _load_symbol_frames(self, start: datetime, end: datetime) -> dict[str, pd.DataFrame]:
        symbols = self._valid_base_symbols()
        self.logger.info("loading %s symbols from %s to %s", len(symbols), start.isoformat(), end.isoformat())
        normalized = {}

        def load_one(symbol: str) -> tuple[str, pd.DataFrame | None]:
            return symbol, self._fetch_hourly_frame(symbol, start, end)

        with concurrent.futures.ThreadPoolExecutor(max_workers=KLINE_BACKFILL_WORKERS) as executor:
            futures = [executor.submit(load_one, symbol) for symbol in symbols]
            for idx, future in enumerate(concurrent.futures.as_completed(futures), start=1):
                symbol, df = future.result()
                if idx % 50 == 0 or idx == len(futures):
                    self.logger.info("loaded %s/%s symbols for backfill", idx, len(futures))
                if df is None or df.empty:
                    continue
                normalized[symbol] = df.sort_index()
        return normalized

    def _fetch_kline_frame(self, symbol: str, start: datetime, end: datetime, interval: str) -> pd.DataFrame | None:
        return self.market_data.get_data(symbol, interval, start, end, silent=True)

    def _fetch_hourly_frame(self, symbol: str, start: datetime, end: datetime) -> pd.DataFrame | None:
        return self._fetch_kline_frame(symbol, start, end, "1h")

    def _top_movers_from_frames(self, frames: dict[str, pd.DataFrame], run_at: datetime, limit: int = 10) -> list[dict[str, Any]]:
        run_at = ensure_local(run_at)
        window_start = run_at - timedelta(days=1)

        movers = []
        for symbol, df in frames.items():
            end_ts, end_close = self._price_at_or_before(df, run_at)
            start_ts, start_close = self._price_at_or_before(df, window_start)
            if end_ts is None or start_ts is None or end_close is None or start_close in (None, 0):
                continue
            if end_ts < run_at - timedelta(hours=3):
                continue
            if start_ts > window_start + timedelta(hours=3):
                continue

            window_df = df[(df.index > window_start) & (df.index <= run_at)]
            movers.append(
                {
                    "symbol": symbol,
                    "contract_symbol": f"{symbol.upper()}USDT",
                    "change_pct": (end_close / start_close - 1) * 100,
                    "last_price": end_close,
                    "quote_volume": float(window_df["amount"].sum()) if not window_df.empty else 0.0,
                }
            )

        movers.sort(key=lambda item: (item["change_pct"], item["quote_volume"]), reverse=True)
        top = movers[:limit]
        for idx, item in enumerate(top, start=1):
            item["rank"] = idx
        return top

    def fetch_top_movers_at(self, run_at: datetime, limit: int = 10) -> list[dict[str, Any]]:
        run_at = ensure_local(run_at)
        frames = self._load_symbol_frames(run_at - timedelta(days=1, hours=2), run_at)
        return self._top_movers_from_frames(frames, run_at, limit=limit)

    def _entry_needs_filter(self, entry: dict[str, Any], run_at: datetime) -> bool:
        added_at = from_iso(entry.get("added_at"))
        last_filter_at = from_iso(entry.get("last_filter_at"))
        if not added_at:
            return False
        if run_at - added_at < timedelta(days=MIN_HOLD_DAYS):
            return False
        if last_filter_at is None:
            return True
        return last_filter_at.date() < run_at.date()

    def _evaluate_breakout(self, symbol: str, run_at: datetime) -> CandidateFilterResult:
        recent_start = run_at - timedelta(days=BREAKOUT_RECENT_DAYS)
        reference_start = run_at - timedelta(days=BREAKOUT_REFERENCE_DAYS)
        df = self.market_data.get_data(symbol, "1h", reference_start, run_at, silent=True)
        if df.empty:
            return CandidateFilterResult(
                passed=True,
                latest_close=None,
                recent_high=None,
                reference_high=None,
                latest_at=None,
                reason="missing_data",
            )

        df = df.sort_index()
        latest = df.iloc[-1]
        latest_at = ensure_local(df.index[-1].to_pydatetime())
        reference_df = df[(df.index >= reference_start) & (df.index < recent_start)]
        recent_df = df[df.index >= recent_start]
        if reference_df.empty or recent_df.empty:
            return CandidateFilterResult(
                passed=True,
                latest_close=float(latest["close"]),
                recent_high=float(recent_df["high"].max()) if not recent_df.empty else None,
                reference_high=None,
                latest_at=latest_at,
                reason="insufficient_window",
            )

        reference_high = float(reference_df["high"].max())
        latest_close = float(latest["close"])
        recent_high = float(recent_df["high"].max())
        return CandidateFilterResult(
            passed=recent_high > reference_high,
            latest_close=latest_close,
            recent_high=recent_high,
            reference_high=reference_high,
            latest_at=latest_at,
            reason="ok",
        )

    def _apply_update(self, movers: list[dict[str, Any]], trigger: str, run_at: datetime) -> dict[str, Any]:
        with self.lock:
            pool = self.state.setdefault("pool", {})
            for symbol in list(pool):
                if not ASCII_SYMBOL_RE.fullmatch(symbol):
                    del pool[symbol]
            added_symbols: list[str] = []

            for mover in movers:
                symbol = mover["symbol"]
                entry = pool.get(symbol)
                if entry is None:
                    entry = {
                        "symbol": symbol,
                        "added_at": to_iso(run_at),
                        "entry_price": mover["last_price"],
                        "entry_change_pct": mover["change_pct"],
                        "entry_rank": mover["rank"],
                        "first_seen_at": to_iso(run_at),
                        "seen_count": 0,
                        "last_filter_at": None,
                        "last_filter_status": None,
                        "last_breakout_high": None,
                        "consecutive_failure_count": 0,
                    }
                    pool[symbol] = entry
                    added_symbols.append(symbol)

                entry["seen_count"] = int(entry.get("seen_count", 0)) + 1
                entry["last_seen_at"] = to_iso(run_at)
                entry["last_price"] = mover["last_price"]
                entry["last_change_pct"] = mover["change_pct"]
                entry["last_rank"] = mover["rank"]
                entry["last_quote_volume"] = mover["quote_volume"]

            removed_symbols: list[str] = []
            for symbol, entry in list(pool.items()):
                if not self._entry_needs_filter(entry, run_at):
                    continue

                result = self._evaluate_breakout(symbol, run_at)
                entry["last_filter_at"] = to_iso(run_at)
                entry["last_breakout_high"] = result.reference_high
                entry["last_recent_high"] = result.recent_high
                entry["last_day_high"] = result.recent_high
                entry["last_breakout_checked_at"] = to_iso(result.latest_at)
                if result.latest_close is not None:
                    entry["last_price"] = result.latest_close
                if result.passed:
                    entry["consecutive_failure_count"] = 0
                    entry["last_filter_status"] = "passed"
                else:
                    entry["consecutive_failure_count"] = int(entry.get("consecutive_failure_count", 0)) + 1
                    entry["last_filter_status"] = "failed"
                if int(entry.get("consecutive_failure_count", 0)) >= MAX_CONSECUTIVE_FAILURES:
                    entry["last_filter_status"] = "removed"
                    removed_symbols.append(symbol)
                    del pool[symbol]

            history_item = {
                "run_at": to_iso(run_at),
                "trigger": trigger,
                "added_symbols": added_symbols,
                "removed_symbols": removed_symbols,
                "top_movers": movers,
                "pool_symbols": sorted(pool.keys()),
                "pool_size": len(pool),
            }
            history = self.state.setdefault("history", [])
            history.insert(0, history_item)
            del history[30:]
            self.state["last_run_at"] = to_iso(run_at)
            self.state["last_run_trigger"] = trigger
            self.state["next_run_at"] = to_iso(self._next_run_time(now_local()))
            self._save_state()

        return history_item

    def _history_item_for_run_at(self, run_at: datetime) -> dict[str, Any] | None:
        run_at_iso = to_iso(run_at)
        with self.lock:
            for item in self.state.get("history", []):
                if item.get("run_at") == run_at_iso:
                    return copy.deepcopy(item)
        return None

    def run_update(self, trigger: str = "manual", run_at: datetime | None = None) -> dict[str, Any]:
        run_at = ensure_local(run_at) or self._latest_schedule_time(now_local())
        existing = self._history_item_for_run_at(run_at)
        if existing is not None and existing.get("top_movers"):
            return existing
        movers = self.fetch_top_movers_at(run_at, limit=10)
        return self._apply_update(movers, trigger, run_at)

    def reset_state(self) -> None:
        with self.lock:
            self.state = self._default_state()
            self._save_state()

    def backfill_days(self, days: int = 7, include_now: bool = True) -> dict[str, Any]:
        if days <= 0:
            raise ValueError("days must be positive")

        current = now_local()
        latest_schedule = self._latest_schedule_time(current)
        first_run = latest_schedule - timedelta(days=days - 1)
        run_points = [first_run + timedelta(days=offset) for offset in range(days)]
        if include_now and current - latest_schedule >= timedelta(minutes=1):
            run_points.append(current)

        self.logger.info(
            "backfill start days=%s include_now=%s points=%s from=%s to=%s",
            days,
            include_now,
            len(run_points),
            run_points[0].isoformat(),
            run_points[-1].isoformat(),
        )

        self.reset_state()
        frames = self._load_symbol_frames(run_points[0] - timedelta(days=1, hours=2), run_points[-1])
        results = []
        for idx, run_point in enumerate(run_points, start=1):
            movers = self._top_movers_from_frames(frames, run_point, limit=10)
            trigger = "backfill_now" if include_now and idx == len(run_points) and run_point != latest_schedule else "backfill"
            result = self._apply_update(movers, trigger, run_point)
            results.append(result)
            self.logger.info(
                "backfill step %s/%s at %s added=%s removed=%s",
                idx,
                len(run_points),
                run_point.isoformat(),
                len(result["added_symbols"]),
                len(result["removed_symbols"]),
            )

        with self.lock:
            self.state["next_run_at"] = to_iso(self._next_run_time(now_local()))
            self._save_state()

        return {
            "days": days,
            "include_now": include_now,
            "runs": len(results),
            "from": to_iso(run_points[0]),
            "to": to_iso(run_points[-1]),
            "last_result": results[-1] if results else None,
        }

    def _build_symbol_snapshot(self, symbol: str, entry: dict[str, Any]) -> dict[str, Any]:
        entry_dt = from_iso(entry.get("added_at")) or now_local() - timedelta(days=1)
        current = now_local()
        start = entry_dt - timedelta(minutes=15)
        end = current
        df = self._fetch_kline_frame(symbol, start, end, DISPLAY_INTERVAL)
        if df is None or df.empty:
            return {
                "symbol": symbol,
                "series": [],
                "base_price": entry.get("entry_price"),
                "latest_price": entry.get("last_price"),
                "latest_change_from_entry_pct": None,
                "latest_point_at": None,
            }

        df = df.sort_index()
        df = df[df.index >= entry_dt]
        if df.empty:
            return {
                "symbol": symbol,
                "series": [],
                "base_price": entry.get("entry_price"),
                "latest_price": entry.get("last_price"),
                "latest_change_from_entry_pct": None,
                "latest_point_at": None,
            }

        base_row = df.iloc[0]
        base_time = ensure_local(df.index[0].to_pydatetime())
        base_price = float(base_row["close"])
        series = []
        sampled = df
        if len(df) > 960:
            sampled = df.iloc[::4]
        elif len(df) > 480:
            sampled = df.iloc[::2]
        if sampled.index[-1] != df.index[-1]:
            sampled = pd.concat([sampled, df.iloc[[-1]]]).sort_index()
            sampled = sampled[~sampled.index.duplicated(keep="last")]
        for idx, row in sampled.iterrows():
            point_time = ensure_local(idx.to_pydatetime())
            close = float(row["close"])
            series.append(
                {
                    "x": round((point_time - base_time).total_seconds() / 86400, 4),
                    "y": round(close / base_price * 100, 4),
                    "t": to_iso(point_time),
                    "price": close,
                }
            )

        latest_price = float(df.iloc[-1]["close"])
        latest_change_pct = (latest_price / base_price - 1) * 100
        return {
            "symbol": symbol,
            "series": series,
            "base_price": base_price,
            "latest_price": latest_price,
            "latest_change_from_entry_pct": round(latest_change_pct, 2),
            "latest_point_at": to_iso(ensure_local(df.index[-1].to_pydatetime())),
        }

    def _build_pool_rows(self, pool: dict[str, Any], tickers: dict[str, dict[str, float]] | None = None) -> list[dict[str, Any]]:
        rows = []
        for symbol, entry in pool.items():
            added_at = from_iso(entry.get("added_at"))
            ticker = tickers.get(symbol) if tickers else None
            latest_price = ticker.get("last_price") if ticker else entry.get("last_price")
            last_change_pct = ticker.get("change_pct") if ticker else entry.get("last_change_pct")
            entry_price = float(entry.get("entry_price") or 0) if entry.get("entry_price") is not None else None
            change_from_entry_pct = None
            if entry_price and latest_price:
                change_from_entry_pct = round((float(latest_price) / entry_price - 1) * 100, 2)

            rows.append(
                {
                    "symbol": symbol,
                    "added_at": entry.get("added_at"),
                    "days_in_pool": round((now_local() - added_at).total_seconds() / 86400, 1) if added_at else None,
                    "entry_price": entry_price,
                    "latest_price": round(float(latest_price), 6) if latest_price is not None else None,
                    "change_from_entry_pct": change_from_entry_pct,
                    "entry_rank": entry.get("entry_rank"),
                    "last_rank": entry.get("last_rank"),
                    "last_change_pct": round(float(last_change_pct), 2) if last_change_pct is not None else None,
                    "last_filter_at": entry.get("last_filter_at"),
                    "last_filter_status": entry.get("last_filter_status"),
                    "consecutive_failure_count": int(entry.get("consecutive_failure_count", 0)),
                }
            )
        rows.sort(key=lambda item: (item["change_from_entry_pct"] is None, -(item["change_from_entry_pct"] or 0)))
        return rows

    def get_dashboard(self) -> dict[str, Any]:
        with self.lock:
            state = copy.deepcopy(self.state)

        pool = state.get("pool", {})
        try:
            tickers = self._ticker_24hr_map()
        except Exception:
            self.logger.warning("failed to refresh live ticker prices for dashboard", exc_info=True)
            tickers = {}
        pool_rows = self._build_pool_rows(pool, tickers=tickers)
        recent_run = state.get("history", [{}])[0] if state.get("history") else {}
        daily_candidate_pools: list[dict[str, Any]] = []
        seen_dates: set[str] = set()
        for item in state.get("history", []):
            run_at = item.get("run_at")
            if not run_at:
                continue
            day_key = run_at[:10]
            if day_key in seen_dates:
                continue
            pool_symbols = item.get("pool_symbols")
            if pool_symbols is None:
                continue
            seen_dates.add(day_key)
            daily_candidate_pools.append(
                {
                    "date": day_key,
                    "run_at": run_at,
                    "trigger": item.get("trigger"),
                    "pool_size": item.get("pool_size", len(pool_symbols)),
                    "symbols": pool_symbols,
                    "added_symbols": item.get("added_symbols", []),
                    "removed_symbols": item.get("removed_symbols", []),
                }
            )

        return {
            "generated_at": to_iso(now_local()),
            "schedule": {
                "timezone": "Asia/Shanghai",
                "daily_run_time": "08:00",
                "display_interval": DISPLAY_INTERVAL,
                "min_hold_days": MIN_HOLD_DAYS,
                "breakout_recent_days": BREAKOUT_RECENT_DAYS,
                "breakout_reference_days": BREAKOUT_REFERENCE_DAYS,
                "max_consecutive_failures": MAX_CONSECUTIVE_FAILURES,
                "last_run_at": state.get("last_run_at"),
                "last_run_trigger": state.get("last_run_trigger"),
                "next_run_at": state.get("next_run_at"),
            },
            "candidate_pool": pool_rows,
            "normalized_series": [],
            "recent_top_movers": recent_run.get("top_movers", []),
            "recent_history": state.get("history", [])[:10],
            "daily_candidate_pools": daily_candidate_pools,
        }

    def get_tradingview_watchlist_text(self) -> str:
        with self.lock:
            symbols = sorted(self.state.get("pool", {}).keys())
        return ",".join(f"BINANCE:{symbol.upper()}USDT.P" for symbol in symbols) + "\n"
