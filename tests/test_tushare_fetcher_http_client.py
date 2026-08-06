# -*- coding: utf-8 -*-
"""Regression tests for TushareFetcher HTTP client initialization."""

import importlib.util
import json
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from tests.litellm_stub import ensure_litellm_stub

ensure_litellm_stub()

try:
    json_repair_available = importlib.util.find_spec("json_repair") is not None
except ValueError:
    json_repair_available = "json_repair" in sys.modules

if not json_repair_available and "json_repair" not in sys.modules:
    sys.modules["json_repair"] = MagicMock()

from data_provider.tushare_fetcher import TushareFetcher, _TushareHttpClient


class TestTushareHttpClient(unittest.TestCase):
    """Ensure the lightweight HTTP client preserves Tushare Pro request semantics."""

    def test_query_posts_to_official_pro_endpoint(self) -> None:
        client = _TushareHttpClient(token="demo-token", timeout=15)
        response = MagicMock(
            status_code=200,
            text=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "close"],
                        "items": [["600519.SH", 1688.0]],
                    },
                }
            ),
        )

        with patch("data_provider.tushare_fetcher.requests.post", return_value=response) as post_mock:
            df = client.daily(ts_code="600519.SH", start_date="20260320", end_date="20260325")

        post_mock.assert_called_once_with(
            "http://api.tushare.pro",
            json={
                "api_name": "daily",
                "token": "demo-token",
                "params": {
                    "ts_code": "600519.SH",
                    "start_date": "20260320",
                    "end_date": "20260325",
                },
                "fields": "",
            },
            timeout=15,
        )
        self.assertEqual(df.to_dict(orient="records"), [{"ts_code": "600519.SH", "close": 1688.0}])

    def test_query_uses_custom_endpoint_without_changing_global_requests(self) -> None:
        client = _TushareHttpClient(
            token="demo-token",
            timeout=12,
            api_url="https://tushare.example.test",
        )
        response = MagicMock(
            status_code=200,
            text=json.dumps(
                {
                    "code": 0,
                    "data": {
                        "fields": ["ts_code", "close"],
                        "items": [["000001.SZ", 10.5]],
                    },
                }
            ),
        )

        with (
            patch("data_provider.tushare_fetcher.requests.post", return_value=response) as post_mock,
            patch("data_provider.tushare_fetcher.requests.Session") as session_mock,
        ):
            df = client.daily(ts_code="000001.SZ")

        post_mock.assert_called_once_with(
            "https://tushare.example.test",
            json={
                "api_name": "daily",
                "token": "demo-token",
                "params": {"ts_code": "000001.SZ"},
                "fields": "",
            },
            timeout=12,
        )
        session_mock.assert_not_called()
        self.assertEqual(df.to_dict(orient="records"), [{"ts_code": "000001.SZ", "close": 10.5}])

    def test_bypass_proxy_uses_an_isolated_session(self) -> None:
        response = MagicMock(
            status_code=200,
            text=json.dumps({"code": 0, "data": {"fields": [], "items": []}}),
        )
        session = MagicMock()
        session.trust_env = True
        session.post.return_value = response

        with patch("data_provider.tushare_fetcher.requests.Session", return_value=session):
            client = _TushareHttpClient(
                token="demo-token",
                api_url="https://tushare.example.test",
                bypass_proxy=True,
            )
            client.trade_cal(exchange="SSE")

        self.assertFalse(session.trust_env)
        session.post.assert_called_once_with(
            "https://tushare.example.test",
            json={
                "api_name": "trade_cal",
                "token": "demo-token",
                "params": {"exchange": "SSE"},
                "fields": "",
            },
            timeout=30,
        )


class TestTushareFetcherInit(unittest.TestCase):
    """Ensure fetcher initialization no longer depends on the tushare SDK package."""

    def test_init_builds_http_client_when_token_present(self) -> None:
        config = SimpleNamespace(tushare_token="demo-token")

        with patch("data_provider.tushare_fetcher.get_config", return_value=config):
            fetcher = TushareFetcher()

        self.assertIsInstance(fetcher._api, _TushareHttpClient)
        self.assertTrue(fetcher.is_available())
        self.assertEqual(fetcher.priority, -1)

    def test_init_passes_custom_endpoint_and_proxy_scope_to_http_client(self) -> None:
        config = SimpleNamespace(
            tushare_token="demo-token",
            tushare_api_url="https://tushare.example.test",
            tushare_bypass_proxy=True,
        )
        session = MagicMock()
        session.trust_env = True

        with (
            patch("data_provider.tushare_fetcher.get_config", return_value=config),
            patch("data_provider.tushare_fetcher.requests.Session", return_value=session),
        ):
            fetcher = TushareFetcher()

        self.assertEqual(fetcher._api._api_url, "https://tushare.example.test")
        self.assertFalse(session.trust_env)


if __name__ == "__main__":
    unittest.main()
