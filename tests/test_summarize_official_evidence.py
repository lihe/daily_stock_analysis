from __future__ import annotations

import json

from scripts.summarize_official_evidence import load_evidence, render_summary


def test_summary_surfaces_partial_coverage(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "query-002270.json").write_text(
        json.dumps(
            {
                "stock_code": "002270",
                "stock_name": "华明装备",
                "status": "PARTIAL",
                "events": [{"event_type": "earnings"}],
                "sources": [
                    {"source": "szse_announcement", "status": "ok"},
                    {"source": "szse_regulatory", "status": "unavailable"},
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    summary = render_summary(load_evidence(evidence_dir))

    assert "002270 华明装备" in summary
    assert "`PARTIAL`" in summary
    assert "szse_regulatory" in summary
    assert "不能把缺失数据解释为无风险" in summary


def test_unreadable_evidence_is_reported_as_blocked(tmp_path):
    evidence_dir = tmp_path / "evidence"
    evidence_dir.mkdir()
    (evidence_dir / "broken.json").write_text("{", encoding="utf-8")

    summary = render_summary(load_evidence(evidence_dir))

    assert "`BLOCKED`" in summary
    assert "broken.json" in summary
