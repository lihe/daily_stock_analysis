# -*- coding: utf-8 -*-
"""Tushare 结构化基本面适配器的公开行为测试。"""

from __future__ import annotations

from datetime import date, timedelta
import threading
import time

import pandas as pd

from data_provider.tushare_fundamental_adapter import TushareFundamentalAdapter


def test_fundamental_bundle_routes_every_endpoint_through_callback() -> None:
    calls: list[tuple[str, dict]] = []

    def callback(api_name: str, **kwargs) -> pd.DataFrame:
        calls.append((api_name, kwargs))
        return pd.DataFrame()

    adapter = TushareFundamentalAdapter(callback)

    adapter.get_fundamental_bundle("600519", timeout_seconds=1.0)

    assert {api_name for api_name, _ in calls} == {
        "fina_indicator",
        "income",
        "cashflow",
        "forecast",
        "express",
        "dividend",
        "top10_holders",
    }
    assert all(kwargs["ts_code"] == "600519.SH" for _, kwargs in calls)


def test_fundamental_bundle_selects_latest_visible_consolidated_revision() -> None:
    frames = {
        "fina_indicator": pd.DataFrame(
            [
                {
                    "end_date": "20260331",
                    "ann_date": "20260419",
                    "f_ann_date": "20260420",
                    "report_type": "1",
                    "tr_yoy": 10.0,
                    "netprofit_yoy": 7.0,
                    "roe": 18.0,
                    "grossprofit_margin": 55.0,
                },
                {
                    "end_date": "20260331",
                    "ann_date": "20260421",
                    "f_ann_date": "20260425",
                    "report_type": 1,
                    "tr_yoy": 12.5,
                    "netprofit_yoy": 8.5,
                    "roe": 19.0,
                    "grossprofit_margin": 56.0,
                },
                {
                    "end_date": "20260331",
                    "ann_date": "20260430",
                    "report_type": 2,
                    "tr_yoy": 999.0,
                },
                {
                    "end_date": "20260331",
                    "f_ann_date": "20990101",
                    "report_type": 1,
                    "tr_yoy": 888.0,
                },
            ]
        ),
        "income": pd.DataFrame(
            [
                {
                    "end_date": "20260331",
                    "f_ann_date": "20260420",
                    "report_type": "1",
                    "total_revenue": 100.0,
                    "n_income_attr_p": 20.0,
                },
                {
                    "end_date": "20260331",
                    "f_ann_date": "20260426",
                    "report_type": "1",
                    "total_revenue": 120.0,
                    "n_income_attr_p": 30.0,
                },
                {
                    "end_date": "20251231",
                    "f_ann_date": "20260320",
                    "report_type": "1",
                    "total_revenue": 999.0,
                    "n_income_attr_p": 999.0,
                },
            ]
        ),
        "cashflow": pd.DataFrame(
            [
                {
                    "end_date": "20260331",
                    "ann_date": "20260422",
                    "report_type": "1",
                    "n_cashflow_act": 40.0,
                },
                {
                    "end_date": "20251231",
                    "ann_date": "20260320",
                    "report_type": "1",
                    "n_cashflow_act": 999.0,
                },
            ]
        ),
    }

    adapter = TushareFundamentalAdapter(lambda api_name, **_: frames.get(api_name, pd.DataFrame()))

    result = adapter.get_fundamental_bundle("600519", timeout_seconds=1.0)

    assert result["growth"] == {
        "revenue_yoy": 12.5,
        "revenue_yoy_basis": "cumulative",
        "net_profit_yoy": 8.5,
        "net_profit_yoy_basis": "cumulative",
        "roe": 19.0,
        "gross_margin": 56.0,
    }
    assert result["earnings"]["financial_report"] == {
        "report_date": "2026-03-31",
        "announcement_date": "2026-04-25",
        "revenue": 120.0,
        "net_profit_parent": 30.0,
        "operating_cash_flow": 40.0,
        "roe": 19.0,
        "currency": "CNY",
        "amount_unit": "yuan",
        "report_type": 1,
    }
    assert result["status"] == "partial"


def test_growth_uses_single_quarter_fields_only_when_cumulative_fields_are_missing() -> None:
    indicator = pd.DataFrame(
        [
            {
                "end_date": "20260331",
                "ann_date": "20260420",
                "report_type": "1",
                "tr_yoy": None,
                "or_yoy": None,
                "netprofit_yoy": None,
                "q_sales_yoy": 6.5,
                "q_profit_yoy": 4.5,
            }
        ]
    )
    adapter = TushareFundamentalAdapter(
        lambda api_name, **_: indicator if api_name == "fina_indicator" else pd.DataFrame()
    )

    result = adapter.get_fundamental_bundle("000001", timeout_seconds=1.0)

    assert result["growth"]["revenue_yoy"] == 6.5
    assert result["growth"]["revenue_yoy_basis"] == "single_quarter"
    assert result["growth"]["net_profit_yoy"] == 4.5
    assert result["growth"]["net_profit_yoy_basis"] == "single_quarter"
    assert result["growth"]["roe"] is None
    assert result["growth"]["gross_margin"] is None


def test_financial_report_keeps_missing_fields_none_and_uses_visible_income_metadata() -> None:
    income = pd.DataFrame(
        [
            {
                "end_date": "20260331",
                "ann_date": "20260420",
                "report_type": "1",
                "total_revenue": 120.0,
            }
        ]
    )
    adapter = TushareFundamentalAdapter(
        lambda api_name, **_: income if api_name == "income" else pd.DataFrame()
    )

    report = adapter.get_fundamental_bundle("600519", timeout_seconds=1.0)["earnings"]["financial_report"]

    assert report["report_date"] == "2026-03-31"
    assert report["announcement_date"] == "2026-04-20"
    assert report["revenue"] == 120.0
    assert report["net_profit_parent"] is None
    assert report["operating_cash_flow"] is None
    assert report["roe"] is None


def test_bundle_exposes_forecast_and_express_summary() -> None:
    frames = {
        "forecast": pd.DataFrame(
            [
                {
                    "end_date": "20260630",
                    "ann_date": "20260715",
                    "type": "预增",
                    "p_change_min": 10.0,
                    "p_change_max": 20.0,
                    "summary": "预计归母净利润增长",
                    "change_reason": "主营业务增长",
                }
            ]
        ),
        "express": pd.DataFrame(
            [
                {
                    "end_date": "20260630",
                    "ann_date": "20260720",
                    "perf_summary": "上半年经营稳健",
                }
            ]
        ),
    }
    adapter = TushareFundamentalAdapter(lambda api_name, **_: frames.get(api_name, pd.DataFrame()))

    result = adapter.get_fundamental_bundle("600519", timeout_seconds=1.0)

    assert result["earnings"]["forecast"] == {
        "report_date": "2026-06-30",
        "announcement_date": "2026-07-15",
        "type": "预增",
        "profit_change_min_pct": 10.0,
        "profit_change_max_pct": 20.0,
        "summary": "预计归母净利润增长",
        "change_reason": "主营业务增长",
    }
    assert result["earnings"]["forecast_summary"] == "预计归母净利润增长"
    assert result["earnings"]["express"] == {
        "report_date": "2026-06-30",
        "announcement_date": "2026-07-20",
        "perf_summary": "上半年经营稳健",
    }
    assert result["earnings"]["quick_report_summary"] == "上半年经营稳健"


def test_dividend_ttm_uses_implemented_pre_tax_cash_div_tax_without_swapping_fields() -> None:
    today = date.today()

    def compact(day: date) -> str:
        return day.strftime("%Y%m%d")

    dividend = pd.DataFrame(
        [
            {
                "ann_date": compact(today - timedelta(days=20)),
                "div_proc": "实施",
                "record_date": compact(today - timedelta(days=11)),
                "ex_date": compact(today - timedelta(days=10)),
                "cash_div": 0.2,
                "cash_div_tax": 0.3,
            },
            {
                "ann_date": compact(today - timedelta(days=110)),
                "div_proc": "实施",
                "record_date": compact(today - timedelta(days=101)),
                "ex_date": compact(today - timedelta(days=100)),
                "cash_div": 0.4,
                "cash_div_tax": 0.5,
            },
            {
                "ann_date": compact(today - timedelta(days=2)),
                "div_proc": "预案",
                "ex_date": compact(today - timedelta(days=1)),
                "cash_div": 0.6,
                "cash_div_tax": 0.7,
            },
            {
                "ann_date": compact(today - timedelta(days=370)),
                "div_proc": "实施",
                "ex_date": compact(today - timedelta(days=366)),
                "cash_div": 0.8,
                "cash_div_tax": 0.9,
            },
        ]
    )
    adapter = TushareFundamentalAdapter(
        lambda api_name, **_: dividend if api_name == "dividend" else pd.DataFrame()
    )

    result = adapter.get_fundamental_bundle("600519", timeout_seconds=1.0)

    payload = result["earnings"]["dividend"]
    assert payload["ttm_event_count"] == 2
    assert payload["ttm_cash_dividend_per_share"] == 0.8
    assert payload["coverage"] == "implemented_cash_dividend_pre_tax_365d"
    assert payload["currency"] == "CNY"
    assert payload["amount_unit"] == "yuan_per_share"
    assert payload["events"][0]["cash_dividend_per_share"] == 0.3
    assert payload["events"][0]["cash_div_tax"] == 0.3
    assert payload["events"][0]["cash_div"] == 0.2


def test_top10_holders_returns_latest_visible_snapshot_without_manufactured_change() -> None:
    holders = pd.DataFrame(
        [
            {
                "end_date": "20260331",
                "ann_date": "20260425",
                "holder_name": "股东甲",
                "hold_amount": 1000.0,
                "hold_ratio": 10.0,
                "hold_float_ratio": 12.0,
                "holder_type": "基金",
                "hold_change": 88.0,
            },
            {
                "end_date": "20260331",
                "ann_date": "20260425",
                "holder_name": "股东乙",
                "hold_amount": 800.0,
                "hold_ratio": 8.0,
                "hold_float_ratio": None,
                "holder_type": None,
                "hold_change": -10.0,
            },
            {
                "end_date": "20251231",
                "ann_date": "20260320",
                "holder_name": "旧股东",
                "hold_amount": 9999.0,
                "hold_ratio": 99.0,
            },
        ]
    )
    adapter = TushareFundamentalAdapter(
        lambda api_name, **_: holders if api_name == "top10_holders" else pd.DataFrame()
    )

    result = adapter.get_fundamental_bundle("600519", timeout_seconds=1.0)

    snapshot = result["institution"]["top10_holder_snapshot"]
    assert snapshot["report_date"] == "2026-03-31"
    assert snapshot["announcement_date"] == "2026-04-25"
    assert snapshot["holder_count"] == 2
    assert snapshot["amount_unit"] == "share"
    assert snapshot["holders"][0] == {
        "holder_name": "股东甲",
        "hold_amount": 1000.0,
        "hold_ratio": 10.0,
        "hold_float_ratio": 12.0,
        "holder_type": "基金",
    }
    assert snapshot["holders"][1]["hold_float_ratio"] is None
    assert snapshot["holders"][1]["holder_type"] is None
    assert "top10_holder_change" not in result["institution"]
    assert all("hold_change" not in item for item in snapshot["holders"])


def test_capital_flow_uses_true_net_mf_amount_names_and_whole_sector_list() -> None:
    moneyflow = pd.DataFrame(
        [
            {"trade_date": "20260701", "net_mf_amount": 1.0},
            {"trade_date": "20260711", "net_mf_amount": 11.0},
            {"trade_date": "20260703", "net_mf_amount": 3.0},
            {"trade_date": "20260709", "net_mf_amount": 9.0},
            {"trade_date": "20260705", "net_mf_amount": 5.0},
            {"trade_date": "20260707", "net_mf_amount": 7.0},
            {"trade_date": "20260702", "net_mf_amount": 2.0},
            {"trade_date": "20260710", "net_mf_amount": 10.0},
            {"trade_date": "20260704", "net_mf_amount": 4.0},
            {"trade_date": "20260708", "net_mf_amount": 8.0},
            {"trade_date": "20260706", "net_mf_amount": 6.0},
        ]
    )
    sectors = pd.DataFrame(
        [
            {"industry": "行业甲", "net_amount": -30000.0},
            {"industry": "行业乙", "net_amount": 10000.0},
            {"industry": "行业丙", "net_amount": 50000.0},
            {"industry": "行业丁", "net_amount": -10000.0},
            {"industry": "行业戊", "net_amount": 20000.0},
        ]
    )
    calls: list[str] = []

    def callback(api_name: str, **_) -> pd.DataFrame:
        calls.append(api_name)
        return moneyflow if api_name == "moneyflow" else sectors

    result = TushareFundamentalAdapter(callback).get_capital_flow(
        "600519", timeout_seconds=1.0, top_n=2
    )

    assert set(calls) == {"moneyflow", "moneyflow_ind_ths"}
    assert result["stock_flow"] == {
        "trade_date": "2026-07-11",
        "completed_trade_dates": [
            "2026-07-11",
            "2026-07-10",
            "2026-07-09",
            "2026-07-08",
            "2026-07-07",
            "2026-07-06",
            "2026-07-05",
            "2026-07-04",
            "2026-07-03",
            "2026-07-02",
        ],
        "net_mf_amount": 11.0,
        "net_mf_amount_5d": 45.0,
        "net_mf_amount_10d": 65.0,
        "amount_unit": "万元",
    }
    assert result["sector_rankings"] == {
        "top": [
            {"name": "行业丙", "net_amount": 5.0, "amount_unit": "亿元"},
            {"name": "行业戊", "net_amount": 2.0, "amount_unit": "亿元"},
        ],
        "bottom": [
            {"name": "行业甲", "net_amount": -3.0, "amount_unit": "亿元"},
            {"name": "行业丁", "net_amount": -1.0, "amount_unit": "亿元"},
        ],
    }
    assert "main_net_inflow" not in result["stock_flow"]
    assert "inflow_5d" not in result["stock_flow"]
    assert "inflow_10d" not in result["stock_flow"]


def test_endpoint_error_is_sanitized_while_other_endpoint_data_survives() -> None:
    def callback(api_name: str, **_) -> pd.DataFrame:
        if api_name == "income":
            raise ValueError("sensitive upstream details")
        if api_name == "forecast":
            return pd.DataFrame(
                [{"end_date": "20260630", "ann_date": "20260715", "summary": "预增"}]
            )
        return pd.DataFrame()

    result = TushareFundamentalAdapter(callback).get_fundamental_bundle(
        "600519", timeout_seconds=1.0
    )

    assert result["status"] == "partial"
    assert result["earnings"]["forecast_summary"] == "预增"
    assert result["errors"] == ["income:ValueError"]
    assert "sensitive upstream details" not in str(result)
    assert all(item.startswith("tushare.") for item in result["source_chain"])


def test_fundamental_timeout_returns_completed_partial_without_waiting_for_running_calls() -> None:
    release = threading.Event()
    lock = threading.Lock()
    started: list[str] = []
    active = 0
    peak_active = 0

    def callback(api_name: str, **_) -> pd.DataFrame:
        nonlocal active, peak_active
        with lock:
            started.append(api_name)
            active += 1
            peak_active = max(peak_active, active)
        try:
            if api_name == "forecast":
                return pd.DataFrame(
                    [{"end_date": "20260630", "ann_date": "20260715", "summary": "已完成"}]
                )
            release.wait(timeout=1.0)
            return pd.DataFrame()
        finally:
            with lock:
                active -= 1

    started_at = time.monotonic()
    result = TushareFundamentalAdapter(callback).get_fundamental_bundle(
        "600519", timeout_seconds=0.05
    )
    elapsed = time.monotonic() - started_at
    with lock:
        started_before_release = list(started)
    release.set()
    time.sleep(0.05)

    assert elapsed < 0.3
    assert peak_active <= 4
    assert result["earnings"]["forecast_summary"] == "已完成"
    assert set(result["errors"]) == {
        f"{api_name}:TimeoutError"
        for api_name in {
            "fina_indicator",
            "income",
            "cashflow",
            "express",
            "dividend",
            "top10_holders",
        }
    }
    with lock:
        assert started == started_before_release
