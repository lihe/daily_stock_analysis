# -*- coding: utf-8 -*-
"""
Contract tests for get_capital_flow tool output semantics.
"""

import os
import sys
import unittest
from threading import BoundedSemaphore
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from src.agent.tools.data_tools import _handle_get_capital_flow
from data_provider.base import DataFetcherManager


class _TushareCapabilityFetcher:
    name = "TushareFetcher"
    priority = 2

    def __init__(self, capital_flow):
        self.capital_flow = capital_flow
        self.capital_flow_timeouts = []

    def is_available_for_request(self, _capability: str) -> bool:
        return True

    def is_available(self) -> bool:
        return True

    def get_capital_flow(self, _stock_code: str, timeout_seconds: float, top_n: int = 5):
        self.capital_flow_timeouts.append((timeout_seconds, top_n))
        return self.capital_flow


class _DummyManagerOk:
    """Returns a well-formed capital flow context."""

    def get_capital_flow_context(self, _stock_code: str):
        return {
            "status": "ok",
            "data": {
                "stock_flow": {
                    "main_net_inflow": 1500000.0,
                    "inflow_5d": 8000000.0,
                    "inflow_10d": 15000000.0,
                },
                "sector_rankings": {
                    "top": [{"name": "白酒", "inflow": 5e8}, {"name": "半导体", "inflow": 3e8}],
                    "bottom": [{"name": "煤炭", "inflow": -2e8}],
                },
            },
            "errors": [],
        }


class _DummyManagerNotSupported:
    """Returns not_supported status (e.g. ETF or HK stock)."""

    def get_capital_flow_context(self, _stock_code: str):
        return {"status": "not_supported"}


class _DummyManagerRaises:
    """Simulates a fetch failure."""

    def get_capital_flow_context(self, _stock_code: str):
        raise RuntimeError("network timeout")


class TestGetCapitalFlowContract(unittest.TestCase):

    def test_tushare_stock_and_sector_fallback_are_independent_and_sector_is_atomic(self) -> None:
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "partial",
            "stock_flow": {
                "trade_date": "2026-08-05",
                "net_mf_amount": 0.0,
                "net_mf_amount_5d": None,
                "amount_unit": "万元",
            },
            "sector_rankings": {
                "top": [{"name": "半导体", "net_amount": 5.0, "amount_unit": "亿元"}],
                "bottom": [],
            },
            "source_chain": ["tushare.moneyflow", "tushare.moneyflow_ind_ths"],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        ak_payload = {
            "status": "partial",
            "stock_flow": {"main_net_inflow": 9.0, "inflow_5d": 20.0, "inflow_10d": 30.0},
            "sector_rankings": {
                "top": [{"name": "白酒", "net_inflow": 99.0}],
                "bottom": [{"name": "煤炭", "net_inflow": -50.0}],
            },
            "source_chain": ["capital_stock:akshare", "capital_sector:akshare"],
            "errors": [],
        }
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager._fundamental_adapter, "get_capital_flow", return_value=ak_payload):
            ctx = manager.get_capital_flow_context("600519", budget_seconds=3.0)

        stock_flow = ctx["data"]["stock_flow"]
        self.assertEqual(stock_flow["net_mf_amount"], 0.0)
        self.assertEqual(stock_flow["main_net_inflow"], 9.0)
        self.assertEqual(stock_flow["inflow_5d"], 20.0)
        self.assertEqual(ctx["data"]["sector_rankings"], {
            "top": [{"name": "半导体", "net_amount": 5.0, "amount_unit": "亿元"}],
            "bottom": [],
        })

    def test_capital_flow_deadline_uses_preferred_slice_and_actual_positive_remainder(self) -> None:
        clock = {"now": 10.0}
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        wrapper_timeouts = []

        def run_with_timeout(task, timeout_seconds, task_name, absolute_deadline=None):
            wrapper_timeouts.append((task_name, timeout_seconds))
            result = task()
            # 首选调用和调度共消耗 0.75 秒；fallback 必须拿到 2.25 秒，而不是固定 1.2 秒。
            if "tushare" in task_name:
                clock["now"] += 0.75
            return result, None, 0

        ak_payload = {
            "status": "partial",
            "stock_flow": {"main_net_inflow": 1.0},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["capital_stock:akshare"],
            "errors": [],
        }
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch.object(manager, "_run_with_timeout", side_effect=run_with_timeout), \
                patch.object(manager._fundamental_adapter, "get_capital_flow", return_value=ak_payload):
            ctx = manager.get_capital_flow_context("600519", budget_seconds=3.0)

        self.assertEqual(ctx["data"]["stock_flow"]["main_net_inflow"], 1.0)
        self.assertAlmostEqual(tushare.capital_flow_timeouts[0][0], 1.8)
        self.assertAlmostEqual(wrapper_timeouts[0][1], 1.8)
        self.assertAlmostEqual(wrapper_timeouts[1][1], 2.25)

    def test_capability_lookup_time_reduces_preferred_adapter_deadline_remainder(self) -> None:
        clock = {"now": 15.0}
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])

        def find_tushare(_capability):
            clock["now"] += 0.5
            return tushare

        def run_with_timeout(task, timeout_seconds, task_name, absolute_deadline=None):
            return task(), None, 0

        ak_payload = {
            "status": "partial",
            "stock_flow": {"main_net_inflow": 1.0},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["capital_stock:akshare"],
            "errors": [],
        }
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch.object(manager, "_find_available_tushare_fetcher", side_effect=find_tushare, create=True), \
                patch.object(manager, "_run_with_timeout", side_effect=run_with_timeout), \
                patch.object(manager._fundamental_adapter, "get_capital_flow", return_value=ak_payload):
            manager.get_capital_flow_context("600519", budget_seconds=3.0)

        self.assertAlmostEqual(tushare.capital_flow_timeouts[0][0], 1.3)

    def test_capital_flow_slow_preferred_call_reduces_fallback_below_reserved_target(self) -> None:
        clock = {"now": 30.0}
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "partial",
            "stock_flow": {"net_mf_amount": 1.0},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["tushare.moneyflow"],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        wrapper_timeouts = []

        def run_with_timeout(task, timeout_seconds, task_name, absolute_deadline=None):
            wrapper_timeouts.append((task_name, timeout_seconds))
            result = task()
            if "tushare" in task_name:
                clock["now"] += 1.9
            return result, None, 0

        ak_payload = {
            "status": "partial",
            "stock_flow": {"main_net_inflow": 2.0},
            "sector_rankings": {"top": [{"name": "白酒", "net_inflow": 3.0}], "bottom": []},
            "source_chain": ["capital_sector:akshare"],
            "errors": [],
        }
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch.object(manager, "_run_with_timeout", side_effect=run_with_timeout), \
                patch.object(manager._fundamental_adapter, "get_capital_flow", return_value=ak_payload):
            ctx = manager.get_capital_flow_context("600519", budget_seconds=3.0)

        self.assertEqual(ctx["data"]["sector_rankings"]["top"][0]["name"], "白酒")
        self.assertAlmostEqual(wrapper_timeouts[1][1], 1.1)

    def test_complete_tushare_net_mf_fields_still_fill_akshare_main_flow_fields(self) -> None:
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "partial",
            "stock_flow": {
                "trade_date": "2026-08-05",
                "net_mf_amount": 1.0,
                "net_mf_amount_5d": 2.0,
                "net_mf_amount_10d": 3.0,
                "amount_unit": "万元",
            },
            "sector_rankings": {
                "top": [{"name": "半导体", "net_amount": 5.0, "amount_unit": "亿元"}],
                "bottom": [],
            },
            "source_chain": ["tushare.moneyflow", "tushare.moneyflow_ind_ths"],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        ak_payload = {
            "status": "partial",
            "stock_flow": {"main_net_inflow": 99.0},
            "sector_rankings": {
                "top": [{"name": "白酒", "net_inflow": 8.0}],
                "bottom": [{"name": "煤炭", "net_inflow": -2.0}],
            },
            "source_chain": ["capital_sector:akshare"],
            "errors": [],
        }
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager._fundamental_adapter, "get_capital_flow", return_value=ak_payload) as ak_call:
            ctx = manager.get_capital_flow_context("600519", budget_seconds=3.0)

        self.assertEqual(ctx["data"]["stock_flow"]["net_mf_amount"], 1.0)
        self.assertEqual(ctx["data"]["stock_flow"]["main_net_inflow"], 99.0)
        self.assertEqual(ctx["data"]["sector_rankings"], tushare.capital_flow["sector_rankings"])
        ak_call.assert_called_once_with("600519")

    def test_worker_pool_exhaustion_records_errors_without_unstarted_providers(self) -> None:
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "partial",
            "stock_flow": {"net_mf_amount": 1.0},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["tushare.moneyflow"],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        manager._fundamental_timeout_slots = BoundedSemaphore(1)
        self.assertTrue(manager._fundamental_timeout_slots.acquire(blocking=False))
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        try:
            with patch("src.config.get_config", return_value=cfg):
                ctx = manager.get_capital_flow_context("600519", budget_seconds=3.0)
        finally:
            manager._fundamental_timeout_slots.release()

        providers = [item["provider"] for item in ctx["source_chain"]]
        self.assertNotIn("tushare_capital_flow", providers)
        self.assertNotIn("akshare_capital_flow", providers)
        self.assertEqual(tushare.capital_flow_timeouts, [])
        self.assertEqual(ctx["status"], "failed")
        self.assertTrue(ctx["errors"])
        self.assertTrue(all("worker pool exhausted" in error for error in ctx["errors"]))

    def test_worker_start_failure_keeps_scheduling_error_without_provider_metadata(self) -> None:
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "partial",
            "stock_flow": {"net_mf_amount": 1.0},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["tushare.moneyflow"],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch("data_provider.base.Thread.start", side_effect=RuntimeError("thread start failed")):
            ctx = manager.get_capital_flow_context("600519", budget_seconds=3.0)

        self.assertEqual(ctx["source_chain"], [])
        self.assertEqual(ctx["errors"], ["thread start failed", "thread start failed"])
        self.assertEqual(tushare.capital_flow_timeouts, [])
        self.assertEqual(ctx["status"], "failed")

    def test_zero_budget_public_capital_flow_fails_without_provider_calls(self) -> None:
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "partial",
            "stock_flow": {"net_mf_amount": 1.0},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["tushare.moneyflow"],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager._fundamental_adapter, "get_capital_flow") as ak_call:
            ctx = manager.get_capital_flow_context("600519", budget_seconds=0.0)

        self.assertEqual(ctx["status"], "failed")
        self.assertEqual(tushare.capital_flow_timeouts, [])
        ak_call.assert_not_called()

    def test_capital_flow_zero_deadline_remainder_skips_akshare_and_fails_open(self) -> None:
        clock = {"now": 20.0}
        tushare = _TushareCapabilityFetcher(capital_flow={
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])

        def run_with_timeout(task, timeout_seconds, _task_name, absolute_deadline=None):
            result = task()
            clock["now"] += 3.1
            return result, None, int(timeout_seconds * 1000)

        cfg = SimpleNamespace(fundamental_fetch_timeout_seconds=3.0, fundamental_retry_max=1)
        with patch("src.config.get_config", return_value=cfg), \
                patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch.object(manager, "_run_with_timeout", side_effect=run_with_timeout), \
                patch.object(manager._fundamental_adapter, "get_capital_flow") as ak_call:
            ctx = manager.get_capital_flow_context("600519", budget_seconds=3.0)

        self.assertEqual(ctx["status"], "failed")
        self.assertEqual(ctx.get("data", {}).get("stock_flow", {}), {})
        ak_call.assert_not_called()

    def test_ok_response_shape(self) -> None:
        """Happy path: key fields are present and values match the source data."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerOk(),
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["main_net_inflow"], 1500000.0)
        self.assertEqual(result["inflow_5d"], 8000000.0)
        self.assertEqual(result["inflow_10d"], 15000000.0)
        self.assertIn("sector_rankings", result)
        self.assertIn("top_inflow_sectors", result["sector_rankings"])
        self.assertIn("top_outflow_sectors", result["sector_rankings"])
        # At most 3 items are returned per ranking list
        self.assertLessEqual(len(result["sector_rankings"]["top_inflow_sectors"]), 3)
        self.assertEqual(result["errors"], [])

    def test_not_supported_for_non_cn_or_etf(self) -> None:
        """ETF / non-CN stocks return status=not_supported with an explanatory note."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerNotSupported(),
        ):
            result = _handle_get_capital_flow("510300")

        self.assertEqual(result["stock_code"], "510300")
        self.assertEqual(result["status"], "not_supported")
        self.assertIn("note", result)

    def test_exception_path_formatting(self) -> None:
        """Fetch errors are caught and returned with status=error."""
        with patch(
            "src.agent.tools.data_tools._get_fetcher_manager",
            return_value=_DummyManagerRaises(),
        ):
            result = _handle_get_capital_flow("600519")

        self.assertEqual(result["stock_code"], "600519")
        self.assertEqual(result["status"], "error")
        self.assertIn("capital flow fetch failed", result["error"])
        self.assertIn("network timeout", result["error"])


if __name__ == "__main__":
    unittest.main()
