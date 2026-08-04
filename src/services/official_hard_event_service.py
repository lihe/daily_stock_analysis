# -*- coding: utf-8 -*-
"""Collect, persist, and enforce exchange-verified hard-event evidence."""

from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple

from src.schemas.hard_event import (
    OfficialHardEvent,
    OfficialHardEventEvidence,
    OfficialSourceStatus,
)
from src.services.exchange_disclosure_service import (
    ExchangeDisclosureClient,
    OfficialQueryResult,
)


logger = logging.getLogger(__name__)

_CST = timezone(timedelta(hours=8))
_HARD_EVENT_TERMS = (
    "停牌",
    "复牌",
    "停复牌",
    "监管",
    "处罚",
    "问询",
    "立案",
    "公告",
    "预告",
    "年报",
    "中报",
    "季报",
    "业绩变脸",
    "净利润",
    "营业收入",
    "监管函",
    "监管警示",
    "监管措施",
    "关注函",
    "问询函",
    "纪律处分",
    "公开谴责",
    "通报批评",
    "行政处罚",
    "立案调查",
    "减持",
    "业绩预告",
    "业绩快报",
    "业绩预增",
    "业绩预减",
    "业绩预亏",
    "业绩修正",
    "年度报告",
    "半年报",
    "半年度报告",
    "季度报告",
    "一季报",
    "三季报",
)
_REGULATION_TERMS = (
    "监管函",
    "监管警示",
    "关注函",
    "问询函",
    "纪律处分",
    "公开谴责",
    "通报批评",
    "行政处罚",
    "立案调查",
)
_EARNINGS_TERMS = (
    "业绩预告",
    "业绩快报",
    "业绩预增",
    "业绩预减",
    "业绩预亏",
    "业绩修正",
    "年度报告",
    "半年报",
    "半年度报告",
    "季度报告",
    "一季报",
    "三季报",
)
_SENTENCE_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")
_REPORT_PERIOD_PATTERNS = (
    re.compile(r"((?:19|20)\d{2})年(年度|半年度|第一季度|一季度|第三季度|三季度)"),
    re.compile(r"((?:19|20)\d{2})年度"),
)


class OfficialHardEventService:
    """Orchestrate exchange queries while preserving per-source completeness."""

    def __init__(
        self,
        client: Optional[ExchangeDisclosureClient] = None,
        *,
        lookback_days: int = 180,
        evidence_dir: Optional[Path] = None,
    ) -> None:
        self.client = client or ExchangeDisclosureClient()
        self.lookback_days = max(1, int(lookback_days))
        self.evidence_dir = evidence_dir or (
            Path(__file__).resolve().parents[2] / "reports" / "evidence"
        )

    def collect(
        self,
        code: str,
        stock_name: str,
        *,
        end_date: Optional[date] = None,
        query_id: Optional[str] = None,
    ) -> OfficialHardEventEvidence:
        normalized = str(code or "").strip()
        normalized_name = stock_name if isinstance(stock_name, str) else str(stock_name or "")
        query_end = end_date or datetime.now(_CST).date()
        query_start = query_end - timedelta(days=self.lookback_days)
        exchange = self.client.exchange_for_code(normalized)

        if exchange is None:
            evidence = OfficialHardEventEvidence(
                stock_code=normalized,
                stock_name=normalized_name,
                query_start=query_start.isoformat(),
                query_end=query_end.isoformat(),
                checked_at=datetime.now(_CST).isoformat(timespec="seconds"),
                status="NOT_APPLICABLE",
                notes=["该证券不属于当前正式交易所硬事件适配范围。"],
            )
            self._persist_safely(evidence, query_id=query_id)
            return evidence

        query_specs = self._query_specs(exchange)
        source_statuses: List[OfficialSourceStatus] = []
        records: List[Dict[str, Any]] = []

        for source, category, fetcher in query_specs:
            try:
                result = fetcher(normalized, start_date=query_start, end_date=query_end)
                if not isinstance(result, OfficialQueryResult):
                    raise TypeError("official adapter returned an invalid result")
                records.extend(result.records)
                source_statuses.append(
                    OfficialSourceStatus(
                        source=source,
                        category=category,
                        status="ok",
                        record_count=len(result.records),
                    )
                )
            except Exception as exc:
                logger.warning(
                    "正式交易所数据查询失败: code=%s source=%s error=%s",
                    normalized,
                    source,
                    exc,
                )
                source_statuses.append(
                    OfficialSourceStatus(
                        source=source,
                        category=category,
                        status="unavailable",
                        error=f"{type(exc).__name__}: {str(exc)[:300]}",
                    )
                )

        events = _classify_records(records, normalized)
        successful = sum(1 for item in source_statuses if item.status == "ok")
        if successful == 0:
            status = "BLOCKED"
        elif successful < len(source_statuses):
            status = "PARTIAL"
        elif events:
            status = "VERIFIED"
        else:
            status = "CLEAN"

        notes: List[str] = []
        if status in {"PARTIAL", "BLOCKED"}:
            notes.append("正式交易所查询不完整，不能据此认定未发现硬事件。")
        elif status == "CLEAN":
            notes.append("查询窗口内未发现已配置类型的硬事件；该结论不代表股票整体无风险。")
        else:
            notes.append("硬事件仅依据交易所元数据确认，正文数值未下载核验。")

        evidence = OfficialHardEventEvidence(
            stock_code=normalized,
            stock_name=normalized_name,
            query_start=query_start.isoformat(),
            query_end=query_end.isoformat(),
            checked_at=datetime.now(_CST).isoformat(timespec="seconds"),
            status=status,
            events=events,
            sources=source_statuses,
            notes=notes,
        )
        self._persist_safely(evidence, query_id=query_id)
        return evidence

    def unavailable_evidence(
        self,
        code: str,
        stock_name: str,
        *,
        end_date: Optional[date] = None,
        error: str,
        query_id: Optional[str] = None,
    ) -> OfficialHardEventEvidence:
        query_end = end_date or datetime.now(_CST).date()
        evidence = OfficialHardEventEvidence(
            stock_code=str(code or "").strip(),
            stock_name=stock_name if isinstance(stock_name, str) else str(stock_name or ""),
            query_start=(query_end - timedelta(days=self.lookback_days)).isoformat(),
            query_end=query_end.isoformat(),
            checked_at=datetime.now(_CST).isoformat(timespec="seconds"),
            status="BLOCKED",
            sources=[
                OfficialSourceStatus(
                    source="official_hard_event_service",
                    category="all",
                    status="unavailable",
                    error=error[:300],
                )
            ],
            notes=["正式交易所查询未执行完成，不能据此认定未发现硬事件。"],
        )
        self._persist_safely(evidence, query_id=query_id)
        return evidence

    def _persist_safely(
        self,
        evidence: OfficialHardEventEvidence,
        *,
        query_id: Optional[str],
    ) -> None:
        try:
            self.persist(evidence, query_id=query_id)
        except OSError as exc:
            logger.warning(
                "正式交易所证据文件写入失败: code=%s error=%s",
                evidence.stock_code,
                exc,
            )

    def persist(
        self,
        evidence: OfficialHardEventEvidence,
        *,
        query_id: Optional[str],
    ) -> Path:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        safe_query_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", str(query_id or "run"))
        target = self.evidence_dir / f"{safe_query_id}-{evidence.stock_code}.json"
        temp = target.with_suffix(target.suffix + ".tmp")
        temp.write_text(
            json.dumps(evidence.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temp.replace(target)
        return target

    def _query_specs(
        self,
        exchange: str,
    ) -> List[Tuple[str, str, Callable[..., OfficialQueryResult]]]:
        if exchange == "szse":
            return [
                ("szse_announcement", "announcement", self.client.fetch_szse_announcements),
                ("szse_regulatory", "regulation", self.client.fetch_szse_regulatory_measures),
                ("szse_disciplinary", "regulation", self.client.fetch_szse_disciplinary_actions),
            ]
        return [
            ("sse_announcement", "announcement", self.client.fetch_sse_announcements),
            ("sse_regulatory", "regulation", self.client.fetch_sse_regulatory),
        ]


def format_official_hard_event_prompt(evidence: OfficialHardEventEvidence) -> str:
    """Build an authoritative prompt section without adding inferred facts."""
    if evidence.status == "NOT_APPLICABLE":
        return ""
    lines = [
        "## 正式交易所硬事件（唯一硬事实来源）",
        f"- 核验状态：{evidence.status}",
        f"- 查询窗口：{evidence.query_start} 至 {evidence.query_end}",
        "- 规则：停牌、监管、业绩报告/预告、减持只能引用本节；通用新闻只能作为线索。",
        "- 规则：发布日期不等于报告期；不得根据标题补写公告正文中的金额、比例或日期。",
    ]
    if evidence.events:
        lines.append("- 已确认事件：")
        for event in evidence.events[:20]:
            period = f"；报告期={event.report_period}" if event.report_period else ""
            lines.append(
                f"  - {event.publish_date} | {event.event_type}/{event.subtype} | "
                f"{event.title}{period} | {event.source_url or '无链接'}"
            )
    elif evidence.coverage_complete:
        lines.append("- 已确认事件：查询窗口内未发现已配置类型的硬事件。")
    else:
        lines.append("- 已确认事件：查询不完整，风险状态未知。")
    return "\n".join(lines)


def render_official_hard_event_evidence(
    evidence: Any,
    *,
    report_language: str = "zh",
) -> str:
    """Render the hard-event section directly from persisted evidence."""
    if not isinstance(evidence, dict):
        return ""
    status = str(evidence.get("status") or "BLOCKED")
    if status == "NOT_APPLICABLE":
        return ""
    events = evidence.get("events") if isinstance(evidence.get("events"), list) else []
    sources = evidence.get("sources") if isinstance(evidence.get("sources"), list) else []
    is_en = str(report_language or "zh").lower().startswith("en")

    heading = "### Official Exchange Hard Events" if is_en else "### 正式交易所硬事件"
    status_label = "Verification status" if is_en else "核验状态"
    window_label = "Window" if is_en else "查询窗口"
    lines = [
        heading,
        "",
        f"**{status_label}**: `{status}`",
        f"**{window_label}**: {evidence.get('query_start', 'N/A')} - {evidence.get('query_end', 'N/A')}",
    ]

    if events:
        lines.append("")
        for item in events[:20]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "N/A")
            url = str(item.get("source_url") or "")
            linked_title = f"[{title}]({url})" if url else title
            period = item.get("report_period")
            period_text = f" | report period: {period}" if is_en and period else ""
            if period and not is_en:
                period_text = f" | 报告期：{period}"
            lines.append(
                f"- {item.get('publish_date', 'N/A')} | "
                f"{item.get('event_type', 'other')}/{item.get('subtype', 'other')} | "
                f"{linked_title}{period_text}"
            )
    elif status in {"CLEAN", "NOT_APPLICABLE"}:
        lines.extend(
            [
                "",
                (
                    "- No configured hard event was found in the completed query window."
                    if is_en
                    else "- 已完成查询的窗口内未发现已配置类型的硬事件。"
                ),
            ]
        )
    else:
        lines.extend(
            [
                "",
                (
                    "- Official-source coverage is incomplete; risk remains unknown."
                    if is_en
                    else "- 正式交易所查询不完整，风险状态未知，不能认定为安全。"
                ),
            ]
        )

    source_states = [
        f"{item.get('source')}={item.get('status')}"
        for item in sources
        if isinstance(item, dict) and item.get("source")
    ]
    if source_states:
        lines.extend(["", f"*Sources: {', '.join(source_states)}*"])
    return "\n".join(lines)


def apply_official_hard_event_guardrail(
    result: Any,
    evidence: OfficialHardEventEvidence,
) -> List[str]:
    """Remove LLM-authored hard facts and enforce fail-closed risk handling."""
    adjustments: List[str] = []
    result.official_hard_event_evidence = evidence.to_dict()
    if evidence.status == "NOT_APPLICABLE":
        return adjustments

    replacement = (
        "Hard-event facts are shown only in the official exchange section."
        if str(getattr(result, "report_language", "zh")).lower().startswith("en")
        else "硬事件事实仅以正式交易所核验章节为准。"
    )

    for field_name in (
        "news_summary",
        "risk_warning",
        "fundamental_analysis",
        "company_highlights",
        "analysis_summary",
        "key_points",
        "buy_reason",
        "short_term_outlook",
        "medium_term_outlook",
    ):
        value = getattr(result, field_name, None)
        sanitized, changed = _sanitize_generated_value(value, replacement)
        if changed:
            setattr(result, field_name, sanitized)
            adjustments.append(f"sanitized:{field_name}")

    dashboard = getattr(result, "dashboard", None)
    if isinstance(dashboard, dict):
        sanitized_dashboard, changed = _sanitize_generated_value(dashboard, replacement)
        if changed and isinstance(sanitized_dashboard, dict):
            result.dashboard = sanitized_dashboard
            dashboard = sanitized_dashboard
            adjustments.append("sanitized:dashboard")

    if evidence.status in {"PARTIAL", "BLOCKED"}:
        is_en = str(getattr(result, "report_language", "zh")).lower().startswith("en")
        result.confidence_level = "Low" if is_en else "低"
        result.operation_advice = "Watch" if is_en else "观望"
        result.decision_type = "hold"
        result.action = "watch"
        result.action_label = "Watch" if is_en else "观望"
        dashboard = result.dashboard if isinstance(getattr(result, "dashboard", None), dict) else {}
        result.dashboard = dashboard
        core = dashboard.get("core_conclusion")
        if not isinstance(core, dict):
            core = {}
            dashboard["core_conclusion"] = core
        limitation = (
            "Official exchange hard-event coverage is incomplete; do not open a new position."
            if is_en
            else "正式交易所硬事件查询不完整，不新增仓位。"
        )
        core["one_sentence"] = limitation
        position_advice = core.get("position_advice")
        if not isinstance(position_advice, dict):
            position_advice = {}
            core["position_advice"] = position_advice
        position_advice["no_position"] = limitation
        phase_decision = dashboard.get("phase_decision")
        if not isinstance(phase_decision, dict):
            phase_decision = {}
            dashboard["phase_decision"] = phase_decision
        limitations = phase_decision.get("data_limitations")
        if not isinstance(limitations, list):
            limitations = []
        if limitation not in limitations:
            limitations.append(limitation)
        phase_decision["data_limitations"] = limitations
        adjustments.append(f"fail_closed:{evidence.status}")

    return adjustments


def sanitize_unverified_hard_event_context(value: Any) -> Any:
    """Remove hard-event claims from non-official text before LLM ingestion."""
    if not isinstance(value, str) or not value.strip():
        return value
    parts = _SENTENCE_SPLIT.split(value)
    kept = [part for part in parts if part and not _contains_hard_event_term(part)]
    return "".join(kept).strip()


def _classify_records(records: Iterable[Dict[str, Any]], code: str) -> List[OfficialHardEvent]:
    events: List[OfficialHardEvent] = []
    seen: set[Tuple[str, str, str, str]] = set()
    for record in records:
        title = str(record.get("title") or "").strip()
        if not title:
            continue
        event_type, subtype = _classify_record(record, title)
        if event_type is None:
            continue
        publish_date = str(record.get("publish_date") or "")
        source = str(record.get("source") or "official_exchange")
        source_url = str(record.get("source_url") or "")
        identity = (source, source_url or title, event_type, subtype)
        if identity in seen:
            continue
        seen.add(identity)
        external_id = str(record.get("external_id") or source_url or title)
        digest = hashlib.sha256(
            f"{source}|{external_id}|{event_type}|{subtype}".encode("utf-8")
        ).hexdigest()[:20]
        events.append(
            OfficialHardEvent(
                event_id=digest,
                stock_code=code,
                event_type=event_type,
                subtype=subtype,
                title=title,
                publish_date=publish_date,
                event_date=str(record.get("event_date") or "") or None,
                report_period=_extract_report_period(title) if event_type == "earnings" else None,
                source=source,
                source_url=source_url,
                raw_fields=(
                    dict(record.get("raw_fields") or {})
                    if isinstance(record.get("raw_fields"), dict)
                    else {}
                ),
            )
        )
    events.sort(key=lambda item: (item.publish_date, item.event_id), reverse=True)
    return events


def _classify_record(record: Dict[str, Any], title: str) -> Tuple[Optional[str], str]:
    if str(record.get("category") or "") == "regulation":
        return "regulation", str(record.get("subtype") or "监管措施")
    if "停牌" in title or "复牌" in title:
        return "suspension", "复牌" if "复牌" in title and "停牌" not in title else "停牌"
    if any(term in title for term in _REGULATION_TERMS):
        subtype = next((term for term in _REGULATION_TERMS if term in title), "监管事项")
        return "regulation", subtype
    if "减持" in title:
        if "被动减持" in title:
            subtype = "被动减持"
        elif "未减持" in title and "期限届满" in title:
            subtype = "减持期限届满（未减持）"
        elif any(term in title for term in ("完成", "结果", "实施完毕", "期限届满")):
            subtype = "减持完成"
        elif any(term in title for term in ("进展", "实施情况", "减持时间过半")):
            subtype = "减持进展"
        elif any(term in title for term in ("计划", "预披露")):
            subtype = "减持计划"
        else:
            subtype = "减持事项"
        return "reduction", subtype
    if "业绩说明会" in title:
        return None, ""
    if any(term in title for term in _EARNINGS_TERMS):
        if any(term in title for term in ("摘要", "英文版")):
            return None, ""
        if "业绩修正" in title:
            subtype = "业绩修正"
        elif "业绩快报" in title:
            subtype = "业绩快报"
        elif any(term in title for term in ("业绩预告", "业绩预增", "业绩预减", "业绩预亏")):
            subtype = "业绩预告"
        elif "半年度报告" in title or "半年报" in title:
            subtype = "半年度报告"
        elif any(term in title for term in ("季度报告", "一季报", "三季报")):
            subtype = "季度报告"
        else:
            subtype = "年度报告"
        return "earnings", subtype
    return None, ""


def _extract_report_period(title: str) -> Optional[str]:
    for pattern in _REPORT_PERIOD_PATTERNS:
        match = pattern.search(title)
        if match:
            suffix = match.group(2) if match.lastindex and match.lastindex >= 2 else "年度"
            if suffix == "年度":
                return f"{match.group(1)}年度"
            return f"{match.group(1)}年{suffix}"
    return None


def _sanitize_generated_value(value: Any, replacement: str) -> Tuple[Any, bool]:
    if isinstance(value, str):
        if not _contains_hard_event_term(value):
            return value, False
        parts = _SENTENCE_SPLIT.split(value)
        kept = [part for part in parts if part and not _contains_hard_event_term(part)]
        sanitized = "".join(kept).strip()
        if sanitized:
            sanitized = f"{sanitized} {replacement}"
        else:
            sanitized = replacement
        return sanitized, True
    if isinstance(value, list):
        sanitized_list: List[Any] = []
        changed = False
        for item in value:
            sanitized, item_changed = _sanitize_generated_value(item, replacement)
            changed = changed or item_changed
            if item_changed and sanitized == replacement:
                continue
            if sanitized not in (None, "", [], {}):
                sanitized_list.append(sanitized)
        return sanitized_list, changed
    if isinstance(value, dict):
        sanitized_dict: Dict[str, Any] = {}
        changed = False
        for key, item in value.items():
            sanitized, item_changed = _sanitize_generated_value(item, replacement)
            sanitized_dict[key] = sanitized
            changed = changed or item_changed
        return sanitized_dict, changed
    return value, False


def _contains_hard_event_term(value: str) -> bool:
    return any(term in value for term in _HARD_EVENT_TERMS)
