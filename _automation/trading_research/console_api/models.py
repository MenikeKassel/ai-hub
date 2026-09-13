from __future__ import annotations
from datetime import date
from typing import Any
from pydantic import BaseModel, Field

class KolCreate(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    handle: str = Field(min_length=1, max_length=100)
    platform: str = Field(default="X", pattern="^(X|Zhihu)$")
    profile_url: str = Field(default="", max_length=500)
    domain: str = Field(default="", max_length=300)
    tracking_mode: str = Field(default="all", max_length=30)


class KolPatch(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    domain: str | None = Field(default=None, max_length=300)
    status: str | None = None
    tracking_mode: str | None = Field(default=None, max_length=30)
    availability_status: str | None = Field(default=None, max_length=30)
    availability_reason: str | None = Field(default=None, max_length=2000)


class KolBackfillRequest(BaseModel):
    count: int = Field(default=200, ge=1)


class DiscoveryRunRequest(BaseModel):
    platform: str = Field(pattern=r"^[a-z][a-z0-9_]{0,31}$")
    query: str = Field(min_length=1, max_length=500)
    limit: int = Field(default=50, ge=1, le=200)


class DiscoveryDecisionRequest(BaseModel):
    note: str = Field(default="", max_length=2000)


class DigestAuthorProfileInput(BaseModel):
    display_name: str = Field(min_length=1, max_length=100)
    profile_url: str = Field(min_length=20, max_length=500)


class DigestAuthorProfileBatch(BaseModel):
    profiles: list[DigestAuthorProfileInput] = Field(min_length=1, max_length=200)


class FetchRequest(BaseModel):
    max_count: int = Field(default=50, ge=1, le=100)
    classify: bool = True
    provider: str = Field(default="auto", pattern="^(auto|twitter|nitter)$")


class TwitterCredentialRequest(BaseModel):
    auth_token: str = Field(min_length=1, max_length=8192)
    ct0: str = Field(min_length=1, max_length=8192)


class XSessionCredentialRequest(TwitterCredentialRequest):
    label: str = Field(default="", max_length=100)


class XSessionStatusRequest(BaseModel):
    status: str = Field(pattern="^(ready|disabled)$")
    reason: str = Field(default="", max_length=2000)


class XCollectionPolicyRequest(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    reason: str = Field(default="", max_length=2000)


class PublicBackupPolicyRequest(BaseModel):
    enabled: bool | None = None
    paused: bool | None = None
    reason: str = Field(default="", max_length=2000)


class DeepSeekCredentialRequest(BaseModel):
    api_key: str = Field(min_length=1, max_length=1024)


class PerformanceRefreshRequest(BaseModel):
    as_of: str | None = None


class FoundationRefreshRequest(BaseModel):
    as_of: str = Field(default="auto", pattern=r"^(auto|\d{4}-\d{2}-\d{2})$")
    notify: bool = True


class Draft(BaseModel):
    symbol: str
    security_name: str = ""
    direction: str
    action: str = Field(default="watch", pattern="^(buy|add|hold|watch|reduce|sell|avoid)$")
    horizon: str = Field(default="unspecified", pattern="^(intraday|short|swing|medium_long|unspecified)$")
    strength: str = Field(default="unspecified", pattern="^(explicit|moderate|weak|unspecified)$")
    thesis: str
    evidence_type: str
    confidence: float = Field(default=0, ge=0, le=1)
    evidence_spans: list[str] = Field(default_factory=list)
    evidence_source: str = "text"
    conditions: list[str] = Field(default_factory=list)
    depends_on_ocr: bool = False
    mention_kind: str = "recommendation"


class ReviewRequest(BaseModel):
    action: str
    note: str = Field(default="", max_length=2000)
    drafts: list[Draft] = Field(default_factory=list)
    confirm_leads: bool = False
    refresh_returns: bool = False


class RecommendationDraftPatch(BaseModel):
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, min_length=1, max_length=100)
    direction: str | None = Field(default=None, pattern="^(long|short)$")
    action: str | None = Field(default=None, pattern="^(buy|add|hold|watch|reduce|sell|avoid)$")
    horizon: str | None = Field(default=None, pattern="^(intraday|short|swing|medium_long|unspecified)$")
    strength: str | None = Field(default=None, pattern="^(explicit|moderate|weak|unspecified)$")
    thesis: str | None = Field(default=None, min_length=1, max_length=4000)
    evidence_type: str | None = Field(default=None, max_length=40)
    evidence_spans: list[str] | None = None
    conditions: list[str] | None = None
    mention_kind: str | None = Field(default=None, max_length=40)
    correction_type: str | None = Field(
        default=None,
        pattern="^(missed_stock|wrong_mapping|wrong_direction|wrong_thesis|wrong_evidence|wrong_content_type)$",
    )
    note: str = Field(default="", max_length=2000)


class RecommendationDraftAction(BaseModel):
    note: str = Field(default="", max_length=2000)


class RecommendationDraftBulkPreviewRequest(BaseModel):
    review_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    queue_scope: str = Field(default="morning", pattern="^(morning|backlog)$")
    status: str = Field(default="ready", pattern="^ready$")
    limit: int = Field(default=200, ge=1, le=200)


class RecommendationDraftBulkApproveRequest(BaseModel):
    snapshot_token: str = Field(min_length=20, max_length=200)
    note: str = Field(default="", max_length=2000)


class ManualRecommendationDraftRequest(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    security_name: str = Field(min_length=1, max_length=100)
    direction: str = Field(default="long", pattern="^(long|short)$")
    action: str = Field(default="watch", pattern="^(buy|add|hold|watch|reduce|sell|avoid)$")
    horizon: str = Field(default="unspecified", pattern="^(intraday|short|swing|medium_long|unspecified)$")
    strength: str = Field(default="explicit", pattern="^(explicit|moderate|weak|unspecified)$")
    thesis: str = Field(min_length=1, max_length=4000)
    evidence_spans: list[str] = Field(min_length=1)
    conditions: list[str] = Field(default_factory=list)
    evidence_source: str = Field(default="text", pattern="^(text|ocr|image)$")
    depends_on_ocr: bool = False
    review_date: str = Field(pattern=r"^\d{4}-\d{2}-\d{2}$")
    correction_type: str = Field(default="missed_stock", pattern="^missed_stock$")
    note: str = Field(min_length=1, max_length=2000)


class StockLeadReviewRequest(BaseModel):
    action: str = Field(pattern="^(confirmed|ignored|pending)$")
    note: str = Field(default="", max_length=2000)
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, max_length=100)


class ReviewAgentSettingsRequest(BaseModel):
    mode: str = Field(pattern="^(shadow|enabled)$")


class EventUpdateRequest(BaseModel):
    action: str = Field(pattern="^(activate|exclude|restore|archive)$")
    kol_name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: str | None = Field(default=None, min_length=1, max_length=30)
    source_url: str | None = Field(default=None, min_length=1, max_length=2000)
    source_note: str | None = Field(default=None, min_length=1, max_length=2000)
    posted_at: str | None = Field(default=None, min_length=1, max_length=80)
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, max_length=100)
    direction: str | None = Field(default=None, pattern="^(long|short)$")
    thesis: str | None = Field(default=None, min_length=1, max_length=4000)
    exclusion_reason: str | None = Field(default=None, max_length=2000)


class EventAmendmentRequest(BaseModel):
    reason: str = Field(min_length=1, max_length=2000)
    kol_name: str | None = Field(default=None, min_length=1, max_length=100)
    platform: str | None = Field(default=None, min_length=1, max_length=30)
    source_url: str | None = Field(default=None, min_length=1, max_length=2000)
    source_note: str | None = Field(default=None, max_length=2000)
    posted_at: str | None = Field(default=None, min_length=1, max_length=80)
    symbol: str | None = Field(default=None, pattern=r"^\d{6}$")
    security_name: str | None = Field(default=None, min_length=1, max_length=100)
    direction: str | None = Field(default=None, pattern="^(long|short)$")
    thesis: str | None = Field(default=None, min_length=1, max_length=4000)


class InstrumentCreate(BaseModel):
    symbol: str = Field(pattern=r"^\d{6}$")
    name: str = Field(min_length=1, max_length=100)
    instrument_type: str = Field(pattern="^(stock|etf|index)$")
    exchange: str = Field(pattern="^(SH|SZ|BJ)$")
    lifecycle: str = Field(default="tracking", pattern="^(pinned|tracking|archived)$")
    source: str = Field(default="manual", max_length=100)


class InstrumentPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=100)
    lifecycle: str | None = Field(default=None, pattern="^(pinned|tracking|archived)$")
    status: str | None = Field(default=None, max_length=30)


class MarketSyncRequest(BaseModel):
    symbols: list[str] = Field(default_factory=list)
    start: date | None = None
    end: date | None = None


class FreeStockDBUpdateRequest(BaseModel):
    dry_run: bool = False
