# -*- coding: utf-8 -*-
"""
Tests for structured fundamental context (P0).
"""

import json
import logging
import os
import sys
import time
import unittest
from datetime import datetime
from threading import BoundedSemaphore, Event, Thread
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from data_provider.base import DataFetcherManager


class _DummyFetcher:
    def __init__(self, name: str, priority: int, rankings=None):
        self.name = name
        self.priority = priority
        self._rankings = rankings

    def get_sector_rankings(self, _n: int = 5):
        return self._rankings


class _DummyBoardFetcher:
    def __init__(self, name: str, priority: int, boards=None):
        self.name = name
        self.priority = priority
        self._boards = boards or []

    def get_belong_board(self, _stock_code: str):
        return self._boards


class _TushareCapabilityFetcher:
    name = "TushareFetcher"
    priority = 2

    def __init__(self, bundle=None, capital_flow=None, available: bool = True):
        self.bundle = bundle
        self.capital_flow = capital_flow
        self.available = available
        self.bundle_timeouts = []
        self.capital_flow_timeouts = []

    def is_available_for_request(self, _capability: str) -> bool:
        return self.available

    def is_available(self) -> bool:
        return self.available

    def get_fundamental_bundle(self, _stock_code: str, timeout_seconds: float):
        self.bundle_timeouts.append(timeout_seconds)
        if callable(self.bundle):
            return self.bundle(timeout_seconds)
        if isinstance(self.bundle, Exception):
            raise self.bundle
        return self.bundle

    def get_capital_flow(self, _stock_code: str, timeout_seconds: float, top_n: int = 5):
        self.capital_flow_timeouts.append((timeout_seconds, top_n))
        if isinstance(self.capital_flow, Exception):
            raise self.capital_flow
        return self.capital_flow


def _manager_config(timeout: float = 3.0):
    return SimpleNamespace(
        enable_fundamental_pipeline=True,
        fundamental_cache_ttl_seconds=0,
        fundamental_cache_max_entries=0,
        fundamental_stage_timeout_seconds=timeout,
        fundamental_fetch_timeout_seconds=timeout,
        fundamental_retry_max=1,
    )


def _empty_bundle():
    return {
        "status": "not_supported",
        "growth": {},
        "earnings": {},
        "institution": {},
        "source_chain": [],
        "errors": [],
    }


class TestFundamentalContext(unittest.TestCase):
    def test_cn_fundamental_context_logs_sanitized_source_evidence(self) -> None:
        tushare = _TushareCapabilityFetcher(bundle={
            "status": "partial",
            "growth": {"revenue_yoy": 8.0},
            "earnings": {"financial_report": {"revenue": 123456789.0}},
            "institution": {
                "top10_holder_snapshot": {"holders": [{"holder_name": "敏感股东名称"}]},
                "top10_holder_change": 0.0,
            },
            "source_chain": ["tushare.fina_indicator", "tushare.income"],
            "errors": [
                "income:TimeoutError: Token SECRET_TOKEN https://api.example.test/raw?token=SECRET_TOKEN"
            ],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        capital_flow = manager._build_fundamental_block(
            "failed",
            {},
            [{"provider": "tushare.moneyflow", "result": "failed", "duration_ms": 7}],
            ["ConnectionError: private upstream host and raw response body"],
        )
        dragon_tiger = manager._build_fundamental_block("not_supported")
        boards = manager._build_fundamental_block(
            "partial",
            {},
            [{
                "provider": "boards:akshare_stock_board_industry_name_em",
                "result": "partial",
                "duration_ms": 3,
            }],
            ["arbitrary business error value 998877"],
        )

        with patch("src.config.get_config", return_value=_manager_config()), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch.object(manager, "get_capital_flow_context", return_value=capital_flow), \
                patch.object(manager, "get_dragon_tiger_context", return_value=dragon_tiger), \
                patch.object(manager, "get_board_context", return_value=boards), \
                self.assertLogs("data_provider.base", level="INFO") as logs:
            context = manager.get_fundamental_context("SH600519", budget_seconds=3.0)

        evidence_records = [
            record
            for record in logs.records
            if record.getMessage().startswith("[DataSourceEvidence] ")
        ]
        self.assertEqual(len(evidence_records), 1)
        message = evidence_records[0].getMessage()
        self.assertNotIn("\r", message)
        self.assertNotIn("\n", message)
        serialized = message.split("[DataSourceEvidence] ", 1)[1]
        evidence = json.loads(serialized)
        self.assertEqual(evidence["stock_code"], "600519")
        self.assertEqual(evidence["overall_status"], context["status"])
        self.assertEqual(evidence["coverage"], context["coverage"])
        self.assertEqual(evidence["blocks"]["growth"]["status"], "ok")
        self.assertEqual(
            evidence["blocks"]["growth"]["provider_endpoints"],
            [
                {"name": "tushare.fina_indicator", "result": "partial"},
                {"name": "tushare.income", "result": "partial"},
            ],
        )
        self.assertEqual(evidence["blocks"]["growth"]["error_types"], ["timeout"])
        self.assertEqual(evidence["blocks"]["capital_flow"]["error_types"], ["connection"])
        self.assertEqual(evidence["blocks"]["boards"]["error_types"], ["provider_error"])

        for secret in (
            "SECRET_TOKEN",
            "Token",
            "https://",
            "raw response body",
            "敏感股东名称",
            "123456789",
            "998877",
        ):
            self.assertNotIn(secret, serialized)
        self.assertNotIn("duration_ms", serialized)

    def test_cn_fundamental_context_cache_hit_logs_source_evidence_without_mutating_context(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = _manager_config()
        cfg.fundamental_cache_ttl_seconds = 120
        cfg.fundamental_cache_max_entries = 8
        cached_context = {
            "market": "cn",
            "valuation": manager._build_fundamental_block(
                "ok",
                {"pe_ratio": 998877.0},
                [{"provider": "realtime_quote", "result": "ok", "duration_ms": 1}],
            ),
            "growth": manager._build_fundamental_block(
                "partial",
                {"revenue_yoy": 7654321.0},
                [{"provider": "growth:akshare_financial_analysis", "result": "partial", "duration_ms": 2}],
                ["RuntimeError: Token CACHE_SECRET https://cache.example.test/raw"],
            ),
            "earnings": manager._build_fundamental_block("not_supported"),
            "institution": manager._build_fundamental_block("not_supported"),
            "capital_flow": manager._build_fundamental_block("not_supported"),
            "dragon_tiger": manager._build_fundamental_block("not_supported"),
            "boards": manager._build_fundamental_block("not_supported"),
        }
        manager._refresh_fundamental_context_metadata(cached_context, is_etf=False)
        cache_key = manager._get_fundamental_cache_key("600519", 3.0)
        manager._fundamental_cache[cache_key] = {"ts": time.time(), "context": cached_context}

        original_builder = DataFetcherManager._build_fundamental_source_evidence
        builder_lock_states = []

        def build_and_record_lock_state(stock_code, context):
            builder_lock_states.append(manager._fundamental_cache_lock._is_owned())
            return original_builder(stock_code, context)

        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote") as fetch_quote, \
                patch.object(
                    DataFetcherManager,
                    "_build_fundamental_source_evidence",
                    side_effect=build_and_record_lock_state,
                ), \
                self.assertLogs("data_provider.base", level="INFO") as logs:
            context = manager.get_fundamental_context("600519", budget_seconds=3.0)

        fetch_quote.assert_not_called()
        self.assertIs(context, cached_context)
        self.assertEqual(builder_lock_states, [False])
        evidence_records = [
            record
            for record in logs.records
            if record.getMessage().startswith("[DataSourceEvidence] ")
        ]
        self.assertEqual(len(evidence_records), 1)
        message = evidence_records[0].getMessage()
        self.assertNotIn("\r", message)
        self.assertNotIn("\n", message)
        serialized = message.split("[DataSourceEvidence] ", 1)[1]
        evidence = json.loads(serialized)
        self.assertEqual(evidence["stock_code"], "600519")
        self.assertEqual(evidence["overall_status"], "partial")
        self.assertEqual(
            evidence["blocks"]["growth"]["provider_endpoints"],
            [{"name": "growth:akshare_financial_analysis", "result": "partial"}],
        )
        self.assertEqual(evidence["blocks"]["growth"]["error_types"], ["provider_error"])
        for secret in ("CACHE_SECRET", "Token", "https://", "998877", "7654321"):
            self.assertNotIn(secret, serialized)

    def test_failed_cn_context_logs_exactly_one_sanitized_evidence_record(self) -> None:
        manager = DataFetcherManager(fetchers=[])

        with self.assertLogs("data_provider.base", level="INFO") as logs:
            context = manager.build_failed_fundamental_context(
                "SH600519\r\n",
                "RuntimeError:\r\nToken FAILED_SECRET https://private.example/raw response=998877",
            )

        self.assertEqual(context["status"], "failed")
        evidence_records = [
            record
            for record in logs.records
            if record.getMessage().startswith("[DataSourceEvidence] ")
        ]
        self.assertEqual(len(evidence_records), 1)
        message = evidence_records[0].getMessage()
        self.assertNotIn("\r", message)
        self.assertNotIn("\n", message)
        evidence = json.loads(message.split("[DataSourceEvidence] ", 1)[1])
        self.assertEqual(evidence["stock_code"], "600519")
        self.assertEqual(evidence["overall_status"], "failed")
        self.assertEqual(evidence["blocks"]["valuation"]["error_types"], ["failed"])
        for secret in ("FAILED_SECRET", "Token", "https://", "raw response", "998877"):
            self.assertNotIn(secret, message)

    def test_disabled_cn_context_logs_exactly_one_evidence_record(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = _manager_config()
        cfg.enable_fundamental_pipeline = False

        with patch("src.config.get_config", return_value=cfg), \
                self.assertLogs("data_provider.base", level="INFO") as logs:
            context = manager.get_fundamental_context("600519")

        self.assertEqual(context["status"], "not_supported")
        evidence_records = [
            record
            for record in logs.records
            if record.getMessage().startswith("[DataSourceEvidence] ")
        ]
        self.assertEqual(len(evidence_records), 1)
        self.assertNotIn("\r", evidence_records[0].getMessage())
        self.assertNotIn("\n", evidence_records[0].getMessage())

    def test_evidence_serialization_is_deterministic_and_sanitizes_untrusted_inputs(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        context = {
            "status": "failed",
            "coverage": {"valuation": "failed"},
            "valuation": manager._build_fundamental_block(
                "failed",
                {},
                [{
                    "provider": "provider\r\nToken PROVIDER_SECRET https://private.example/raw",
                    "result": "failed",
                    "duration_ms": 1,
                }],
                ["TimeoutError:\r\nToken ERROR_SECRET https://private.example/raw response=7654321"],
            ),
        }

        with self.assertLogs("data_provider.base", level="INFO") as logs:
            manager._emit_fundamental_source_evidence(
                "600519\r\nToken STOCK_SECRET https://private.example/raw",
                context,
            )
            manager._emit_fundamental_source_evidence(
                "600519\r\nToken STOCK_SECRET https://private.example/raw",
                context,
            )

        messages = [
            record.getMessage()
            for record in logs.records
            if record.getMessage().startswith("[DataSourceEvidence] ")
        ]
        self.assertEqual(len(messages), 2)
        self.assertEqual(messages[0], messages[1])
        evidence = json.loads(messages[0].split("[DataSourceEvidence] ", 1)[1])
        self.assertEqual(evidence["stock_code"], "redacted")
        self.assertEqual(evidence["blocks"]["valuation"]["provider_endpoints"], [])
        self.assertEqual(evidence["blocks"]["valuation"]["error_types"], ["timeout"])
        for message in messages:
            self.assertNotIn("\r", message)
            self.assertNotIn("\n", message)
            for secret in (
                "PROVIDER_SECRET",
                "ERROR_SECRET",
                "STOCK_SECRET",
                "Token",
                "https://",
                "raw response",
                "7654321",
            ):
                self.assertNotIn(secret, message)

    def test_evidence_serialization_is_skipped_when_info_logging_is_disabled(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        context = manager.build_failed_fundamental_context("159915", "not logged for ETF")
        original_builder = DataFetcherManager._build_fundamental_source_evidence

        with patch("data_provider.base.logger.isEnabledFor", return_value=False) as is_enabled, \
                patch.object(
                    DataFetcherManager,
                    "_build_fundamental_source_evidence",
                    wraps=original_builder,
                ) as builder:
            result = manager._emit_fundamental_source_evidence("600519", context)

        self.assertIsNone(result)
        is_enabled.assert_called_once_with(logging.INFO)
        builder.assert_not_called()

    def test_no_tushare_fetcher_keeps_akshare_bundle_and_capital_flow_routes(self) -> None:
        manager = DataFetcherManager(fetchers=[_DummyFetcher("AkshareFetcher", priority=1)])
        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        ak_bundle = {
            "status": "partial",
            "growth": {"revenue_yoy": 8.0},
            "earnings": {"forecast_summary": "预增"},
            "institution": {"top10_holder_change": 1.0},
            "source_chain": ["growth:akshare"],
            "errors": [],
        }
        ak_capital = {
            "status": "partial",
            "stock_flow": {"main_net_inflow": 3.0},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": ["capital_stock:akshare"],
            "errors": [],
        }
        with patch("src.config.get_config", return_value=_manager_config()), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch.object(manager._fundamental_adapter, "get_fundamental_bundle", return_value=ak_bundle) as ak_bundle_call, \
                patch.object(manager._fundamental_adapter, "get_capital_flow", return_value=ak_capital) as ak_flow_call, \
                patch.object(manager._fundamental_adapter, "get_dragon_tiger_flag", return_value={
                    "status": "not_supported", "source_chain": [], "errors": []
                }) as dragon_call, \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=3.0)

        self.assertEqual(ctx["growth"]["data"]["revenue_yoy"], 8.0)
        self.assertEqual(ctx["capital_flow"]["data"]["stock_flow"]["main_net_inflow"], 3.0)
        ak_bundle_call.assert_called_once_with("600519")
        ak_flow_call.assert_called_once_with("600519")
        dragon_call.assert_called_once_with("600519")

    def test_complete_tushare_bundle_suppresses_akshare(self) -> None:
        tushare = _TushareCapabilityFetcher(bundle={
            "status": "partial",
            "growth": {"revenue_yoy": 0.0, "profitable": False},
            "earnings": {"financial_report": {"report_date": "2026-06-30", "revenue": 1.0}},
            "institution": {
                "top10_holder_snapshot": {"holders": [{"holder_name": "股东甲"}]},
                "top10_holder_change": 0.0,
            },
            "source_chain": ["tushare.fina_indicator"],
            "errors": [],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        with patch("src.config.get_config", return_value=_manager_config()), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch.object(manager._fundamental_adapter, "get_fundamental_bundle") as ak_bundle_call, \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=3.0)

        self.assertEqual(ctx["growth"]["data"], {"revenue_yoy": 0.0, "profitable": False})
        self.assertEqual(ctx["institution"]["data"]["top10_holder_snapshot"]["holders"][0]["holder_name"], "股东甲")
        ak_bundle_call.assert_not_called()

    def test_partial_tushare_bundle_recursively_fills_only_missing_values(self) -> None:
        report_date = datetime(2026, 6, 30).date()
        tushare = _TushareCapabilityFetcher(bundle={
            "status": "partial",
            "growth": {
                "revenue_yoy": 0.0,
                "profitable": False,
                "report_date": report_date,
                "labels": ["累计"],
                "empty_labels": [],
                "net_profit_yoy": None,
            },
            "earnings": {"financial_report": {"revenue": None}},
            "institution": {
                "top10_holder_snapshot": {"holders": [{"holder_name": "股东甲"}]},
                "top10_holder_change": None,
            },
            "source_chain": ["tushare.fina_indicator", "tushare.top10_holders"],
            "errors": ["income:TimeoutError"],
        })
        manager = DataFetcherManager(fetchers=[tushare])
        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        ak_bundle = {
            "status": "partial",
            "growth": {
                "revenue_yoy": 99.0,
                "profitable": True,
                "report_date": "2025-12-31",
                "labels": ["单季"],
                "empty_labels": ["由 AkShare 补齐"],
                "net_profit_yoy": 7.5,
            },
            "earnings": {"financial_report": {"revenue": 123.0}},
            "institution": {
                "top10_holder_snapshot": {"holders": [{"holder_name": "股东乙"}]},
                "top10_holder_change": -1.5,
            },
            "source_chain": ["growth:akshare", "top10:akshare"],
            "errors": ["akshare_partial"],
        }
        with patch("src.config.get_config", return_value=_manager_config()), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch.object(manager._fundamental_adapter, "get_fundamental_bundle", return_value=ak_bundle), \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=3.0)

        growth = ctx["growth"]["data"]
        self.assertEqual(growth["revenue_yoy"], 0.0)
        self.assertIs(growth["profitable"], False)
        self.assertEqual(growth["report_date"], report_date)
        self.assertEqual(growth["labels"], ["累计"])
        self.assertEqual(growth["empty_labels"], ["由 AkShare 补齐"])
        self.assertEqual(growth["net_profit_yoy"], 7.5)
        institution = ctx["institution"]["data"]
        self.assertEqual(institution["top10_holder_snapshot"]["holders"], [{"holder_name": "股东甲"}])
        self.assertEqual(institution["top10_holder_change"], -1.5)
        providers = [item["provider"] for item in ctx["growth"]["source_chain"]]
        self.assertIn("tushare.fina_indicator", providers)
        self.assertIn("growth:akshare", providers)
        self.assertIn("income:TimeoutError", ctx["growth"]["errors"])
        self.assertIn("akshare_partial", ctx["growth"]["errors"])

    def test_empty_or_error_tushare_bundle_falls_back_to_akshare(self) -> None:
        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        ak_bundle = {
            "status": "partial",
            "growth": {"revenue_yoy": 8.0},
            "earnings": {},
            "institution": {},
            "source_chain": ["growth:akshare"],
            "errors": [],
        }
        for preferred in (_empty_bundle(), RuntimeError("tushare unavailable")):
            with self.subTest(preferred=type(preferred).__name__):
                manager = DataFetcherManager(fetchers=[_TushareCapabilityFetcher(bundle=preferred)])
                with patch("src.config.get_config", return_value=_manager_config()), \
                        patch.object(manager, "get_realtime_quote", return_value=quote), \
                        patch.object(manager._fundamental_adapter, "get_fundamental_bundle", return_value=ak_bundle) as ak_call, \
                        patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                        patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                        patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
                    ctx = manager.get_fundamental_context("600519", budget_seconds=3.0)

                self.assertEqual(ctx["growth"]["data"]["revenue_yoy"], 8.0)
                ak_call.assert_called_once_with("600519")

    def test_etf_and_offshore_routes_do_not_call_tushare_capabilities(self) -> None:
        tushare = _TushareCapabilityFetcher(bundle=_empty_bundle())
        manager = DataFetcherManager(fetchers=[tushare])
        etf_quote = SimpleNamespace(pe_ratio=None, pb_ratio=None, total_mv=1.0, circ_mv=1.0)
        offshore_bundle = {
            "status": "not_supported", "growth": {}, "earnings": {},
            "belong_boards": [], "source_chain": [], "errors": [],
        }
        with patch("src.config.get_config", return_value=_manager_config()), \
                patch.object(manager, "get_realtime_quote", return_value=etf_quote), \
                patch.object(manager._fundamental_adapter, "get_fundamental_bundle", return_value=_empty_bundle()), \
                patch.object(manager._yfinance_fundamental_adapter, "get_fundamental_bundle", return_value=offshore_bundle):
            manager.get_fundamental_context("159915", realtime_quote=SimpleNamespace(pe_ratio=99.0))
            manager.get_fundamental_context("AAPL")

        self.assertEqual(tushare.bundle_timeouts, [])
        self.assertEqual(tushare.capital_flow_timeouts, [])

    def test_fundamental_bundle_uses_same_deadline_for_tushare_and_akshare(self) -> None:
        clock = {"now": 40.0}
        tushare = _TushareCapabilityFetcher(bundle=_empty_bundle())
        manager = DataFetcherManager(fetchers=[tushare])
        wrapper_timeouts = []

        def run_with_timeout(task, timeout_seconds, task_name, absolute_deadline=None):
            wrapper_timeouts.append((task_name, timeout_seconds))
            result = task()
            if "tushare" in task_name:
                clock["now"] += 0.5
            return result, None, 0

        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        ak_bundle = {
            "status": "partial",
            "growth": {"revenue_yoy": 8.0},
            "earnings": {},
            "institution": {},
            "source_chain": ["growth:akshare"],
            "errors": [],
        }
        with patch("src.config.get_config", return_value=_manager_config()), \
                patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch.object(manager, "_run_with_timeout", side_effect=run_with_timeout), \
                patch.object(manager._fundamental_adapter, "get_fundamental_bundle", return_value=ak_bundle), \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=3.0, realtime_quote=quote)

        self.assertEqual(ctx["growth"]["data"]["revenue_yoy"], 8.0)
        self.assertAlmostEqual(tushare.bundle_timeouts[0], 1.8)
        self.assertAlmostEqual(wrapper_timeouts[0][1], 1.8)
        self.assertAlmostEqual(wrapper_timeouts[1][1], 2.5)

    def test_fundamental_bundle_lock_timeout_never_calls_tushare_after_return(self) -> None:
        late_call = Event()

        def preferred_bundle(_timeout_seconds):
            late_call.set()
            return _empty_bundle()

        tushare = _TushareCapabilityFetcher(bundle=preferred_bundle)
        manager = DataFetcherManager(fetchers=[tushare])
        fetcher_lock = manager._get_fetcher_call_lock(tushare)
        lock_held = Event()
        release_lock = Event()
        wrapper_timeouts = []
        real_run_with_timeout = manager._run_with_timeout

        def hold_preferred_lock() -> None:
            with fetcher_lock:
                lock_held.set()
                release_lock.wait(timeout=2.0)

        def record_timeout(task, timeout_seconds, task_name, absolute_deadline=None):
            wrapper_timeouts.append((task_name, timeout_seconds))
            return real_run_with_timeout(
                task,
                timeout_seconds,
                task_name,
                absolute_deadline=absolute_deadline,
            )

        fallback_payload = {
            "status": "partial",
            "growth": {"revenue_yoy": 8.0},
            "earnings": {},
            "institution": {},
            "source_chain": ["growth:akshare"],
            "errors": [],
        }
        holder = Thread(target=hold_preferred_lock, daemon=True)
        holder.start()
        self.assertTrue(lock_held.wait(timeout=1.0))

        try:
            with patch("src.config.get_config", return_value=_manager_config(timeout=0.25)), \
                    patch.object(manager, "_run_with_timeout", side_effect=record_timeout), \
                    patch.object(
                        manager._fundamental_adapter,
                        "get_fundamental_bundle",
                        return_value=fallback_payload,
                    ):
                payload, _ = manager._get_ordered_fundamental_bundle("600519", 0.25)
            calls_at_return = len(tushare.bundle_timeouts)
        finally:
            release_lock.set()
            holder.join(timeout=1.0)

        called_after_release = late_call.wait(timeout=0.2)
        providers = [item["provider"] for item in payload["source_chain"]]
        fallback_timeouts = [
            timeout
            for task_name, timeout in wrapper_timeouts
            if task_name == "akshare_fundamental_bundle"
        ]
        self.assertEqual(calls_at_return, 0)
        self.assertFalse(called_after_release)
        self.assertNotIn("tushare_fundamental_bundle", providers)
        self.assertIn("growth:akshare", providers)
        self.assertTrue(any("timeout" in error for error in payload["errors"]))
        self.assertEqual(len(fallback_timeouts), 1)
        self.assertGreater(fallback_timeouts[0], 0.0)
        self.assertLess(fallback_timeouts[0], 0.25)

    def test_fundamental_bundle_recomputes_tushare_timeout_after_lock_acquisition(self) -> None:
        complete_bundle = {
            "status": "partial",
            "growth": {"revenue_yoy": 8.0},
            "earnings": {"financial_report": {"revenue": 1.0}},
            "institution": {"top10_holder_change": 0.0},
            "source_chain": ["tushare.fina_indicator"],
            "errors": [],
        }
        tushare = _TushareCapabilityFetcher(bundle=complete_bundle)
        manager = DataFetcherManager(fetchers=[tushare])
        fetcher_lock = manager._get_fetcher_call_lock(tushare)
        lock_held = Event()
        release_lock = Event()
        call_started = Event()
        wrapper_timeouts = []
        real_run_with_timeout = manager._run_with_timeout

        def hold_preferred_lock() -> None:
            with fetcher_lock:
                lock_held.set()
                release_lock.wait(timeout=2.0)

        def release_after_wait() -> None:
            if call_started.wait(timeout=1.0):
                time.sleep(0.1)
            release_lock.set()

        def record_timeout(task, timeout_seconds, task_name, absolute_deadline=None):
            wrapper_timeouts.append((task_name, timeout_seconds))
            return real_run_with_timeout(
                task,
                timeout_seconds,
                task_name,
                absolute_deadline=absolute_deadline,
            )

        holder = Thread(target=hold_preferred_lock, daemon=True)
        releaser = Thread(target=release_after_wait, daemon=True)
        holder.start()
        self.assertTrue(lock_held.wait(timeout=1.0))
        releaser.start()
        try:
            call_started.set()
            with patch("src.config.get_config", return_value=_manager_config(timeout=1.0)), \
                    patch.object(manager, "_run_with_timeout", side_effect=record_timeout), \
                    patch.object(manager._fundamental_adapter, "get_fundamental_bundle") as ak_call:
                payload, _ = manager._get_ordered_fundamental_bundle("600519", 1.0)
        finally:
            release_lock.set()
            holder.join(timeout=1.0)
            releaser.join(timeout=1.0)

        preferred_timeout = next(
            timeout
            for task_name, timeout in wrapper_timeouts
            if task_name == "tushare_fundamental_bundle"
        )
        injected_timeout = tushare.bundle_timeouts[0]
        self.assertEqual(payload["growth"]["revenue_yoy"], 8.0)
        self.assertGreater(injected_timeout, 0.0)
        self.assertLess(injected_timeout, preferred_timeout - 0.05)
        ak_call.assert_not_called()

    def test_fundamental_bundle_retry_passes_decreasing_preferred_deadline_remainder(self) -> None:
        clock = {"now": 50.0}
        calls = {"count": 0}
        complete_bundle = {
            "status": "partial",
            "growth": {"revenue_yoy": 1.0},
            "earnings": {"financial_report": {"revenue": 2.0}},
            "institution": {"top10_holder_change": 0.0},
            "source_chain": ["tushare.fina_indicator"],
            "errors": [],
        }

        def bundle_result(_timeout_seconds):
            calls["count"] += 1
            if calls["count"] == 1:
                raise RuntimeError("retryable")
            return complete_bundle

        tushare = _TushareCapabilityFetcher(bundle=bundle_result)
        manager = DataFetcherManager(fetchers=[tushare])
        wrapper_timeouts = []

        def run_with_timeout(task, timeout_seconds, task_name, absolute_deadline=None):
            wrapper_timeouts.append((task_name, timeout_seconds))
            try:
                result, err = task(), None
            except RuntimeError as exc:
                result, err = None, str(exc)
            clock["now"] += 0.6
            return result, err, 600

        cfg = _manager_config()
        cfg.fundamental_retry_max = 2
        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        with patch("src.config.get_config", return_value=cfg), \
                patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch.object(manager, "_run_with_timeout", side_effect=run_with_timeout), \
                patch.object(manager._fundamental_adapter, "get_fundamental_bundle") as ak_call, \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=3.0, realtime_quote=quote)

        self.assertEqual(ctx["growth"]["data"]["revenue_yoy"], 1.0)
        self.assertEqual(len(tushare.bundle_timeouts), 2)
        self.assertAlmostEqual(tushare.bundle_timeouts[0], 1.8)
        self.assertAlmostEqual(tushare.bundle_timeouts[1], 1.2)
        self.assertAlmostEqual(wrapper_timeouts[1][1], 1.2)
        ak_call.assert_not_called()

    def test_run_with_timeout_counts_wall_clock_rollback_and_scheduling_against_monotonic_deadline(self) -> None:
        manager = DataFetcherManager(fetchers=[_DummyFetcher("AkshareFetcher", priority=1)])
        clock = {"now": 10.0}
        join_timeouts = []

        class ImmediateThread:
            def __init__(self, target, daemon, name):
                self._target = target

            def start(self):
                clock["now"] += 0.4
                self._target()

            def join(self, timeout):
                join_timeouts.append(timeout)

            def is_alive(self):
                return False

        with patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("data_provider.base.time.time", side_effect=[100.0, 90.0]), \
                patch("data_provider.base.Thread", ImmediateThread):
            result, err, duration_ms = manager._run_with_timeout(
                lambda: "ok",
                1.0,
                "monotonic_deadline",
                absolute_deadline=11.0,
            )

        self.assertEqual(result, "ok")
        self.assertIsNone(err)
        self.assertEqual(duration_ms, 400)
        self.assertAlmostEqual(join_timeouts[0], 0.6)

    def test_run_with_timeout_rejects_result_completed_after_deadline_during_thread_start(self) -> None:
        manager = DataFetcherManager(fetchers=[_DummyFetcher("AkshareFetcher", priority=1)])
        clock = {"now": 20.0}

        class DelayedStartThread:
            def __init__(self, target, daemon, name):
                self._target = target

            def start(self):
                clock["now"] += 1.1
                self._target()

            def join(self, timeout):
                return None

            def is_alive(self):
                return False

        with patch("data_provider.base.time.monotonic", side_effect=lambda: clock["now"]), \
                patch("data_provider.base.Thread", DelayedStartThread):
            result, err, duration_ms = manager._run_with_timeout(
                lambda: "late result",
                1.0,
                "late_start",
                absolute_deadline=21.0,
            )

        self.assertIsNone(result)
        self.assertIn("timeout", err or "")
        self.assertEqual(duration_ms, 1100)

    def test_duplicate_tushare_names_scan_to_first_available_capability_instance(self) -> None:
        available = _TushareCapabilityFetcher(bundle={
            "status": "partial",
            "growth": {"revenue_yoy": 4.0},
            "earnings": {"financial_report": {"revenue": 2.0}},
            "institution": {"top10_holder_change": 0.0},
            "source_chain": ["tushare.fina_indicator"],
            "errors": [],
        })
        available.priority = 1
        unavailable = _TushareCapabilityFetcher(bundle=RuntimeError("must not run"), available=False)
        unavailable.priority = 2
        manager = DataFetcherManager(fetchers=[available, unavailable])
        quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0, circ_mv=1.0)
        with patch("src.config.get_config", return_value=_manager_config()), \
                patch.object(manager._fundamental_adapter, "get_fundamental_bundle") as ak_call, \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=3.0, realtime_quote=quote)

        self.assertEqual(ctx["growth"]["data"]["revenue_yoy"], 4.0)
        self.assertEqual(len(available.bundle_timeouts), 1)
        self.assertEqual(unavailable.bundle_timeouts, [])
        ak_call.assert_not_called()

    def test_offshore_market_returns_not_supported_when_adapter_empty(self) -> None:
        """When yfinance adapter has no data, offshore (US/HK) status is not_supported.

        capital_flow / dragon_tiger / boards stay not_supported regardless of
        adapter outcome since yfinance has no equivalent feed for those blocks.
        """
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        empty_bundle = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "belong_boards": [],
            "source_chain": [],
            "errors": [],
        }
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=None), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=empty_bundle,
                ):
            ctx = manager.get_fundamental_context("AAPL")
        self.assertEqual(ctx["market"], "us")
        self.assertEqual(ctx["status"], "not_supported")
        self.assertEqual(ctx["coverage"].get("growth"), "not_supported")
        self.assertEqual(ctx["coverage"].get("earnings"), "not_supported")
        self.assertEqual(ctx["coverage"].get("capital_flow"), "not_supported")
        self.assertEqual(ctx["coverage"].get("dragon_tiger"), "not_supported")
        self.assertEqual(ctx["coverage"].get("boards"), "not_supported")
        self.assertEqual(ctx.get("belong_boards"), [])

    def test_offshore_market_populates_blocks_when_adapter_has_data(self) -> None:
        """US/HK fundamental context surfaces yfinance bundle into growth/earnings/belong_boards."""
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=2.0,
            fundamental_fetch_timeout_seconds=1.5,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            pe_ratio=32.5,
            pb_ratio=58.2,
            total_mv=3.4e12,
            circ_mv=3.4e12,
            source=SimpleNamespace(value="longbridge"),
        )
        bundle = {
            "status": "partial",
            "growth": {
                "revenue_yoy": 16.5,
                "net_profit_yoy": 19.3,
                "roe": 141.4,
                "gross_margin": 47.8,
            },
            "earnings": {
                "financial_report": {
                    "report_date": "2026-03-31",
                    "revenue": 1.11e11,
                    "net_profit_parent": 2.95e10,
                    "operating_cash_flow": 2.87e10,
                    "roe": 141.4,
                    "currency": "USD",
                },
                "dividend": {
                    "events": [{
                        "event_date": "2026-05-11",
                        "ex_dividend_date": "2026-05-11",
                        "cash_dividend_per_share": 0.27,
                        "is_pre_tax": True,
                    }],
                    "ttm_event_count": 4,
                    "ttm_cash_dividend_per_share": 1.05,
                    "ttm_dividend_yield_pct": 0.36,
                },
            },
            "belong_boards": [
                {"name": "Technology", "type": "行业"},
                {"name": "Consumer Electronics", "type": "概念"},
            ],
            "source_chain": ["growth:yfinance.info"],
            "errors": [],
        }
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch(
                    "data_provider.yfinance_fundamental_adapter.YfinanceFundamentalAdapter.get_fundamental_bundle",
                    return_value=bundle,
                ):
            ctx = manager.get_fundamental_context("AAPL")
        self.assertEqual(ctx["market"], "us")
        # Offshore status only considers valuation/growth/earnings (capital_flow
        # etc. are intentionally not_supported); "ok" when all three populate.
        self.assertEqual(ctx["status"], "ok")
        self.assertEqual(ctx["coverage"].get("growth"), "ok")
        self.assertEqual(ctx["coverage"].get("earnings"), "ok")
        self.assertEqual(ctx["coverage"].get("capital_flow"), "not_supported")
        self.assertEqual(ctx["coverage"].get("boards"), "not_supported")
        growth_data = ctx["growth"].get("data") or {}
        self.assertEqual(growth_data.get("revenue_yoy"), 16.5)
        self.assertEqual(growth_data.get("roe"), 141.4)
        financial_report = (ctx["earnings"].get("data") or {}).get("financial_report") or {}
        self.assertEqual(financial_report.get("currency"), "USD")
        self.assertEqual(financial_report.get("revenue"), 1.11e11)
        dividend = (ctx["earnings"].get("data") or {}).get("dividend") or {}
        self.assertEqual(dividend.get("ttm_cash_dividend_per_share"), 1.05)
        self.assertEqual(dividend.get("ttm_dividend_yield_pct"), 0.36)
        self.assertEqual(ctx.get("belong_boards"), [
            {"name": "Technology", "type": "行业"},
            {"name": "Consumer Electronics", "type": "概念"},
        ])

    def test_etf_market_downgrades_to_partial_or_not_supported(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            pe_ratio=None,
            pb_ratio=None,
            total_mv=5.0e10,
            circ_mv=4.0e10,
            source=SimpleNamespace(value="tencent"),
        )
        # Mock get_fundamental_bundle so growth/earnings/institution are not_supported (no network).
        bundle = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch(
                    "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle",
                    return_value=bundle,
                ):
            ctx = manager.get_fundamental_context("159915")
        self.assertEqual(ctx["market"], "cn")
        self.assertIn(ctx["status"], ("partial", "not_supported"))
        self.assertEqual(ctx["coverage"].get("valuation"), "ok")
        self.assertEqual(ctx["coverage"].get("growth"), "not_supported")
        self.assertEqual(ctx["coverage"].get("earnings"), "not_supported")
        self.assertEqual(ctx["coverage"].get("institution"), "not_supported")
        self.assertEqual(ctx["coverage"].get("capital_flow"), "not_supported")
        self.assertEqual(ctx["coverage"].get("dragon_tiger"), "not_supported")
        self.assertEqual(ctx["coverage"].get("boards"), "not_supported")

    def test_sector_rankings_use_ordered_fallback(self) -> None:
        akshare = _DummyFetcher("AkshareFetcher", priority=5, rankings=None)
        tushare = _DummyFetcher(
            "TushareFetcher",
            priority=1,
            rankings=([{"name": "半导体", "change_pct": 1.0}], [{"name": "消费", "change_pct": -1.0}]),
        )
        efinance = _DummyFetcher(
            "EfinanceFetcher",
            priority=0,
            rankings=([{"name": "地产", "change_pct": 2.0}], [{"name": "煤炭", "change_pct": -2.0}]),
        )
        manager = DataFetcherManager(fetchers=[efinance, tushare, akshare])
        top, bottom = manager.get_sector_rankings(1)
        self.assertEqual(top[0]["name"], "地产")
        self.assertEqual(bottom[0]["name"], "煤炭")

    def test_fundamental_context_aggregates_blocks(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            pe_ratio=12.3,
            pb_ratio=2.1,
            total_mv=1.0e11,
            circ_mv=7.0e10,
            source=SimpleNamespace(value="tencent"),
        )
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=quote) as fetch_quote, \
                patch("data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle", return_value={
                    "growth": {"revenue_yoy": 10.1, "net_profit_yoy": 8.5},
                    "earnings": {"forecast_summary": "预增"},
                    "institution": {"institution_holding_change": 1.2},
                    "source_chain": ["growth:akshare"],
                    "errors": [],
                }), \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "partial", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "partial", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "partial", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=1.5)
        self.assertEqual(ctx["market"], "cn")
        self.assertIn("valuation", ctx)
        self.assertIn("growth", ctx)
        self.assertIn("capital_flow", ctx)
        self.assertIn("dragon_tiger", ctx)
        fetch_quote.assert_called_once_with("600519")

    def test_fundamental_context_reuses_provided_realtime_quote(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            pe_ratio=12.3,
            pb_ratio=2.1,
            total_mv=1.0e11,
            circ_mv=7.0e10,
            source=SimpleNamespace(value="tencent"),
        )
        bundle = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote") as fetch_quote, \
                patch(
                    "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle",
                    return_value=bundle,
                ), \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context(
                "600519",
                budget_seconds=1.5,
                realtime_quote=quote,
            )

        fetch_quote.assert_not_called()
        self.assertEqual(ctx["valuation"]["data"]["pe_ratio"], 12.3)
        self.assertEqual(ctx["valuation"]["data"]["pb_ratio"], 2.1)
        self.assertEqual(ctx["valuation"]["data"]["total_mv"], 1.0e11)
        self.assertEqual(ctx["valuation"]["data"]["circ_mv"], 7.0e10)

    def test_fundamental_context_reuses_quote_when_cache_hits(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        current_quote = SimpleNamespace(pe_ratio=20.0, pb_ratio=2.0, total_mv=2.0e11, circ_mv=1.4e11)
        cached_context = {
            "market": "cn",
            "valuation": manager._build_fundamental_block(
                "failed", {}, [{"provider": "realtime_quote", "result": "failed", "duration_ms": 1}], ["old quote failed"]
            ),
            "growth": manager._build_fundamental_block("ok", {"revenue_yoy": 1.0}),
            "earnings": manager._build_fundamental_block("not_supported"),
            "institution": manager._build_fundamental_block("not_supported"),
            "capital_flow": manager._build_fundamental_block("not_supported"),
            "dragon_tiger": manager._build_fundamental_block("not_supported"),
            "boards": manager._build_fundamental_block("not_supported"),
        }
        manager._refresh_fundamental_context_metadata(cached_context, is_etf=False)
        cache_key = manager._get_fundamental_cache_key("600519", 1.5)
        manager._fundamental_cache[cache_key] = {"ts": time.time(), "context": cached_context}
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote") as fetch_quote:
            ctx = manager.get_fundamental_context(
                "600519",
                budget_seconds=1.5,
                realtime_quote=current_quote,
            )

        fetch_quote.assert_not_called()
        self.assertEqual(ctx["valuation"]["data"]["pe_ratio"], 20.0)
        self.assertEqual(ctx["coverage"]["valuation"], "ok")
        self.assertEqual(ctx["status"], "ok")
        self.assertIn(
            {"provider": "realtime_quote", "result": "ok", "duration_ms": 0},
            ctx["source_chain"],
        )
        self.assertNotIn("old quote failed", ctx["errors"])
        self.assertEqual(cached_context["valuation"]["status"], "failed")
        self.assertEqual(cached_context["coverage"]["valuation"], "failed")
        self.assertEqual(cached_context["status"], "partial")

    def test_fundamental_context_none_quote_retries_realtime_quote(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=0,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=2,
        )
        quote = SimpleNamespace(pe_ratio=12.3, pb_ratio=2.1, total_mv=1.0e11, circ_mv=7.0e10)
        bundle = {"status": "not_supported", "growth": {}, "earnings": {}, "institution": {}, "source_chain": [], "errors": []}
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", side_effect=[RuntimeError("first attempt failed"), quote]) as fetch_quote, \
                patch("data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle", return_value=bundle), \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=1.5)

        self.assertEqual(fetch_quote.call_count, 2)
        self.assertEqual(ctx["valuation"]["data"]["pe_ratio"], 12.3)

    def test_etf_context_does_not_reuse_provided_realtime_quote(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        fetched_quote = SimpleNamespace(pe_ratio=10.0, pb_ratio=1.0, total_mv=1.0e11, circ_mv=7.0e10)
        provided_quote = SimpleNamespace(pe_ratio=20.0, pb_ratio=2.0, total_mv=2.0e11, circ_mv=1.4e11)
        bundle = {"status": "not_supported", "growth": {}, "earnings": {}, "institution": {}, "source_chain": [], "errors": []}
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=fetched_quote) as fetch_quote, \
                patch("data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle", return_value=bundle):
            first_ctx = manager.get_fundamental_context("159915", realtime_quote=provided_quote)
            cached_ctx = manager.get_fundamental_context("159915", realtime_quote=provided_quote)

        fetch_quote.assert_called_once_with("159915")
        self.assertEqual(first_ctx["valuation"]["data"]["pe_ratio"], 10.0)
        self.assertEqual(cached_ctx["valuation"]["data"]["pe_ratio"], 10.0)

    def test_fundamental_context_realtime_quote_is_keyword_only(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        quote = SimpleNamespace(pe_ratio=12.3)

        with self.assertRaises(TypeError):
            manager.get_fundamental_context("600519", 1.5, quote)

    def test_fundamental_context_derives_ttm_dividend_yield_from_quote_price(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            price=50.0,
            pe_ratio=12.3,
            pb_ratio=2.1,
            total_mv=1.0e11,
            circ_mv=7.0e10,
            source=SimpleNamespace(value="tencent"),
        )
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch("data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle", return_value={
                    "status": "partial",
                    "growth": {},
                    "earnings": {
                        "dividend": {
                            "ttm_cash_dividend_per_share": 2.5,
                            "ttm_event_count": 1,
                            "events": [{"event_date": "2026-01-01", "cash_dividend_per_share": 2.5}],
                        }
                    },
                    "institution": {},
                    "source_chain": [],
                    "errors": [],
                }), \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=1.5)

        dividend_payload = ctx["earnings"]["data"]["dividend"]
        self.assertAlmostEqual(dividend_payload["ttm_dividend_yield_pct"], 5.0, places=6)
        self.assertIn("yield_formula", dividend_payload)

    def test_fundamental_context_dividend_yield_keeps_null_when_price_invalid(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            price=None,
            pe_ratio=12.3,
            pb_ratio=2.1,
            total_mv=1.0e11,
            circ_mv=7.0e10,
            source=SimpleNamespace(value="tencent"),
        )
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch("data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle", return_value={
                    "status": "partial",
                    "growth": {},
                    "earnings": {
                        "dividend": {
                            "ttm_cash_dividend_per_share": 1.2,
                            "events": [{"event_date": "2026-01-01", "cash_dividend_per_share": 1.2}],
                        }
                    },
                    "institution": {},
                    "source_chain": [],
                    "errors": [],
                }), \
                patch.object(manager, "get_capital_flow_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_dragon_tiger_context", return_value={"status": "not_supported", "source_chain": []}), \
                patch.object(manager, "get_board_context", return_value={"status": "not_supported", "source_chain": []}):
            ctx = manager.get_fundamental_context("600519", budget_seconds=1.5)

        dividend_payload = ctx["earnings"]["data"]["dividend"]
        self.assertIsNone(dividend_payload.get("ttm_dividend_yield_pct"))
        self.assertIn("invalid_price_for_ttm_dividend_yield", ctx["earnings"]["errors"])

    def test_non_etf_board_budget_not_forced_to_zero(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            pe_ratio=12.3,
            pb_ratio=2.1,
            total_mv=1.0e11,
            circ_mv=7.0e10,
            source=SimpleNamespace(value="tencent"),
        )
        bundle = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }
        budgets = {}

        def _capital_flow_side_effect(_stock_code: str, budget_seconds: float = 0.0):
            budgets["capital_flow"] = budget_seconds
            return {"status": "not_supported", "source_chain": [], "errors": [], "data": {}}

        def _dragon_tiger_side_effect(_stock_code: str, budget_seconds: float = 0.0):
            budgets["dragon_tiger"] = budget_seconds
            return {"status": "not_supported", "source_chain": [], "errors": [], "data": {}}

        def _boards_side_effect(_stock_code: str, budget_seconds: float = 0.0):
            budgets["boards"] = budget_seconds
            return {"status": "not_supported", "source_chain": [], "errors": [], "data": {}}

        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch(
                    "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle",
                    return_value=bundle,
                ), \
                patch.object(manager, "get_capital_flow_context", side_effect=_capital_flow_side_effect), \
                patch.object(manager, "get_dragon_tiger_context", side_effect=_dragon_tiger_side_effect), \
                patch.object(manager, "get_board_context", side_effect=_boards_side_effect):
            manager.get_fundamental_context("600519")

        self.assertGreater(budgets.get("capital_flow", 0.0), 0.0)
        self.assertGreater(budgets.get("dragon_tiger", 0.0), 0.0)
        self.assertGreater(budgets.get("boards", 0.0), 0.0)

    def test_run_with_timeout_limits_hanging_workers(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        manager._fundamental_timeout_slots = BoundedSemaphore(1)

        unblock = Event()

        def _hanging_task():
            unblock.wait(timeout=0.5)
            return 1

        try:
            result, err, _ = manager._run_with_timeout(_hanging_task, 0.01, "hang")
            self.assertIsNone(result)
            self.assertIn("timeout", err or "")

            result2, err2, _ = manager._run_with_timeout(_hanging_task, 0.01, "hang")
            self.assertIsNone(result2)
            self.assertIn("worker pool exhausted", err2 or "")
        finally:
            unblock.set()
            time.sleep(0.02)

    def test_infer_block_status_treats_all_null_payload_as_non_ok(self) -> None:
        self.assertEqual(
            DataFetcherManager._infer_block_status(
                {"revenue_yoy": None, "net_profit_yoy": None, "summary": ""},
                "partial",
            ),
            "partial",
        )
        self.assertEqual(
            DataFetcherManager._infer_block_status(
                {"revenue_yoy": None, "net_profit_yoy": None},
                "not_supported",
            ),
            "not_supported",
        )
        self.assertEqual(
            DataFetcherManager._infer_block_status(
                {"revenue_yoy": 0.0},
                "partial",
            ),
            "ok",
        )

    def test_valuation_all_none_fields_should_not_be_ok(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        quote = SimpleNamespace(
            pe_ratio=None,
            pb_ratio=None,
            total_mv=None,
            circ_mv=None,
            source=SimpleNamespace(value="tencent"),
        )
        bundle = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "get_realtime_quote", return_value=quote), \
                patch(
                    "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_fundamental_bundle",
                    return_value=bundle,
                ):
            ctx = manager.get_fundamental_context("600519")

        self.assertEqual(ctx["coverage"].get("valuation"), "partial")

    def test_fundamental_cache_key_isolated_by_budget_bucket(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        key_default = manager._get_fundamental_cache_key("600519")
        key_low = manager._get_fundamental_cache_key("600519", 0.4)
        key_high = manager._get_fundamental_cache_key("600519", 1.5)

        self.assertNotEqual(key_default, key_low)
        self.assertNotEqual(key_low, key_high)
        self.assertIn("budget=", key_low)

    def test_board_context_empty_rankings_mark_failed(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        with patch("src.config.get_config", return_value=cfg), \
                patch.object(manager, "_get_sector_rankings_with_meta", return_value=([], [], [], "all failed")):
            ctx = manager.get_board_context("600519", budget_seconds=0.5)
        self.assertEqual(ctx["status"], "failed")
        self.assertEqual(ctx["data"], {})

    def test_capital_flow_not_supported_status(self) -> None:
        manager = DataFetcherManager(fetchers=[])
        cfg = SimpleNamespace(
            enable_fundamental_pipeline=True,
            fundamental_cache_ttl_seconds=120,
            fundamental_stage_timeout_seconds=1.5,
            fundamental_fetch_timeout_seconds=0.8,
            fundamental_retry_max=1,
        )
        with patch("src.config.get_config", return_value=cfg), \
                patch(
                    "data_provider.fundamental_adapter.AkshareFundamentalAdapter.get_capital_flow",
                    return_value={
                        "status": "not_supported",
                        "stock_flow": {},
                        "sector_rankings": {"top": [], "bottom": []},
                        "source_chain": [],
                        "errors": [],
                    },
                ):
            ctx = manager.get_capital_flow_context("600519", budget_seconds=0.5)
        self.assertEqual(ctx["status"], "not_supported")

    def test_get_belong_boards_from_capability_probe(self) -> None:
        fetcher = _DummyBoardFetcher(
            "EfinanceFetcher",
            priority=0,
            boards=[{"name": "白酒"}, {"board_name": "消费"}],
        )
        manager = DataFetcherManager(fetchers=[fetcher])
        boards = manager.get_belong_boards("600519")
        self.assertEqual(len(boards), 2)
        self.assertEqual(boards[0]["name"], "白酒")
        self.assertEqual(boards[1]["name"], "消费")

    def test_get_belong_boards_preserves_cn_code_and_type_fields(self) -> None:
        fetcher = _DummyBoardFetcher(
            "EfinanceFetcher",
            priority=0,
            boards=[
                {"板块名称": "白酒", "板块代码": "BK0815", "板块类型": "行业"},
                {"板块": "消费", "代码": "BK0475", "类别": "概念"},
            ],
        )
        manager = DataFetcherManager(fetchers=[fetcher])
        boards = manager.get_belong_boards("600519")
        self.assertEqual(len(boards), 2)
        self.assertEqual(
            boards[0],
            {"name": "白酒", "code": "BK0815", "type": "行业"},
        )
        self.assertEqual(
            boards[1],
            {"name": "消费", "code": "BK0475", "type": "概念"},
        )

    def test_get_belong_boards_supports_extended_name_aliases_in_dict_payload(self) -> None:
        fetcher = _DummyBoardFetcher(
            "EfinanceFetcher",
            priority=0,
            boards=[
                {"所属板块": "新能源"},
                {"板块名": "半导体"},
                {"industry": "医药"},
                {"行业": "算力"},
            ],
        )
        manager = DataFetcherManager(fetchers=[fetcher])
        boards = manager.get_belong_boards("600519")
        self.assertEqual(
            boards,
            [
                {"name": "新能源"},
                {"name": "半导体"},
                {"name": "医药"},
                {"name": "算力"},
            ],
        )

    def test_missing_value_helpers_keep_common_null_compatibility(self) -> None:
        for value in (None, np.nan, "", "  ", "null", "NaN", " n/a "):
            self.assertTrue(DataFetcherManager._is_missing_board_value(value))
        self.assertFalse(DataFetcherManager._is_missing_board_value("白酒"))
        self.assertFalse(DataFetcherManager._has_meaningful_payload(np.array([None, np.nan])))
        self.assertTrue(DataFetcherManager._has_meaningful_payload(np.array([None, "白酒"])))

    def test_missing_value_helpers_log_expected_pd_isna_fallback(self) -> None:
        sentinel = object()
        with patch("data_provider.base.pd.isna", side_effect=ValueError("ambiguous")):
            with self.assertLogs("data_provider.base", level="DEBUG") as logs:
                self.assertFalse(DataFetcherManager._is_missing_board_value(sentinel))
                self.assertTrue(DataFetcherManager._has_meaningful_payload(sentinel))

        joined_logs = "\n".join(logs.output)
        self.assertIn("[board_value] pd.isna fallback", joined_logs)
        self.assertIn("[fundamental_payload] pd.isna fallback", joined_logs)

    def test_missing_value_helpers_propagate_array_protocol_pd_isna_errors(self) -> None:
        class _ArrayProtocolErrorPayload:
            def __array__(self):
                raise ValueError("boom")

        payload = _ArrayProtocolErrorPayload()
        with self.assertRaises(ValueError):
            DataFetcherManager._is_missing_board_value(payload)
        with self.assertRaises(ValueError):
            DataFetcherManager._has_meaningful_payload(payload)

    def test_missing_value_helpers_propagate_unexpected_pd_isna_errors(self) -> None:
        sentinel = object()
        with patch("data_provider.base.pd.isna", side_effect=RuntimeError("boom")):
            with self.assertRaises(RuntimeError):
                DataFetcherManager._is_missing_board_value(sentinel)
            with self.assertRaises(RuntimeError):
                DataFetcherManager._has_meaningful_payload(sentinel)


if __name__ == "__main__":
    unittest.main()
