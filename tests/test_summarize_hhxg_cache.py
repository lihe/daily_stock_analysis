from __future__ import annotations

import json

from scripts.summarize_hhxg_cache import load_manifest, render_summary


def test_render_summary_lists_scope_status_and_gap(tmp_path):
    path = tmp_path / "manifest.json"
    path.write_text(
        json.dumps(
            {
                "cache_id": "hhxg-test",
                "enabled": True,
                "scope_count": 2,
                "target_date": "2026-08-04",
                "fetched_at": "2026-08-04T06:20:00Z",
                "status_counts": {"ok": 1, "stale": 0, "unknown_date": 0, "unavailable": 1},
                "scopes": [
                    {
                        "scope": "snapshot",
                        "status": "ok",
                        "data_date": "2026-08-04",
                        "record_count": 1,
                        "error": "",
                    },
                    {
                        "scope": "risk-alerts",
                        "status": "unavailable",
                        "data_date": "",
                        "record_count": 0,
                        "error": "no_data",
                    },
                ],
            }
        ),
        encoding="utf-8",
    )

    summary = render_summary(load_manifest(path))

    assert "hhxg-test" in summary
    assert "scope_count: `2`" in summary
    assert "| snapshot | `ok` | 2026-08-04 | 1 | - |" in summary
    assert "| risk-alerts | `unavailable` | - | 0 | no_data |" in summary


def test_missing_manifest_is_reported_without_failure(tmp_path):
    manifest = load_manifest(tmp_path / "missing.json")

    assert manifest == {}
    assert "未生成 HHXG 缓存清单" in render_summary(manifest)
