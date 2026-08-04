#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize the run-level HHXG Data API cache for GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, List, Optional


def load_manifest(path: Path) -> Dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {"enabled": False, "reason": "manifest_unreadable"}
    return payload if isinstance(payload, dict) else {"enabled": False, "reason": "manifest_invalid"}


def render_summary(manifest: Dict[str, Any]) -> str:
    lines = ["## HHXG Data API 共享缓存", ""]
    if not manifest:
        lines.append("未生成 HHXG 缓存清单；market-only 模式或未进入股票分析时属于正常情况。")
        return "\n".join(lines)

    enabled = bool(manifest.get("enabled"))
    reason = _cell(manifest.get("reason") or "")
    lines.extend(
        [
            f"- cache_id: `{_cell(manifest.get('cache_id') or '')}`",
            f"- target_date: `{_cell(manifest.get('target_date') or '')}`",
            f"- fetched_at: `{_cell(manifest.get('fetched_at') or '')}`",
            f"- enabled: `{'true' if enabled else 'false'}`",
            f"- scope_count: `{_cell(manifest.get('scope_count') or 0)}`",
        ]
    )
    if reason:
        lines.append(f"- reason: `{reason}`")

    counts = manifest.get("status_counts")
    if isinstance(counts, dict):
        lines.append(
            "- status: "
            f"ok={counts.get('ok', 0)}, stale={counts.get('stale', 0)}, "
            f"unknown_date={counts.get('unknown_date', 0)}, unavailable={counts.get('unavailable', 0)}"
        )

    scopes = manifest.get("scopes")
    if isinstance(scopes, list) and scopes:
        lines.extend(["", "| Scope | 状态 | 数据日期 | 记录数 | 缺口 |", "| --- | --- | --- | ---: | --- |"])
        for item in scopes:
            if not isinstance(item, dict):
                continue
            lines.append(
                f"| {_cell(item.get('scope') or '')} | `{_cell(item.get('status') or 'unavailable')}` | "
                f"{_cell(item.get('data_date') or '-')} | {_cell(item.get('record_count') or 0)} | "
                f"{_cell(item.get('error') or '-')} |"
            )

    lines.extend(
        [
            "",
            "> HHXG 仅作市场、题材、技术和软风险辅助；stale、unknown_date、unavailable 均是数据缺口，不能解释为无风险。",
        ]
    )
    return "\n".join(lines)


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path("reports/evidence/hhxg_data/manifest.json"),
    )
    parser.add_argument(
        "--github-summary",
        type=Path,
        default=Path(os.environ["GITHUB_STEP_SUMMARY"]) if os.environ.get("GITHUB_STEP_SUMMARY") else None,
    )
    args = parser.parse_args(argv)

    summary = render_summary(load_manifest(args.manifest))
    print(summary)
    if args.github_summary:
        args.github_summary.parent.mkdir(parents=True, exist_ok=True)
        with args.github_summary.open("a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
