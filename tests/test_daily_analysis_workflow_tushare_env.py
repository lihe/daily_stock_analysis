# -*- coding: utf-8 -*-
"""Checks for Tushare-compatible endpoint mappings in the daily Action."""

from pathlib import Path

import yaml


ROOT_DIR = Path(__file__).resolve().parent.parent
WORKFLOW_PATH = ROOT_DIR / ".github/workflows/00-daily-analysis.yml"


def _load_daily_analysis_env() -> dict[str, str]:
    workflow = yaml.safe_load(WORKFLOW_PATH.read_text(encoding="utf-8"))
    steps = workflow["jobs"]["analyze"]["steps"]
    analyze_step = next(step for step in steps if step.get("name") == "执行股票分析")
    return analyze_step["env"]


def test_daily_analysis_maps_tushare_compatible_endpoint_settings() -> None:
    env = _load_daily_analysis_env()

    assert env["TUSHARE_TOKEN"] == "${{ secrets.TUSHARE_TOKEN }}"
    assert env["TUSHARE_API_URL"] == "${{ vars.TUSHARE_API_URL || 'http://api.tushare.pro' }}"
    assert env["TUSHARE_BYPASS_PROXY"] == "${{ vars.TUSHARE_BYPASS_PROXY || 'false' }}"
