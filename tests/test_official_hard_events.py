from __future__ import annotations

from datetime import date
from types import SimpleNamespace

from src.services.exchange_disclosure_service import OfficialQueryResult
from src.services.official_hard_event_service import (
    OfficialHardEventService,
    apply_official_hard_event_guardrail,
    format_official_hard_event_prompt,
    render_official_hard_event_evidence,
    sanitize_unverified_hard_event_context,
)


class _CompleteSzseClient:
    @staticmethod
    def exchange_for_code(code: str) -> str:
        return "szse"

    @staticmethod
    def fetch_szse_announcements(code, *, start_date, end_date):
        return OfficialQueryResult(
            source="szse_announcement",
            category="announcement",
            records=[
                {
                    "source": "szse_announcement",
                    "category": "announcement",
                    "external_id": "ann-1",
                    "stock_code": code,
                    "title": "示例公司：2025年半年度报告",
                    "publish_date": "2026-07-10",
                    "source_url": "https://disc.static.szse.cn/example.pdf",
                    "raw_fields": {},
                }
            ],
        )

    @staticmethod
    def fetch_szse_regulatory_measures(code, *, start_date, end_date):
        return OfficialQueryResult("szse_regulatory", "regulation", [])

    @staticmethod
    def fetch_szse_disciplinary_actions(code, *, start_date, end_date):
        return OfficialQueryResult("szse_disciplinary", "regulation", [])


class _PartialSzseClient(_CompleteSzseClient):
    @staticmethod
    def fetch_szse_regulatory_measures(code, *, start_date, end_date):
        raise TimeoutError("exchange timeout")


class _ReductionSzseClient(_CompleteSzseClient):
    @staticmethod
    def fetch_szse_announcements(code, *, start_date, end_date):
        return OfficialQueryResult(
            source="szse_announcement",
            category="announcement",
            records=[
                {
                    "source": "szse_announcement",
                    "category": "announcement",
                    "external_id": "ann-reduction",
                    "stock_code": code,
                    "title": "关于减持计划期限届满未减持公司股份的公告",
                    "publish_date": "2026-06-18",
                    "source_url": "https://disc.static.szse.cn/reduction.pdf",
                    "raw_fields": {},
                }
            ],
        )


def _analysis_result() -> SimpleNamespace:
    return SimpleNamespace(
        code="002270",
        name="示例公司",
        sentiment_score=80,
        trend_prediction="看多",
        operation_advice="买入",
        decision_type="buy",
        confidence_level="高",
        analysis_summary="公司今日停牌，短期值得买入。",
        news_summary="2025年半年报是当前最新业绩事件。",
        risk_warning="近期收到监管函。",
        dashboard={
            "core_conclusion": {
                "one_sentence": "公司停牌后即将复牌，可积极买入。",
                "position_advice": {"no_position": "买入", "has_position": "持有"},
            },
            "intelligence": {
                "risk_alerts": ["2026-08-04 公司停牌", "行业需求走弱"],
                "earnings_outlook": "2025年半年报显示当前业绩增长",
                "positive_catalysts": ["公司预告上半年归母净利润同比增长"],
                "latest_news": "公司公告目前无监管处罚或业绩变脸硬事实",
            },
        },
    )


def test_report_period_is_independent_from_publish_date(tmp_path):
    service = OfficialHardEventService(
        client=_CompleteSzseClient(),
        evidence_dir=tmp_path,
    )

    evidence = service.collect(
        "002270",
        "示例公司",
        end_date=date(2026, 8, 4),
        query_id="query-1",
    )

    assert evidence.status == "VERIFIED"
    assert len(evidence.events) == 1
    event = evidence.events[0]
    assert event.publish_date == "2026-07-10"
    assert event.report_period == "2025年半年度"
    assert "2026年半年度" not in format_official_hard_event_prompt(evidence)
    assert (tmp_path / "query-1-002270.json").exists()


def test_partial_official_coverage_fails_closed_and_removes_llm_hard_facts(tmp_path):
    service = OfficialHardEventService(
        client=_PartialSzseClient(),
        evidence_dir=tmp_path,
    )
    evidence = service.collect(
        "002270",
        "示例公司",
        end_date=date(2026, 8, 4),
        query_id="query-2",
    )
    result = _analysis_result()

    adjustments = apply_official_hard_event_guardrail(result, evidence)

    assert evidence.status == "PARTIAL"
    assert result.operation_advice == "观望"
    assert result.decision_type == "hold"
    assert result.confidence_level == "低"
    assert "fail_closed:PARTIAL" in adjustments
    assert "停牌" not in result.analysis_summary
    assert "半年报" not in result.news_summary
    assert "监管函" not in result.risk_warning
    assert result.dashboard["intelligence"]["risk_alerts"] == ["行业需求走弱"]
    assert result.dashboard["intelligence"]["positive_catalysts"] == []
    assert "监管处罚" not in result.dashboard["intelligence"]["latest_news"]
    assert result.official_hard_event_evidence["status"] == "PARTIAL"


def test_rendered_section_comes_from_official_evidence(tmp_path):
    service = OfficialHardEventService(
        client=_CompleteSzseClient(),
        evidence_dir=tmp_path,
    )
    evidence = service.collect(
        "002270",
        "示例公司",
        end_date=date(2026, 8, 4),
        query_id="query-3",
    )

    rendered = render_official_hard_event_evidence(evidence.to_dict())

    assert "正式交易所硬事件" in rendered
    assert "2026-07-10" in rendered
    assert "报告期：2025年半年度" in rendered
    assert "https://disc.static.szse.cn/example.pdf" in rendered


def test_clean_requires_every_official_query_to_succeed(tmp_path):
    service = OfficialHardEventService(
        client=_CompleteSzseClient(),
        evidence_dir=tmp_path,
    )
    service.client.fetch_szse_announcements = lambda *args, **kwargs: OfficialQueryResult(
        "szse_announcement",
        "announcement",
        [],
    )

    evidence = service.collect(
        "002270",
        "示例公司",
        end_date=date(2026, 8, 4),
        query_id="query-4",
    )

    assert evidence.status == "CLEAN"
    assert evidence.coverage_complete is True
    assert all(source.status == "ok" for source in evidence.sources)


def test_reduction_expiry_without_sale_is_not_labeled_as_a_new_plan(tmp_path):
    service = OfficialHardEventService(
        client=_ReductionSzseClient(),
        evidence_dir=tmp_path,
    )

    evidence = service.collect(
        "002315",
        "示例公司",
        end_date=date(2026, 8, 4),
        query_id="query-5",
    )

    assert evidence.status == "VERIFIED"
    assert evidence.events[0].subtype == "减持期限届满（未减持）"


def test_non_a_share_evidence_does_not_rewrite_report(tmp_path):
    service = OfficialHardEventService(
        client=_CompleteSzseClient(),
        evidence_dir=tmp_path,
    )
    service.client.exchange_for_code = lambda code: None
    evidence = service.collect(
        "AAPL",
        "Apple",
        end_date=date(2026, 8, 4),
        query_id="query-6",
    )
    result = _analysis_result()

    adjustments = apply_official_hard_event_guardrail(result, evidence)

    assert evidence.status == "NOT_APPLICABLE"
    assert adjustments == []
    assert "停牌" in result.analysis_summary
    assert format_official_hard_event_prompt(evidence) == ""
    assert render_official_hard_event_evidence(evidence.to_dict()) == ""


def test_annual_report_period_is_rendered_without_duplicate_year_marker(tmp_path):
    client = _CompleteSzseClient()
    client.fetch_szse_announcements = lambda *args, **kwargs: OfficialQueryResult(
        "szse_announcement",
        "announcement",
        [
            {
                "source": "szse_announcement",
                "category": "announcement",
                "external_id": "annual-report",
                "stock_code": "002270",
                "title": "示例公司2025年年度报告",
                "publish_date": "2026-03-30",
                "source_url": "https://disc.static.szse.cn/annual.pdf",
            }
        ],
    )
    evidence = OfficialHardEventService(client=client, evidence_dir=tmp_path).collect(
        "002270",
        "示例公司",
        end_date=date(2026, 8, 4),
        query_id="query-7",
    )

    assert evidence.events[0].report_period == "2025年度"


def test_unverified_news_hard_events_are_removed_before_llm():
    news = (
        "2026-08-04 行业需求改善。\n"
        "2026-07-22 公司预告上半年归母净利润同比增长。\n"
        "2026-08-03 券商观点认为估值合理。"
    )

    sanitized = sanitize_unverified_hard_event_context(news)

    assert "行业需求改善" in sanitized
    assert "券商观点认为估值合理" in sanitized
    assert "预告" not in sanitized
    assert "净利润" not in sanitized


def test_unverified_hard_event_synonyms_are_removed_without_hiding_trading_advice():
    text = (
        "业绩预期承压。"
        "当前动态市盈率为负，公司处于亏损状态。"
        "公司收到警示函并被责令整改。"
        "股票被实施退市风险警示。"
        "审计报告被出具保留意见。"
        "控股股东拟出售公司股份。"
        "持仓浮动亏损达到3%，应执行止损。"
        "走势走弱时应减仓。"
        "行业景气预期承压。"
    )

    sanitized = sanitize_unverified_hard_event_context(text)

    for leaked_claim in (
        "业绩预期承压",
        "公司处于亏损状态",
        "警示函",
        "责令整改",
        "退市风险警示",
        "保留意见",
        "控股股东拟出售公司股份",
    ):
        assert leaked_claim not in sanitized
    assert "持仓浮动亏损达到3%" in sanitized
    assert "走势走弱时应减仓" in sanitized
    assert "行业景气预期承压" in sanitized


def test_unverified_english_hard_event_synonyms_are_removed():
    text = (
        "The company received a regulatory inquiry. "
        "Management issued a profit warning. "
        "The position has an unrealized loss of 3%. "
        "Sector demand remains strong."
    )

    sanitized = sanitize_unverified_hard_event_context(text)

    assert "regulatory inquiry" not in sanitized
    assert "profit warning" not in sanitized
    assert "unrealized loss of 3%" in sanitized
    assert "Sector demand remains strong" in sanitized


def test_guardrail_sanitizes_every_user_visible_narrative_field(tmp_path):
    evidence = OfficialHardEventService(
        client=_CompleteSzseClient(),
        evidence_dir=tmp_path,
    ).collect(
        "002270",
        "示例公司",
        end_date=date(2026, 8, 4),
        query_id="query-visible-fields",
    )
    result = _analysis_result()
    narrative_fields = (
        "trend_analysis",
        "technical_analysis",
        "ma_analysis",
        "volume_analysis",
        "pattern_analysis",
        "sector_position",
        "market_sentiment",
        "hot_topics",
    )
    for field_name in narrative_fields:
        setattr(result, field_name, "公司收到警示函。技术趋势保持强势。")

    adjustments = apply_official_hard_event_guardrail(result, evidence)

    for field_name in narrative_fields:
        value = getattr(result, field_name)
        assert "警示函" not in value
        assert "技术趋势保持强势" in value
        assert f"sanitized:{field_name}" in adjustments
