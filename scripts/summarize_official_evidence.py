#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Summarize persisted official-exchange evidence for GitHub Actions."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional


def load_evidence(evidence_dir: Path) -> List[Dict[str, Any]]:
    records: List[Dict[str, Any]] = []
    if not evidence_dir.is_dir():
        return records

    for path in sorted(evidence_dir.glob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            records.append(
                {
                    "stock_code": path.stem,
                    "stock_name": "证据文件不可读",
                    "status": "BLOCKED",
                    "events": [],
                    "sources": [
                        {
                            "source": path.name,
                            "status": "unavailable",
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    ],
                }
            )
            continue
        if isinstance(payload, dict):
            records.append(payload)
    return records


def render_summary(records: Iterable[Dict[str, Any]]) -> str:
    items = list(records)
    lines = ["## 正式交易所硬事件核验", ""]
    if not items:
        lines.append("未生成股票级正式交易所证据；market-only 模式属于正常情况。")
        return "\n".join(lines)

    lines.extend(
        [
            "| 股票 | 状态 | 硬事件数 | 查询缺口 |",
            "| --- | --- | ---: | --- |",
        ]
    )
    for item in items:
        code = _cell(item.get("stock_code") or "N/A")
        name = _cell(item.get("stock_name") or "")
        status = _cell(item.get("status") or "BLOCKED")
        events = item.get("events")
        event_count = len(events) if isinstance(events, list) else 0
        gaps = _source_gaps(item.get("sources"))
        stock = f"{code} {name}".strip()
        lines.append(f"| {stock} | `{status}` | {event_count} | {gaps} |")

    blocked = sum(
        1 for item in items if str(item.get("status") or "") in {"PARTIAL", "BLOCKED"}
    )
    lines.append("")
    if blocked:
        lines.append(
            f"> {blocked} 只股票的官方查询不完整；对应分析必须保持观望，不能把缺失数据解释为无风险。"
        )
    else:
        lines.append("> 所有股票的已配置正式交易所查询均已完成。")
    return "\n".join(lines)


def _source_gaps(value: Any) -> str:
    if not isinstance(value, list):
        return "来源状态缺失"
    gaps = []
    for source in value:
        if not isinstance(source, dict) or source.get("status") == "ok":
            continue
        gaps.append(str(source.get("source") or "unknown"))
    return _cell(", ".join(gaps) if gaps else "无")


def _cell(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ").strip()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--evidence-dir", type=Path, default=Path("reports/evidence"))
    parser.add_argument(
        "--github-summary",
        type=Path,
        default=Path(os.environ["GITHUB_STEP_SUMMARY"])
        if os.environ.get("GITHUB_STEP_SUMMARY")
        else None,
    )
    args = parser.parse_args(argv)

    summary = render_summary(load_evidence(args.evidence_dir))
    print(summary)
    if args.github_summary:
        args.github_summary.parent.mkdir(parents=True, exist_ok=True)
        with args.github_summary.open("a", encoding="utf-8") as handle:
            handle.write(summary + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
