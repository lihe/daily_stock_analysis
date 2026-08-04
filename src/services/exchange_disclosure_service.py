# -*- coding: utf-8 -*-
"""Official SSE/SZSE metadata adapters for A-share hard events."""

from __future__ import annotations

import logging
import re
import time
from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Callable, Dict, List, Optional
from urllib.parse import urljoin

import requests


logger = logging.getLogger(__name__)

_SSE_PREFIXES = ("600", "601", "603", "605", "688")
_SZSE_PREFIXES = ("000", "001", "002", "003", "300", "301")
_PDF_PATH_PATTERN = re.compile(r"encode-open=['\"]([^'\"]+)['\"]")


class OfficialSourceError(RuntimeError):
    """Raised when an official endpoint cannot provide a complete response."""


@dataclass
class OfficialQueryResult:
    source: str
    category: str
    records: List[Dict[str, Any]]


class ExchangeDisclosureClient:
    """Fetch exchange metadata without relying on search-engine snippets."""

    def __init__(
        self,
        *,
        timeout_seconds: float = 8.0,
        retries: int = 2,
        requester: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.timeout_seconds = max(1.0, float(timeout_seconds))
        self.retries = max(1, int(retries))
        self._requester = requester or requests.request

    @staticmethod
    def exchange_for_code(code: str) -> Optional[str]:
        normalized = str(code or "").strip()
        if len(normalized) != 6 or not normalized.isdigit():
            return None
        if normalized.startswith(_SSE_PREFIXES):
            return "sse"
        if normalized.startswith(_SZSE_PREFIXES):
            return "szse"
        return None

    def fetch_all(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> List[OfficialQueryResult]:
        exchange = self.exchange_for_code(code)
        if exchange == "szse":
            return [
                self.fetch_szse_announcements(code, start_date=start_date, end_date=end_date),
                self.fetch_szse_regulatory_measures(code, start_date=start_date, end_date=end_date),
                self.fetch_szse_disciplinary_actions(code, start_date=start_date, end_date=end_date),
            ]
        if exchange == "sse":
            return [
                self.fetch_sse_announcements(code, start_date=start_date, end_date=end_date),
                self.fetch_sse_regulatory(code, start_date=start_date, end_date=end_date),
            ]
        return []

    def _request_json(self, method: str, url: str, **kwargs: Any) -> Any:
        headers = dict(kwargs.pop("headers", {}) or {})
        headers.setdefault(
            "User-Agent",
            "Mozilla/5.0 (X11; Linux x86_64) official-hard-event-check/1.0",
        )
        headers.setdefault("Accept", "application/json, text/plain, */*")

        last_error: Optional[Exception] = None
        for attempt in range(1, self.retries + 1):
            try:
                response = self._requester(
                    method,
                    url,
                    headers=headers,
                    timeout=self.timeout_seconds,
                    **kwargs,
                )
                response.raise_for_status()
                return response.json()
            except (requests.RequestException, ValueError, TypeError) as exc:
                last_error = exc
                if attempt < self.retries:
                    time.sleep(0.35 * attempt)

        message = f"{method.upper()} {url} failed after {self.retries} attempt(s)"
        if last_error is not None:
            message = f"{message}: {type(last_error).__name__}: {last_error}"
        raise OfficialSourceError(message)

    def fetch_szse_announcements(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> OfficialQueryResult:
        url = "https://www.szse.cn/api/disc/announcement/annList"
        headers = {
            "Referer": f"https://www.szse.cn/disclosure/listed/notice/index.html?stock={code}",
            "Content-Type": "application/json",
        }
        records: List[Dict[str, Any]] = []

        completed = False
        for page_num in range(1, 9):
            payload = {
                "pageSize": 50,
                "pageNum": page_num,
                "stock": [code],
                "channelCode": ["listedNotice_disc"],
            }
            data = self._request_szse_json("POST", url, headers=headers, json=payload)
            rows = data.get("data") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise OfficialSourceError("SZSE announcement response is missing data[]")
            if not rows:
                completed = True
                break

            oldest_date: Optional[date] = None
            for row in rows:
                if not isinstance(row, dict):
                    continue
                publish_date = _parse_date(row.get("publishTime"))
                if publish_date is None:
                    continue
                oldest_date = min(oldest_date, publish_date) if oldest_date else publish_date
                codes = row.get("secCode") or []
                if isinstance(codes, str):
                    codes = [codes]
                if code not in codes or not (start_date <= publish_date <= end_date):
                    continue
                attach_path = str(row.get("attachPath") or "").strip()
                records.append(
                    {
                        "source": "szse_announcement",
                        "category": "announcement",
                        "external_id": str(row.get("annId") or row.get("id") or ""),
                        "stock_code": code,
                        "title": str(row.get("title") or "").strip(),
                        "publish_date": publish_date.isoformat(),
                        "source_url": (
                            urljoin("https://disc.static.szse.cn", attach_path)
                            if attach_path
                            else ""
                        ),
                        "raw_fields": {
                            "ann_id": row.get("annId"),
                            "attach_format": row.get("attachFormat"),
                        },
                    }
                )

            if len(rows) < 50 or (oldest_date is not None and oldest_date < start_date):
                completed = True
                break

        if not completed:
            raise OfficialSourceError("SZSE announcement pagination limit reached before query window completed")

        return OfficialQueryResult("szse_announcement", "announcement", records)

    def fetch_sse_announcements(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> OfficialQueryResult:
        url = "https://query.sse.com.cn/security/stock/queryCompanyBulletin.do"
        headers = {"Referer": "https://www.sse.com.cn/"}
        records: List[Dict[str, Any]] = []

        completed = False
        for page_num in range(1, 6):
            params = {
                "isPagination": "true",
                "productId": code,
                "keyWord": "",
                "securityType": "0101,120100,020100,020200,120200",
                "reportType2": "DQGG",
                "reportType": "ALL",
                "beginDate": start_date.isoformat(),
                "endDate": end_date.isoformat(),
                "pageHelp.pageSize": "100",
                "pageHelp.pageNo": str(page_num),
                "pageHelp.beginPage": str(page_num),
                "pageHelp.cacheSize": "1",
                "pageHelp.endPage": str(page_num + 4),
            }
            data = self._request_json("GET", url, headers=headers, params=params)
            rows = data.get("result") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise OfficialSourceError("SSE announcement response is missing result[]")
            if not rows:
                completed = True
                break

            for row in rows:
                if not isinstance(row, dict) or str(row.get("SECURITY_CODE") or "") != code:
                    continue
                publish_date = _parse_date(row.get("SSEDATE") or row.get("SSEDate"))
                if publish_date is None or not (start_date <= publish_date <= end_date):
                    continue
                path = str(row.get("URL") or "").strip()
                records.append(
                    {
                        "source": "sse_announcement",
                        "category": "announcement",
                        "external_id": path,
                        "stock_code": code,
                        "title": str(row.get("TITLE") or "").strip(),
                        "publish_date": publish_date.isoformat(),
                        "source_url": urljoin("https://www.sse.com.cn", path),
                        "raw_fields": {
                            "bulletin_heading": row.get("BULLETIN_HEADING"),
                            "bulletin_type": row.get("BULLETIN_TYPE"),
                        },
                    }
                )
            if len(rows) < 100:
                completed = True
                break

        if not completed:
            raise OfficialSourceError("SSE announcement pagination limit reached before query window completed")

        return OfficialQueryResult("sse_announcement", "announcement", records)

    def fetch_szse_regulatory_measures(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> OfficialQueryResult:
        data = self._fetch_szse_report(
            catalog_id="1800_jgxxgk",
            referer="https://www.szse.cn/disclosure/supervision/measure/measure/index.html",
            query_params={
                "txtZqdm": code,
                "selectBkmc": "0",
                "txtDate": start_date.isoformat(),
                "txtEnd": end_date.isoformat(),
            },
        )
        records: List[Dict[str, Any]] = []
        for row in data:
            if str(row.get("gkxx_gsdm") or "") != code:
                continue
            event_date = _parse_date(row.get("gkxx_gdrq"))
            if event_date is None or not (start_date <= event_date <= end_date):
                continue
            path = _extract_pdf_path(row.get("hjnr"))
            records.append(
                {
                    "source": "szse_regulatory",
                    "category": "regulation",
                    "external_id": path or f"{code}-{event_date.isoformat()}-{len(records)}",
                    "stock_code": code,
                    "title": str(row.get("gkxx_jgcs") or "监管措施").strip(),
                    "publish_date": event_date.isoformat(),
                    "event_date": event_date.isoformat(),
                    "subtype": str(row.get("gkxx_jgcs") or "监管措施").strip(),
                    "source_url": (
                        urljoin("https://reportdocs.static.szse.cn", path) if path else ""
                    ),
                    "raw_fields": {"involved_party": row.get("gkxx_sjdx")},
                }
            )
        return OfficialQueryResult("szse_regulatory", "regulation", records)

    def fetch_szse_disciplinary_actions(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> OfficialQueryResult:
        data = self._fetch_szse_report(
            catalog_id="1800_jgxxgk_cf",
            referer="https://www.szse.cn/disclosure/supervision/measure/pushish/index.html",
            query_params={
                "txtDMorJC": code,
                "selectGsbk": "0",
                "txtStartDate": start_date.isoformat(),
                "txtEndDate": end_date.isoformat(),
            },
        )
        records: List[Dict[str, Any]] = []
        for row in data:
            if str(row.get("xx_gsdm") or "") != code:
                continue
            event_date = _parse_date(row.get("xx_fwrq"))
            if event_date is None or not (start_date <= event_date <= end_date):
                continue
            path = _extract_pdf_path(row.get("ck"))
            records.append(
                {
                    "source": "szse_disciplinary",
                    "category": "regulation",
                    "external_id": path or f"{code}-{event_date.isoformat()}-{len(records)}",
                    "stock_code": code,
                    "title": str(row.get("xx_bt") or row.get("xx_cflb") or "纪律处分").strip(),
                    "publish_date": event_date.isoformat(),
                    "event_date": event_date.isoformat(),
                    "subtype": str(row.get("xx_cflb") or "纪律处分").strip(),
                    "source_url": (
                        urljoin("https://reportdocs.static.szse.cn", path) if path else ""
                    ),
                    "raw_fields": {},
                }
            )
        return OfficialQueryResult("szse_disciplinary", "regulation", records)

    def _fetch_szse_report(
        self,
        *,
        catalog_id: str,
        referer: str,
        query_params: Dict[str, str],
    ) -> List[Dict[str, Any]]:
        url = "https://www.szse.cn/api/report/ShowReport/data"
        records: List[Dict[str, Any]] = []

        completed = False
        for page_num in range(1, 8):
            params = {
                "SHOWTYPE": "JSON",
                "CATALOGID": catalog_id,
                "PAGENO": str(page_num),
                **query_params,
            }
            payload = self._request_szse_json(
                "GET",
                url,
                headers={"Referer": referer},
                params=params,
            )
            table = payload[0] if isinstance(payload, list) and payload else None
            if not isinstance(table, dict):
                raise OfficialSourceError(f"SZSE report {catalog_id} returned an invalid table")
            rows = table.get("data")
            metadata = table.get("metadata") or {}
            if not isinstance(rows, list):
                raise OfficialSourceError(f"SZSE report {catalog_id} is missing data[]")
            records.extend(row for row in rows if isinstance(row, dict))
            page_count = _safe_int(metadata.get("pagecount"), default=page_num)
            if page_num >= page_count or not rows:
                completed = True
                break
        if not completed:
            raise OfficialSourceError(
                f"SZSE report {catalog_id} pagination limit reached before query completed"
            )
        return records

    def fetch_sse_regulatory(
        self,
        code: str,
        *,
        start_date: date,
        end_date: date,
    ) -> OfficialQueryResult:
        url = "https://query.sse.com.cn/commonSoaQuery.do"
        headers = {"Referer": "https://www.sse.com.cn/regulation/supervision/measures/"}
        records: List[Dict[str, Any]] = []

        completed = False
        for page_num in range(1, 8):
            params = {
                "isPagination": "true",
                "pageHelp.pageSize": "100",
                "pageHelp.pageNo": str(page_num),
                "pageHelp.beginPage": str(page_num),
                "pageHelp.cacheSize": "1",
                "pageHelp.endPage": str(page_num + 4),
                "sqlId": "BS_KCB_GGLL_NEW",
                "siteId": "28",
                "channelId": "10007,10008,10009,10010",
                "stockcode": code,
                "extTeacher": "",
                "extWTFL": "",
                "type": "",
                "createTime": f"{start_date.isoformat()} 00:00:00",
                "createTimeEnd": f"{end_date.isoformat()} 23:59:59",
                "order": "createTime|desc,stockcode|asc",
            }
            data = self._request_json("GET", url, headers=headers, params=params)
            rows = data.get("result") if isinstance(data, dict) else None
            if not isinstance(rows, list):
                raise OfficialSourceError("SSE regulatory response is missing result[]")
            if not rows:
                completed = True
                break

            for row in rows:
                if not isinstance(row, dict):
                    continue
                row_code = str(row.get("extSECURITY_CODE") or row.get("stockcode") or "")
                if row_code != code:
                    continue
                event_date = _parse_date(row.get("createTime"))
                if event_date is None or not (start_date <= event_date <= end_date):
                    continue
                path = str(row.get("docURL") or "").strip()
                if path.startswith("//"):
                    path = "https:" + path
                elif path.startswith("/"):
                    path = urljoin("https://www.sse.com.cn", path)
                elif path and not path.startswith(("http://", "https://")):
                    path = "https://" + path
                subtype = str(row.get("extTYPE") or row.get("extWTFL") or "监管措施").strip()
                records.append(
                    {
                        "source": "sse_regulatory",
                        "category": "regulation",
                        "external_id": str(row.get("docId") or path or ""),
                        "stock_code": code,
                        "title": str(row.get("docTitle") or subtype).strip(),
                        "publish_date": event_date.isoformat(),
                        "event_date": event_date.isoformat(),
                        "subtype": subtype,
                        "source_url": path,
                        "raw_fields": {
                            "channel_id": row.get("channelId"),
                            "involved_party": row.get("extTeacher"),
                        },
                    }
                )
            if len(rows) < 100:
                completed = True
                break

        if not completed:
            raise OfficialSourceError("SSE regulatory pagination limit reached before query completed")

        return OfficialQueryResult("sse_regulatory", "regulation", records)

    def _request_szse_json(self, method: str, url: str, **kwargs: Any) -> Any:
        try:
            return self._request_json(method, url, **kwargs)
        except OfficialSourceError as primary_error:
            backup_url = url.replace("https://www.szse.cn", "https://www.sse.org.cn", 1)
            if backup_url == url:
                raise
            backup_kwargs = dict(kwargs)
            headers = dict(backup_kwargs.get("headers", {}) or {})
            referer = str(headers.get("Referer") or "")
            if referer:
                headers["Referer"] = referer.replace(
                    "https://www.szse.cn",
                    "https://www.sse.org.cn",
                    1,
                )
            backup_kwargs["headers"] = headers
            logger.warning("SZSE 主域名查询失败，尝试官方备用域名: %s", primary_error)
            return self._request_json(method, backup_url, **backup_kwargs)


def _parse_date(value: Any) -> Optional[date]:
    text = str(value or "").strip()
    if len(text) < 10:
        return None
    try:
        return datetime.strptime(text[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _extract_pdf_path(value: Any) -> str:
    match = _PDF_PATH_PATTERN.search(str(value or ""))
    return match.group(1).strip() if match else ""


def _safe_int(value: Any, *, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default
