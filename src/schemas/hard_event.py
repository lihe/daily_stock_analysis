# -*- coding: utf-8 -*-
"""Structured evidence for exchange-verified hard events."""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Dict, List, Optional


@dataclass(frozen=True)
class OfficialHardEvent:
    """One hard event confirmed by exchange metadata."""

    event_id: str
    stock_code: str
    event_type: str
    subtype: str
    title: str
    publish_date: str
    source: str
    source_url: str
    event_date: Optional[str] = None
    report_period: Optional[str] = None
    raw_fields: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class OfficialSourceStatus:
    """Completeness status of one official-source query."""

    source: str
    category: str
    status: str
    record_count: int = 0
    error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass
class OfficialHardEventEvidence:
    """Complete hard-event evidence collected for one stock."""

    stock_code: str
    stock_name: str
    query_start: str
    query_end: str
    checked_at: str
    status: str
    events: List[OfficialHardEvent] = field(default_factory=list)
    sources: List[OfficialSourceStatus] = field(default_factory=list)
    notes: List[str] = field(default_factory=list)

    @property
    def coverage_complete(self) -> bool:
        return self.status in {"VERIFIED", "CLEAN", "NOT_APPLICABLE"}

    def to_dict(self) -> Dict[str, Any]:
        return {
            "stock_code": self.stock_code,
            "stock_name": self.stock_name,
            "query_start": self.query_start,
            "query_end": self.query_end,
            "checked_at": self.checked_at,
            "status": self.status,
            "coverage_complete": self.coverage_complete,
            "events": [event.to_dict() for event in self.events],
            "sources": [source.to_dict() for source in self.sources],
            "notes": list(self.notes),
        }
