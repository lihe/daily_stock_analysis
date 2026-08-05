# -*- coding: utf-8 -*-
"""Tushare 结构化基本面与资金流适配器。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, wait
from datetime import date, datetime, time as datetime_time, timedelta
from typing import Any, Callable, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

import pandas as pd


ApiCallback = Callable[..., pd.DataFrame]
NowProvider = Callable[[], datetime]

_ETF_PREFIXES = ("15", "16", "18", "51", "52", "56", "58")
_CN_STOCK_EXCHANGES = {
    "SH": ("600", "601", "603", "605", "688"),
    "SZ": ("000", "001", "002", "003", "300", "301"),
    "BJ": ("43", "81", "82", "83", "87", "88", "92"),
}


def _supported_cn_stock(stock_code: str) -> bool:
    """只接受可明确归属交易所的 A 股，未知代码宁可不请求。"""
    raw = str(stock_code).strip().upper()
    exchange_hint: Optional[str] = None
    if raw.startswith(("SH", "SZ", "BJ")):
        exchange_hint, raw = raw[:2], raw[2:]
    elif "." in raw:
        raw, exchange_hint = raw.split(".", 1)
        if exchange_hint == "SS":
            exchange_hint = "SH"

    if not raw.isdigit() or len(raw) != 6 or raw.startswith(_ETF_PREFIXES):
        return False
    expected_exchange = next(
        (
            exchange
            for exchange, prefixes in _CN_STOCK_EXCHANGES.items()
            if raw.startswith(prefixes)
        ),
        None,
    )
    if expected_exchange is None:
        return False
    return exchange_hint in (None, expected_exchange)


def _empty_fundamental_bundle() -> Dict[str, Any]:
    return {
        "status": "not_supported",
        "growth": {},
        "earnings": {},
        "institution": {},
        "source_chain": [],
        "errors": [],
    }


def _empty_capital_flow() -> Dict[str, Any]:
    return {
        "status": "not_supported",
        "stock_flow": {},
        "sector_rankings": {"top": [], "bottom": []},
        "source_chain": [],
        "errors": [],
    }


def _to_ts_code(stock_code: str) -> str:
    code = str(stock_code).strip().upper()
    if "." in code:
        symbol, exchange = code.split(".", 1)
        if exchange == "SS":
            exchange = "SH"
        return f"{symbol}.{exchange}"
    if code.startswith(("SH", "SZ", "BJ")):
        return f"{code[2:]}.{code[:2]}"
    exchange = "SH" if code.startswith(("5", "6", "9")) else "BJ" if code.startswith(("4", "8")) else "SZ"
    return f"{code}.{exchange}"


def _safe_float(value: Any) -> Optional[float]:
    try:
        if pd.isna(value):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> Optional[str]:
    if value is None or pd.isna(value):
        return None
    text = str(value).strip()
    return text or None


def _iso_date(value: Any) -> Optional[str]:
    parsed = pd.to_datetime(value, errors="coerce")
    if pd.isna(parsed):
        return None
    return parsed.date().isoformat()


def _announcement_value(row: pd.Series) -> Any:
    """更正公告优先使用 f_ann_date，缺失时才退回首次公告日期。"""
    f_ann_date = row.get("f_ann_date")
    if f_ann_date is not None and not pd.isna(f_ann_date) and str(f_ann_date).strip():
        return f_ann_date
    return row.get("ann_date")


def _select_report_row(
    df: Optional[pd.DataFrame],
    *,
    visible_date: date,
    end_date: Optional[str] = None,
    require_report_type: bool = False,
    require_end_date: bool = False,
) -> Optional[pd.Series]:
    if not isinstance(df, pd.DataFrame) or df.empty:
        return None

    work = df.copy()
    if require_report_type and "report_type" not in work.columns:
        return None
    if "report_type" in work.columns:
        work = work[work["report_type"].astype(str).str.strip() == "1"]
    if (require_end_date or end_date is not None) and "end_date" not in work.columns:
        return None
    if "end_date" in work.columns:
        work["__end_date"] = pd.to_datetime(work["end_date"], errors="coerce")
        if require_end_date:
            work = work[work["__end_date"].notna()]
    else:
        work["__end_date"] = pd.NaT
    if end_date is not None:
        target_end_date = pd.to_datetime(end_date, errors="coerce")
        if pd.isna(target_end_date):
            return None
        work = work[work["__end_date"] == target_end_date]
    if work.empty:
        return None

    work["__announcement"] = work.apply(_announcement_value, axis=1)
    announcement_ts = pd.to_datetime(work["__announcement"], errors="coerce")
    # 未来才公开的修订不能泄漏进当前分析结果。
    work = work[announcement_ts.dt.date <= visible_date]
    if work.empty:
        return None

    work["__announcement_ts"] = pd.to_datetime(work["__announcement"], errors="coerce")
    work = work.sort_values(["__end_date", "__announcement_ts"], ascending=[False, False], na_position="last")
    return work.iloc[0]


def _build_dividend_payload(df: Optional[pd.DataFrame], visible_date: date) -> Dict[str, Any]:
    if not isinstance(df, pd.DataFrame) or df.empty or "div_proc" not in df.columns:
        return {}

    cutoff = visible_date - timedelta(days=365)
    events = []
    for _, row in df.iterrows():
        if _safe_str(row.get("div_proc")) != "实施":
            continue
        ex_date = pd.to_datetime(row.get("ex_date"), errors="coerce")
        if pd.isna(ex_date) or not cutoff <= ex_date.date() <= visible_date:
            continue
        cash_div_tax = _safe_float(row.get("cash_div_tax"))
        if cash_div_tax is None or cash_div_tax <= 0:
            continue
        events.append(
            {
                "event_date": ex_date.date().isoformat(),
                "ex_dividend_date": ex_date.date().isoformat(),
                "record_date": _iso_date(row.get("record_date")),
                "announcement_date": _iso_date(_announcement_value(row)),
                # Tushare 的 cash_div_tax 才是税前每股现金额；不能与 cash_div 对调。
                "cash_dividend_per_share": cash_div_tax,
                "cash_div_tax": cash_div_tax,
                "cash_div": _safe_float(row.get("cash_div")),
                "is_pre_tax": True,
            }
        )
    if not events:
        return {}

    events.sort(key=lambda item: item["event_date"], reverse=True)
    return {
        "events": events[:5],
        "ttm_event_count": len(events),
        "ttm_cash_dividend_per_share": round(sum(item["cash_div_tax"] for item in events), 6),
        "coverage": "implemented_cash_dividend_pre_tax_365d",
        "currency": "CNY",
        "amount_unit": "yuan_per_share",
        "as_of": visible_date.isoformat(),
    }


def _build_top10_snapshot(df: Optional[pd.DataFrame], visible_date: date) -> Dict[str, Any]:
    if not isinstance(df, pd.DataFrame) or df.empty or "end_date" not in df.columns:
        return {}

    work = df.copy()
    work["__end_date"] = pd.to_datetime(work["end_date"], errors="coerce")
    work["__announcement"] = work.apply(_announcement_value, axis=1)
    work["__announcement_ts"] = pd.to_datetime(work["__announcement"], errors="coerce")
    work = work[work["__announcement_ts"].dt.date <= visible_date]
    if work.empty:
        return {}
    latest_end = work["__end_date"].max()
    work = work[work["__end_date"] == latest_end]
    latest_announcement = work["__announcement_ts"].max()
    work = work[work["__announcement_ts"] == latest_announcement]
    if work.empty:
        return {}

    if "hold_ratio" in work.columns:
        work = work.assign(__ratio=pd.to_numeric(work["hold_ratio"], errors="coerce"))
        work = work.sort_values("__ratio", ascending=False, na_position="last")
    holders = []
    for _, row in work.head(10).iterrows():
        holders.append(
            {
                "holder_name": _safe_str(row.get("holder_name")),
                "hold_amount": _safe_float(row.get("hold_amount")),
                "hold_ratio": _safe_float(row.get("hold_ratio")),
                "hold_float_ratio": _safe_float(row.get("hold_float_ratio")),
                "holder_type": _safe_str(row.get("holder_type")),
            }
        )
    return {
        "report_date": latest_end.date().isoformat(),
        "announcement_date": latest_announcement.date().isoformat(),
        "holder_count": len(holders),
        "amount_unit": "share",
        "holders": holders,
    }


class TushareFundamentalAdapter:
    """通过调用方提供的统一出口获取 Tushare 数据。"""

    _FUNDAMENTAL_ENDPOINTS = (
        "fina_indicator",
        "income",
        "cashflow",
        "forecast",
        "express",
        "dividend",
        "top10_holders",
    )

    def __init__(
        self,
        api_callback: ApiCallback,
        now_provider: Optional[NowProvider] = None,
    ) -> None:
        self._api_callback = api_callback
        self._now_provider = now_provider or (
            lambda: datetime.now(ZoneInfo("Asia/Shanghai"))
        )

    def _shanghai_now(self) -> datetime:
        now = self._now_provider()
        zone = ZoneInfo("Asia/Shanghai")
        if now.tzinfo is None:
            return now.replace(tzinfo=zone)
        return now.astimezone(zone)

    def _run_endpoints(
        self,
        requests: Dict[str, Dict[str, Any]],
        timeout_seconds: float,
        max_workers: int,
        thread_name_prefix: str,
    ) -> Tuple[Dict[str, pd.DataFrame], List[str], List[str]]:
        executor = ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix=thread_name_prefix)
        futures = {
            executor.submit(self._api_callback, api_name, **kwargs): api_name
            for api_name, kwargs in requests.items()
        }
        done, pending = wait(futures, timeout=max(0.0, float(timeout_seconds)))
        frames: Dict[str, pd.DataFrame] = {}
        errors: List[str] = []
        for future in done:
            api_name = futures[future]
            try:
                frame = future.result()
                if isinstance(frame, pd.DataFrame):
                    frames[api_name] = frame
            except Exception as exc:
                errors.append(f"{api_name}:{type(exc).__name__}")
        for future in pending:
            errors.append(f"{futures[future]}:TimeoutError")
            future.cancel()
        executor.shutdown(wait=False, cancel_futures=True)

        endpoint_order = {api_name: index for index, api_name in enumerate(requests)}
        errors.sort(key=lambda item: endpoint_order[item.split(":", 1)[0]])
        # cancel() 成功的 future 从未进入 callback，不能出现在来源链中。
        attempted = [futures[future] for future in futures if not future.cancelled()]
        attempted.sort(key=endpoint_order.__getitem__)
        return frames, errors, attempted

    def get_fundamental_bundle(self, stock_code: str, timeout_seconds: float) -> Dict[str, Any]:
        if not _supported_cn_stock(stock_code):
            return _empty_fundamental_bundle()
        ts_code = _to_ts_code(stock_code)
        frames, errors, attempted = self._run_endpoints(
            {api_name: {"ts_code": ts_code} for api_name in self._FUNDAMENTAL_ENDPOINTS},
            timeout_seconds=timeout_seconds,
            max_workers=4,
            thread_name_prefix="tushare-fundamental",
        )
        visible_date = self._shanghai_now().date()

        result = _empty_fundamental_bundle()
        result["source_chain"] = [f"tushare.{api_name}" for api_name in attempted]
        result["errors"] = errors

        indicator_row = _select_report_row(
            frames.get("fina_indicator"),
            visible_date=visible_date,
            require_report_type=True,
            require_end_date=True,
        )
        if indicator_row is None:
            income_anchor = _select_report_row(
                frames.get("income"),
                visible_date=visible_date,
                require_report_type=True,
                require_end_date=True,
            )
            target_end_date = _iso_date(income_anchor.get("end_date")) if income_anchor is not None else None
        else:
            target_end_date = _iso_date(indicator_row.get("end_date"))
        income_row = _select_report_row(
            frames.get("income"),
            visible_date=visible_date,
            end_date=target_end_date,
            require_report_type=True,
        )
        cashflow_row = _select_report_row(
            frames.get("cashflow"),
            visible_date=visible_date,
            end_date=target_end_date,
            require_report_type=True,
        )

        if indicator_row is not None:
            revenue_yoy = _safe_float(indicator_row.get("tr_yoy"))
            if revenue_yoy is None:
                revenue_yoy = _safe_float(indicator_row.get("or_yoy"))
            revenue_basis = "cumulative" if revenue_yoy is not None else None
            if revenue_yoy is None:
                revenue_yoy = _safe_float(indicator_row.get("q_sales_yoy"))
                if revenue_yoy is None:
                    revenue_yoy = _safe_float(indicator_row.get("q_gr_yoy"))
                revenue_basis = "single_quarter" if revenue_yoy is not None else None
            net_profit_yoy = _safe_float(indicator_row.get("netprofit_yoy"))
            net_profit_basis = "cumulative" if net_profit_yoy is not None else None
            if net_profit_yoy is None:
                net_profit_yoy = _safe_float(indicator_row.get("q_profit_yoy"))
                if net_profit_yoy is None:
                    net_profit_yoy = _safe_float(indicator_row.get("q_netprofit_yoy"))
                net_profit_basis = "single_quarter" if net_profit_yoy is not None else None
            result["growth"] = {
                "revenue_yoy": revenue_yoy,
                "revenue_yoy_basis": revenue_basis,
                "net_profit_yoy": net_profit_yoy,
                "net_profit_yoy_basis": net_profit_basis,
                "roe": _safe_float(indicator_row.get("roe")),
                "gross_margin": _safe_float(indicator_row.get("grossprofit_margin")),
            }

        if target_end_date is not None:
            announcement_row = indicator_row
            if announcement_row is None:
                announcement_row = income_row
            if announcement_row is None:
                announcement_row = cashflow_row
            financial_report = {
                "report_date": _iso_date(target_end_date),
                "announcement_date": (
                    _iso_date(_announcement_value(announcement_row)) if announcement_row is not None else None
                ),
                "revenue": _safe_float(income_row.get("total_revenue")) if income_row is not None else None,
                "net_profit_parent": _safe_float(income_row.get("n_income_attr_p")) if income_row is not None else None,
                "operating_cash_flow": (
                    _safe_float(cashflow_row.get("n_cashflow_act")) if cashflow_row is not None else None
                ),
                "roe": _safe_float(indicator_row.get("roe")) if indicator_row is not None else None,
                "currency": "CNY",
                "amount_unit": "yuan",
                "report_type": 1,
            }
            result["earnings"]["financial_report"] = financial_report

        forecast_row = _select_report_row(
            frames.get("forecast"),
            visible_date=visible_date,
        )
        if forecast_row is not None:
            forecast = {
                "report_date": _iso_date(forecast_row.get("end_date")),
                "announcement_date": _iso_date(_announcement_value(forecast_row)),
                "type": _safe_str(forecast_row.get("type")),
                "profit_change_min_pct": _safe_float(forecast_row.get("p_change_min")),
                "profit_change_max_pct": _safe_float(forecast_row.get("p_change_max")),
                "summary": _safe_str(forecast_row.get("summary")),
                "change_reason": _safe_str(forecast_row.get("change_reason")),
            }
            result["earnings"]["forecast"] = forecast
            result["earnings"]["forecast_summary"] = forecast["summary"]

        express_row = _select_report_row(
            frames.get("express"),
            visible_date=visible_date,
        )
        if express_row is not None:
            express = {
                "report_date": _iso_date(express_row.get("end_date")),
                "announcement_date": _iso_date(_announcement_value(express_row)),
                "perf_summary": _safe_str(express_row.get("perf_summary")),
            }
            result["earnings"]["express"] = express
            result["earnings"]["quick_report_summary"] = express["perf_summary"]

        dividend_payload = _build_dividend_payload(
            frames.get("dividend"),
            visible_date,
        )
        if dividend_payload:
            result["earnings"]["dividend"] = dividend_payload

        top10_snapshot = _build_top10_snapshot(
            frames.get("top10_holders"),
            visible_date,
        )
        if top10_snapshot:
            result["institution"]["top10_holder_snapshot"] = top10_snapshot

        if result["growth"] or result["earnings"] or result["institution"]:
            result["status"] = "partial"
        return result

    def get_capital_flow(
        self,
        stock_code: str,
        timeout_seconds: float,
        top_n: int = 5,
    ) -> Dict[str, Any]:
        if not _supported_cn_stock(stock_code):
            return _empty_capital_flow()
        ts_code = _to_ts_code(stock_code)
        frames, errors, attempted = self._run_endpoints(
            {
                "moneyflow": {"ts_code": ts_code},
                "moneyflow_ind_ths": {},
            },
            timeout_seconds=timeout_seconds,
            max_workers=2,
            thread_name_prefix="tushare-capital-flow",
        )

        result = _empty_capital_flow()
        result["source_chain"] = [f"tushare.{api_name}" for api_name in attempted]
        result["errors"] = errors

        stock_df = frames.get("moneyflow")
        if isinstance(stock_df, pd.DataFrame) and not stock_df.empty and {
            "trade_date",
            "net_mf_amount",
        }.issubset(stock_df.columns):
            work = stock_df[["trade_date", "net_mf_amount"]].copy()
            work["__trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
            work["net_mf_amount"] = pd.to_numeric(work["net_mf_amount"], errors="coerce")
            china_now = self._shanghai_now()
            completed_date = china_now.date()
            if china_now.time() < datetime_time(15, 30):
                completed_date -= timedelta(days=1)
            work = work[
                (work["__trade_date"].dt.date <= completed_date)
                & work["net_mf_amount"].notna()
            ].sort_values("__trade_date", ascending=False)
            latest_ten = work.head(10)
            if not latest_ten.empty:
                values = latest_ten["net_mf_amount"].tolist()
                result["stock_flow"] = {
                    "trade_date": latest_ten.iloc[0]["__trade_date"].date().isoformat(),
                    "completed_trade_dates": [
                        value.date().isoformat() for value in latest_ten["__trade_date"]
                    ],
                    "net_mf_amount": float(values[0]),
                    "net_mf_amount_5d": float(sum(values[:5])) if len(values) >= 5 else None,
                    "net_mf_amount_10d": float(sum(values[:10])) if len(values) >= 10 else None,
                    "amount_unit": "万元",
                }

        sector_df = frames.get("moneyflow_ind_ths")
        if isinstance(sector_df, pd.DataFrame) and not sector_df.empty and {
            "industry",
            "net_amount",
        }.issubset(sector_df.columns):
            work = sector_df.copy()
            if "trade_date" in work.columns:
                work["__trade_date"] = pd.to_datetime(work["trade_date"], errors="coerce")
                latest_trade_date = work["__trade_date"].max()
                work = work[work["__trade_date"] == latest_trade_date]
            work["__net_amount"] = pd.to_numeric(work["net_amount"], errors="coerce") / 10000.0
            work = work.dropna(subset=["__net_amount"])

            def normalize_rankings(frame: pd.DataFrame) -> list[Dict[str, Any]]:
                return [
                    {
                        "name": _safe_str(row.get("industry")),
                        "net_amount": float(row["__net_amount"]),
                        "amount_unit": "亿元",
                    }
                    for _, row in frame.iterrows()
                ]

            limit = max(0, int(top_n))
            # 排序必须覆盖完整行业列表，否则 API 原始行序会伪造榜首/榜尾。
            result["sector_rankings"] = {
                "top": normalize_rankings(work.nlargest(limit, "__net_amount")),
                "bottom": normalize_rankings(work.nsmallest(limit, "__net_amount")),
            }

        if result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"]:
            result["status"] = "partial"
        return result
