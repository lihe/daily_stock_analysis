# -*- coding: utf-8 -*-
"""Regression tests for post-merge Tushare follow-up fixes."""

import importlib.util
import sys
import threading
import time
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from unittest.mock import MagicMock, call, patch

import pandas as pd

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_fetcher import TushareFetcher


class TestTushareFetcherFollowUps(unittest.TestCase):
    """Cover rate limiting and cross-day trade-calendar refresh behavior."""

    @staticmethod
    def _make_fetcher() -> TushareFetcher:
        with patch.object(TushareFetcher, "_init_api", return_value=None):
            fetcher = TushareFetcher()
        fetcher._api = MagicMock()
        fetcher.priority = 2
        return fetcher

    def test_get_trade_time_refreshes_trade_calendar_when_day_changes(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.side_effect = [
            pd.DataFrame({"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}),
            pd.DataFrame({"cal_date": ["20260318", "20260317"], "is_open": [1, 1]}),
        ]

        with patch.object(
            fetcher,
            "_get_china_now",
            side_effect=[
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 17, 20, 0),
                datetime(2026, 3, 18, 20, 0),
                datetime(2026, 3, 18, 20, 0),
            ],
        ), patch.object(fetcher, "_check_rate_limit") as rate_limit_mock:
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260317")
            self.assertEqual(fetcher.get_trade_time(early_time="00:00", late_time="19:00"), "20260318")

        self.assertEqual(fetcher._api.trade_cal.call_count, 2)
        self.assertEqual(rate_limit_mock.call_count, 2)
    def test_get_trade_time_returns_latest_trade_date_on_non_trade_day(self) -> None:
        """Non-trade day (e.g. Saturday) should return the most recent trade
        date (Friday), not the one before it (Thursday).  Fixes #1009."""
        fetcher = self._make_fetcher()
        # 2026-03-21 is Saturday; Friday 20 and Thursday 19 are trade dates
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260314", "20260315", "20260316",
                             "20260317", "20260318", "20260319",
                             "20260320", "20260321"],
                "is_open": [0, 0, 1, 1, 1, 1, 1, 0],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # called twice: once by get_trade_time, once by _get_trade_dates
            side_effect=[datetime(2026, 3, 21, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Should be Friday (20th), NOT Thursday (19th)
        self.assertEqual(result, "20260320")

    def test_get_trade_time_trade_day_before_data_ready_returns_previous(self) -> None:
        """On a trade day within the early-late window, should return the
        previous trade date (data not ready yet for today)."""
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": ["20260319", "20260320"],
                "is_open": [1, 1],
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            # Friday 10:00 AM - within 00:00~19:00 window, data not ready
            side_effect=[datetime(2026, 3, 20, 10, 0)] * 2,
        ), patch.object(fetcher, "_check_rate_limit"):
            result = fetcher.get_trade_time(early_time="00:00", late_time="19:00")

        # Data not ready, should fall back to Thursday (19th)
        self.assertEqual(result, "20260319")

    def test_trade_calendar_lookback_covers_ten_sessions_across_long_holiday(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {
                "cal_date": [
                    "20260223",
                    "20260213",
                    "20260212",
                    "20260211",
                    "20260210",
                    "20260209",
                    "20260206",
                    "20260205",
                    "20260204",
                    "20260203",
                    "20260202",
                ],
                "is_open": [1] * 11,
            }
        )

        with patch.object(
            fetcher,
            "_get_china_now",
            return_value=datetime(2026, 2, 23, 18, 59),
        ), patch.object(fetcher, "_check_rate_limit"):
            trade_dates = fetcher._get_trade_dates("20260223")

        self.assertGreaterEqual(len(trade_dates), 10)
        fetcher._api.trade_cal.assert_called_once_with(
            exchange="SSE",
            start_date="20260114",
            end_date="20260223",
        )
        
          
    def test_get_sector_rankings_rate_limits_calendar_and_rankings_api(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.moneyflow_ind_ths.return_value = pd.DataFrame(
            {
                "industry": ["AI", "消费"],
                "pct_change": [1.8, -0.6],
            }
        )

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 16, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            top, bottom = fetcher.get_sector_rankings(n=1)

        self.assertEqual(top, [{"name": "AI", "change_pct": 1.8}])
        self.assertEqual(bottom, [{"name": "消费", "change_pct": -0.6}])
        self.assertEqual(rate_limit_mock.call_count, 2)

    def test_get_chip_distribution_rate_limits_all_tushare_calls(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.trade_cal.return_value = pd.DataFrame(
            {"cal_date": ["20260317", "20260314"], "is_open": [1, 1]}
        )
        fetcher._api.cyq_chips.return_value = pd.DataFrame(
            {
                "price": [9.0, 10.0, 11.0],
                "percent": [20.0, 50.0, 30.0],
            }
        )
        fetcher._api.daily.return_value = pd.DataFrame({"close": [10.5]})

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 20, 0)), patch.object(
            fetcher, "_check_rate_limit"
        ) as rate_limit_mock:
            chip = fetcher.get_chip_distribution("600519")

        self.assertIsNotNone(chip)
        if chip is None:
            self.fail("expected chip distribution data")
        self.assertEqual(chip.date, "2026-03-17")
        self.assertAlmostEqual(chip.profit_ratio, 0.7)
        self.assertAlmostEqual(chip.avg_cost, 10.1)
        self.assertAlmostEqual(chip.concentration_90, 0.1)
        self.assertAlmostEqual(chip.concentration_70, 0.1)
        self.assertEqual(rate_limit_mock.call_count, 3)

    def test_convert_stock_code_accepts_exchange_prefixed_a_share(self) -> None:
        fetcher = self._make_fetcher()

        self.assertEqual(fetcher._convert_stock_code("SZ000001"), "000001.SZ")
        self.assertEqual(fetcher._convert_stock_code("SH600519"), "600519.SH")
        self.assertEqual(fetcher._convert_stock_code("605218"), "605218.SH")
        self.assertEqual(fetcher._convert_stock_code("600519.SS"), "600519.SH")
        self.assertEqual(fetcher._convert_stock_code("920748"), "920748.BJ")
        self.assertEqual(fetcher._convert_stock_code("BJ920748"), "920748.BJ")
        self.assertEqual(fetcher._convert_stock_code("BJ.920748"), "920748.BJ")
        self.assertEqual(fetcher._convert_stock_code("920748.BJ"), "920748.BJ")

    @patch.dict(sys.modules, {"tushare": MagicMock()})
    def test_legacy_realtime_quote_keeps_sz_hint_as_stock_symbol(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.quotation.side_effect = Exception("quota")

        tushare_module = sys.modules["tushare"]
        tushare_module.get_realtime_quotes.return_value = pd.DataFrame(
            [
                {
                    "name": "平安银行",
                    "price": "10.94",
                    "pre_close": "10.88",
                    "volume": "1000",
                    "amount": "2000",
                    "high": "11.00",
                    "low": "10.80",
                    "open": "10.90",
                }
            ]
        )

        with patch.object(
            fetcher,
            "_call_api_with_rate_limit",
            wraps=fetcher._call_api_with_rate_limit,
        ) as api_wrapper:
            quote = fetcher.get_realtime_quote("SZ000001")

        self.assertIsNotNone(quote)
        self.assertEqual(quote.code, "000001")
        self.assertEqual(quote.name, "平安银行")
        tushare_module.get_realtime_quotes.assert_called_once_with("000001")
        self.assertEqual(
            api_wrapper.call_args_list,
            [
                call("quotation", ts_code="000001.SZ"),
                call("get_realtime_quotes", "000001", _target=tushare_module),
            ],
        )

    def test_structured_capabilities_route_every_endpoint_through_fetcher_callback(self) -> None:
        fetcher = self._make_fetcher()
        calls = []
        open_dates = [
            "20260317",
            "20260316",
            "20260313",
            "20260312",
            "20260311",
            "20260310",
            "20260309",
            "20260306",
            "20260305",
            "20260304",
        ]

        class FakeApi:
            def __getattr__(self, api_name):
                def call(**kwargs):
                    calls.append((api_name, kwargs))
                    if api_name == "trade_cal":
                        return pd.DataFrame(
                            {"cal_date": open_dates, "is_open": [1] * len(open_dates)}
                        )
                    return pd.DataFrame()

                return call

        fetcher._api = FakeApi()

        with patch.object(fetcher, "_get_china_now", return_value=datetime(2026, 3, 17, 20, 0)):
            fetcher.get_fundamental_bundle("600519", timeout_seconds=1.0)
            fetcher.get_capital_flow("600519", timeout_seconds=1.0, top_n=3)

        self.assertEqual(
            {api_name for api_name, _ in calls},
            {
                "trade_cal",
                "fina_indicator",
                "income",
                "cashflow",
                "forecast",
                "express",
                "dividend",
                "top10_holders",
                "moneyflow",
                "moneyflow_ind_ths",
            },
        )
        moneyflow_kwargs = next(kwargs for api_name, kwargs in calls if api_name == "moneyflow")
        self.assertEqual(
            moneyflow_kwargs,
            {
                "ts_code": "600519.SH",
                "start_date": "20260304",
                "end_date": "20260317",
            },
        )
        sector_kwargs = next(
            kwargs for api_name, kwargs in calls if api_name == "moneyflow_ind_ths"
        )
        self.assertEqual(sector_kwargs, {"trade_date": "20260317"})
        self.assertEqual(fetcher._call_count, 10)

    def test_shared_api_slots_cap_all_tushare_calls_and_counter_has_no_lost_updates(self) -> None:
        fetcher = self._make_fetcher()
        fetcher.rate_limit_per_minute = 100
        active = 0
        peak_active = 0
        lock = threading.Lock()

        class SlowApi:
            def daily(self, **_kwargs):
                nonlocal active, peak_active
                with lock:
                    active += 1
                    peak_active = max(peak_active, active)
                try:
                    time.sleep(0.02)
                    return pd.DataFrame()
                finally:
                    with lock:
                        active -= 1

        fetcher._api = SlowApi()
        with ThreadPoolExecutor(max_workers=12) as executor:
            futures = [
                executor.submit(fetcher._call_api_with_rate_limit, "daily", ts_code=f"{index:06d}.SZ")
                for index in range(12)
            ]
            for future in futures:
                future.result(timeout=1.0)

        self.assertLessEqual(peak_active, 4)
        self.assertEqual(fetcher._call_count, 12)

    def test_get_daily_data_routes_real_public_path_through_shared_api_wrapper(self) -> None:
        fetcher = self._make_fetcher()
        fetcher._api.daily.return_value = pd.DataFrame(
            {
                "trade_date": ["20260105", "20260102"],
                "open": [10.0, 9.8],
                "high": [10.3, 10.0],
                "low": [9.9, 9.7],
                "close": [10.2, 9.9],
                "vol": [100.0, 80.0],
                "amount": [1000.0, 800.0],
                "pct_chg": [3.0, 1.0],
            }
        )

        with patch.object(fetcher, "_check_rate_limit"), patch.object(
            fetcher,
            "_call_api_with_rate_limit",
            wraps=fetcher._call_api_with_rate_limit,
        ) as api_wrapper:
            result = fetcher.get_daily_data(
                "600519",
                start_date="2026-01-01",
                end_date="2026-01-05",
            )

        self.assertFalse(result.empty)
        api_wrapper.assert_called_once_with(
            "daily",
            ts_code="600519.SH",
            start_date="20260101",
            end_date="20260105",
        )

    def test_quota_wait_does_not_hold_rate_lock_or_shared_api_slots(self) -> None:
        fetcher = self._make_fetcher()
        fetcher.rate_limit_per_minute = 1
        fetcher._minute_start = 1000.0
        fetcher._call_count = 1
        fetcher._api.stock_basic.return_value = pd.DataFrame({"name": ["测试股票"]})

        clock = {"now": 1000.0}
        clock_lock = threading.Lock()
        sleep_started = threading.Event()
        release_sleep = threading.Event()

        def fake_time() -> float:
            with clock_lock:
                return clock["now"]

        def fake_sleep(seconds: float) -> None:
            sleep_started.set()
            release_sleep.wait(timeout=1.0)
            with clock_lock:
                clock["now"] += seconds

        with patch("data_provider.tushare_fetcher.time.time", side_effect=fake_time), patch(
            "data_provider.tushare_fetcher.time.sleep",
            side_effect=fake_sleep,
        ):
            with ThreadPoolExecutor(max_workers=4) as executor:
                futures = [
                    executor.submit(fetcher.get_stock_name, stock_code)
                    for stock_code in ("600519", "600000", "601318", "603288")
                ]
                self.assertTrue(sleep_started.wait(timeout=0.5))
                threading.Event().wait(0.05)

                rate_lock_available = fetcher._rate_limit_lock.acquire(blocking=False)
                if rate_lock_available:
                    fetcher._rate_limit_lock.release()
                api_slot_available = fetcher._api_slots.acquire(blocking=False)
                if api_slot_available:
                    fetcher._api_slots.release()

                release_sleep.set()
                results = [future.result(timeout=1.0) for future in futures]

        self.assertTrue(rate_lock_available)
        self.assertTrue(api_slot_available)
        self.assertEqual(results, ["测试股票"] * 4)
        self.assertEqual(fetcher._api.stock_basic.call_count, 4)
