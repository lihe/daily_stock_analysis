from __future__ import annotations

import json
from datetime import date

import requests

from src.services.hhxg_data_service import (
    HHXG_DATA_SCOPES,
    HHXGDataService,
    render_hhxg_data_evidence,
)


class FakeResponse:
    def __init__(self, payload, status_code=200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"HTTP {self.status_code}")

    def json(self):
        return self._payload


def test_prefetch_fetches_23_scopes_once_and_reuses_cache(tmp_path):
    calls = []

    def fake_get(url, *, headers, timeout):
        calls.append((url, headers, timeout))
        scope = url.rsplit("/", 1)[-1]
        return FakeResponse(
            {
                "success": True,
                "scope": scope,
                "fetched_at": "2026-08-04T06:20:00Z",
                "data": {
                    "items": [
                        {
                            "stock_code": "002270",
                            "trade_date": "2026-08-04",
                            "signal": scope,
                        }
                    ]
                },
            }
        )

    service = HHXGDataService(
        token="test-token",
        cache_dir=tmp_path,
        http_get=fake_get,
    )
    cache = service.prefetch(target_date=date(2026, 8, 4))

    assert len(calls) == len(HHXG_DATA_SCOPES) == 23
    assert {url.rsplit("/", 1)[-1] for url, _, _ in calls} == set(HHXG_DATA_SCOPES)
    assert all(headers["Authorization"] == "Bearer test-token" for _, headers, _ in calls)
    assert cache.status_counts == {"ok": 23, "stale": 0, "unknown_date": 0, "unavailable": 0}
    assert json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))["scope_count"] == 23
    assert all((tmp_path / f"{scope}.json").is_file() for scope in HHXG_DATA_SCOPES)

    evidence = service.build_stock_evidence(cache, "002270")
    first_prompt = service.format_stock_prompt(cache, "002270")
    second_prompt = service.format_stock_prompt(cache, "002270")

    assert len(calls) == 23
    assert evidence["cache_id"] == cache.cache_id
    assert evidence["matched_scopes"] == sorted(HHXG_DATA_SCOPES)
    assert "当前股票 002270" in first_prompt
    assert first_prompt == second_prompt


def test_missing_token_is_explicit_and_cleans_previous_scope_files(tmp_path):
    stale_path = tmp_path / "snapshot.json"
    stale_path.write_text('{"stale": true}\n', encoding="utf-8")
    service = HHXGDataService(token="", cache_dir=tmp_path)

    cache = service.prefetch(target_date=date(2026, 8, 4))

    manifest = json.loads((tmp_path / "manifest.json").read_text(encoding="utf-8"))
    assert cache.enabled is False
    assert cache.reason == "token_missing"
    assert manifest["reason"] == "token_missing"
    assert not stale_path.exists()


def test_auth_failure_is_not_retried(tmp_path):
    calls = []

    def fake_get(url, *, headers, timeout):
        calls.append(url)
        return FakeResponse({"success": False}, status_code=401)

    service = HHXGDataService(token="invalid", cache_dir=tmp_path, http_get=fake_get)
    cache = service.prefetch(target_date=date(2026, 8, 4), scopes=("snapshot",))

    assert calls == ["https://hhxg.top/api/data/snapshot"]
    assert cache.scopes["snapshot"].status == "unavailable"
    assert cache.scopes["snapshot"].error == "auth_failed"


def test_empty_data_and_stale_data_are_not_treated_as_current(tmp_path):
    def fake_get(url, *, headers, timeout):
        scope = url.rsplit("/", 1)[-1]
        if scope == "snapshot":
            data = []
        elif scope == "sentiment":
            data = {"items": [{"trade_date": "2026-08-05"}]}
        else:
            data = {"items": [{"trade_date": "2026-08-01"}]}
        return FakeResponse({"success": True, "scope": scope, "data": data})

    service = HHXGDataService(token="token", cache_dir=tmp_path, http_get=fake_get)
    cache = service.prefetch(
        target_date=date(2026, 8, 4),
        scopes=("snapshot", "breadth", "sentiment"),
    )

    assert cache.scopes["snapshot"].status == "unavailable"
    assert cache.scopes["snapshot"].error == "no_data"
    assert cache.scopes["breadth"].status == "stale"
    assert cache.scopes["sentiment"].status == "unknown_date"
    assert "trade_date" not in service.format_stock_prompt(cache, "002270")


def test_render_evidence_exposes_cache_and_gaps():
    rendered = render_hhxg_data_evidence(
        {
            "cache_id": "hhxg-test",
            "target_date": "2026-08-04",
            "cache_fetched_at": "2026-08-04T06:20:00Z",
            "status_counts": {"ok": 20, "stale": 1, "unknown_date": 1, "unavailable": 1},
            "matched_scopes": ["risk-alerts"],
            "gaps": {
                "stale": ["strategy"],
                "unknown_date": ["community-sentiment"],
                "unavailable": ["breadth"],
            },
        }
    )

    assert "HHXG Data API 共享缓存" in rendered
    assert "hhxg-test" in rendered
    assert "stale=strategy" in rendered
