# -*- coding: utf-8 -*-
"""Efinance 成交量单位契约测试。"""

from __future__ import annotations

import pandas as pd

from data_provider.efinance_fetcher import EfinanceFetcher


def _normalize(raw_volume: int, amount: float, close: float) -> float:
    fetcher = object.__new__(EfinanceFetcher)
    raw = pd.DataFrame(
        [
            {
                "股票代码": "002270",
                "日期": "2026-08-04",
                "开盘": 20.20,
                "收盘": close,
                "最高": 20.90,
                "最低": 20.10,
                "成交量": raw_volume,
                "成交额": amount,
                "涨跌幅": 2.84,
            }
        ]
    )
    return float(fetcher._normalize_data(raw, "002270").iloc[0]["volume"])


def test_efinance_history_converts_lots_to_shares() -> None:
    # Action 中的真实量级：235747 手应统一为 23574700 股。
    assert _normalize(235_747, 485_032_900, 20.64) == 23_574_700


def test_efinance_history_does_not_double_convert_share_volume() -> None:
    # 上游若改为直接返回“股”，成交额/价格校验应阻止再次乘 100。
    assert _normalize(23_574_700, 485_032_900, 20.64) == 23_574_700
