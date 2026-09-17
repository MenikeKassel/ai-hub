"""Compatibility exports; implementations live in kol_sources by responsibility."""
import csv
import hashlib
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import subprocess
import tempfile
import time
import urllib.request
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from typing import Any, Callable, Protocol
import httpx
from filelock import FileLock, Timeout as FileLockTimeout
from kol_tracker import SHANGHAI, now_iso
from runtime_jobs import initialize_schema, owned_worker, worker_active
from model_budget import ModelDailyBudget, OcrDailyBudget
from opencode_go import OPENCODE_GO_API_URL, OPENCODE_GO_MODEL, load_opencode_go_api_key
from kol_sources.core import (
    CredentialStorageError,
    CredentialValidationError,
    FINANCE_WORDS,
    FetchSummary,
    LONG_WORDS,
    ModelProviderUnavailableError,
    ModelWorkerBusyError,
    NUMBERED_STOCK_LINE,
    PROSPECTIVE_PLAN_MARKERS,
    PROVIDER_PRIORITY,
    PostRecord,
    ProviderAttempt,
    ProviderFetchResult,
    REALIZED_POSITION_MARKERS,
    RECOMMENDATION_ACTIONS,
    RECOMMENDATION_HORIZONS,
    RECOMMENDATION_STRENGTHS,
    RETROSPECTIVE_CLAIM,
    RETROSPECTIVE_WORDS,
    RuleResult,
    SEED_KOLS,
    SHORT_WORDS,
    STRUCTURED_RECOMMENDATION_HEADER,
    STRUCTURED_REVIEW_VERSION,
    TwitterAuthenticationError,
    TwitterProviderError,
    TwitterRateLimitError,
    XBudgetDeferredError,
    XPostProvider,
    XSessionUnavailableError,
    ZhihuProviderError,
    _json,
    _loads,
    _normalise_attributed_name,
    _raise_codex_failure,
    _utc_and_local,
    _zhihu_profile_handle,
    validate_twitter_credentials,
)
from kol_sources.normalization import (
    ZHIHU_DIGEST_AUTHOR_RE,
    extract_zhihu_digest_attributions,
    normalise_douyin_post,
    normalise_twitter_post,
    normalise_zhihu_answer,
)
from kol_sources.rules import (
    RuleClassifier,
    validate_event_draft,
    validate_model_payload,
)
from kol_sources.repository import (
    KolPostStore,
    initialize_seed_kols,
    load_stock_aliases,
)
from kol_sources.credentials import (
    DeepSeekCredentialStore,
    KeyringCredentialStore,
    NitterCredentialStore,
    OpenCodeGoCredentialStore,
    ReaderCredentialStore,
)
from kol_sources.sessions import (
    PublicBackupDeferredError,
    PublicBackupGate,
    PublicBackupNotFoundError,
    PublicBackupRateLimitError,
    XSessionManager,
)
from kol_sources.providers import (
    FallbackXPostProvider,
    FxTwitterPublicPostProvider,
    TwitterCliProvider,
    XtfNitterProvider,
    ZhihuProfileProvider,
    _parse_nitter_timestamp,
    _post_provider_warning,
    _provider_result,
    _resolve_twitter_command,
    _snowflake_timestamp,
    _xtf_tweet_payload,
    build_x_post_provider,
)
from kol_sources.media import (
    download_images,
    media_disk_usage,
)
from kol_sources.classifiers import (
    CodexBatchPostClassifier,
    CodexPostClassifier,
    DeepSeekBatchPostClassifier,
    DeepSeekPostClassifier,
    FallbackBatchPostClassifier,
    FallbackPostClassifier,
    OcrBatchClassifier,
    RapidOcrBatchClassifier,
    UnlimitedOcrBatchClassifier,
    _DeepSeekClassifierBase,
    _FallbackClassifierBase,
    _codex_command_prefix,
    _deepseek_review_instruction,
    _deepseek_source_item,
    _is_transient_model_error,
    _parse_json_object_content,
    _resolve_codex_command,
    _run_command_with_tree_timeout,
    build_batch_post_classifier,
    build_post_classifier,
    process_pending_ocr_with_budget,
    process_pending_with_ocr,
)
from kol_sources.processing import (
    classify_pending_in_batches,
    classify_pending_with_codex,
)
from kol_sources.collection import (
    _fetch_with_cursor_search,
    _fetch_with_retry,
    _payload_post_ids,
    run_post_fetch,
)
