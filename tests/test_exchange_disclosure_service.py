from __future__ import annotations

from datetime import date

import pytest
import requests

from src.services.exchange_disclosure_service import (
    ExchangeDisclosureClient,
    OfficialSourceError,
)


class _Response:
    def __init__(self, payload=None, *, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code

    def raise_for_status(self):
        if self.status_code >= 400:
            raise requests.HTTPError(f"{self.status_code} response")

    def json(self):
        return self._payload


def test_sse_announcement_uses_cninfo_when_sse_https_is_forbidden():
    calls = []

    def requester(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.startswith("https://query.sse.com.cn/"):
            return _Response(status_code=403)
        if url.endswith("/new/data/szse_stock.json"):
            return _Response(
                {
                    "stockList": [
                        {"code": "600797", "orgId": "gssh0600797", "zwjc": "浙大网新"}
                    ]
                }
            )
        if url.endswith("/new/hisAnnouncement/query"):
            return _Response(
                {
                    "announcements": [
                        {
                            "secCode": "600797",
                            "orgId": "gssh0600797",
                            "announcementId": "1225434581",
                            "announcementTitle": (
                                "浙大网新科技股份有限公司董事会决议公告"
                            ),
                            "announcementTime": 1784649600000,
                            "adjunctUrl": "finalpage/2026-07-22/1225434581.PDF",
                            "announcementType": "01010503",
                        }
                    ],
                    "totalpages": 1,
                    "hasMore": False,
                }
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    client = ExchangeDisclosureClient(retries=1, requester=requester)

    result = client.fetch_sse_announcements(
        "600797",
        start_date=date(2026, 2, 6),
        end_date=date(2026, 8, 5),
    )

    assert result.source == "sse_announcement"
    assert result.records == [
        {
            "source": "sse_announcement",
            "category": "announcement",
            "external_id": "1225434581",
            "stock_code": "600797",
            "title": "浙大网新科技股份有限公司董事会决议公告",
            "publish_date": "2026-07-22",
            "source_url": "https://static.cninfo.com.cn/finalpage/2026-07-22/1225434581.PDF",
            "raw_fields": {
                "provider": "cninfo",
                "org_id": "gssh0600797",
                "announcement_type": "01010503",
            },
        }
    ]
    cninfo_query = next(call for call in calls if call[1].endswith("/new/hisAnnouncement/query"))
    assert cninfo_query[0] == "POST"
    assert cninfo_query[2]["data"]["stock"] == "600797,gssh0600797"
    assert cninfo_query[2]["data"]["column"] == "sse"


def test_sse_regulatory_retries_same_official_endpoint_over_http_after_https_403():
    calls = []

    def requester(method, url, **kwargs):
        calls.append((method, url, kwargs))
        if url.startswith("https://query.sse.com.cn/"):
            return _Response(status_code=403)
        if url.startswith("http://query.sse.com.cn/"):
            return _Response(
                {
                    "result": [
                        {
                            "extSECURITY_CODE": "600363",
                            "docId": "10827946_28_10010",
                            "docTitle": (
                                "就公司及实际控制人涉嫌非经营性资金往来等事项"
                                "明确监管要求"
                            ),
                            "createTime": "2026-08-04 22:30:56",
                            "extTYPE": "监管工作函",
                            "extWTFL": "监管工作函",
                            "docURL": (
                                "www.sse.com.cn/disclosure/credibility/supervision/"
                                "measures/other/c/10827946/files/example.doc"
                            ),
                            "channelId": "10010",
                            "extTeacher": "上市公司",
                        }
                    ]
                }
            )
        raise AssertionError(f"unexpected request: {method} {url}")

    client = ExchangeDisclosureClient(retries=1, requester=requester)

    result = client.fetch_sse_regulatory(
        "600363",
        start_date=date(2026, 2, 6),
        end_date=date(2026, 8, 5),
    )

    assert [call[1] for call in calls] == [
        "https://query.sse.com.cn/commonSoaQuery.do",
        "http://query.sse.com.cn/commonSoaQuery.do",
    ]
    assert result.records[0]["source_url"].startswith("https://www.sse.com.cn/")
    assert result.records[0]["raw_fields"]["transport"] == "http_fallback"


def test_cninfo_pagination_uses_has_more_instead_of_incorrect_totalpages():
    announcement_pages = []

    def requester(method, url, **kwargs):
        if url.startswith("https://query.sse.com.cn/"):
            return _Response(status_code=403)
        if url.endswith("/new/data/szse_stock.json"):
            return _Response(
                {"stockList": [{"code": "600797", "orgId": "gssh0600797"}]}
            )
        page_num = int(kwargs["data"]["pageNum"])
        announcement_pages.append(page_num)
        return _Response(
            {
                "announcements": [
                    {
                        "secCode": "600797",
                        "orgId": "gssh0600797",
                        "announcementId": f"page-{page_num}",
                        "announcementTitle": f"第{page_num}页公告",
                        "announcementTime": 1784649600000,
                        "adjunctUrl": f"finalpage/page-{page_num}.PDF",
                    }
                ],
                # 该字段在真实接口中可能小于实际页数，不能作为完成依据。
                "totalpages": 1,
                "hasMore": page_num == 1,
            }
        )

    result = ExchangeDisclosureClient(retries=1, requester=requester).fetch_sse_announcements(
        "600797",
        start_date=date(2026, 2, 6),
        end_date=date(2026, 8, 5),
    )

    assert announcement_pages == [1, 2]
    assert [record["external_id"] for record in result.records] == ["page-1", "page-2"]


def test_sse_regulatory_does_not_fall_back_to_non_official_sources():
    def requester(method, url, **kwargs):
        return _Response(status_code=403)

    client = ExchangeDisclosureClient(retries=1, requester=requester)

    with pytest.raises(OfficialSourceError, match="query.sse.com.cn"):
        client.fetch_sse_regulatory(
            "600797",
            start_date=date(2026, 2, 6),
            end_date=date(2026, 8, 5),
        )
