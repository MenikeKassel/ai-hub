from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Protocol

import httpx

from kol_posts import (
    DeepSeekCredentialStore,
    ModelProviderUnavailableError,
    _parse_json_object_content,
)
from opencode_go import OPENCODE_GO_API_URL, OPENCODE_GO_MODEL


LENS_KEYS = {
    "short_term_leader",
    "dow_wave_gann",
    "price_action",
    "ict",
    "wyckoff_orderflow",
}
AI_PROMPT_VERSION = "event-method-research-ai-v5-opencode-go"
FUTURE_LEAKAGE_RE = re.compile(
    r"(?:后续|随后|后来|事后|最终).{0,12}(?:收益|上涨|下跌|涨幅|跌幅)"
    r"|(?:发帖后|推荐后|此后|之后).{0,8}(?:\d+|一|二|三|四|五|六|七|八|九|"
    r"十|两)?(?:天|周|个月|月)?.{0,8}(?:收益|上涨|下跌|涨幅|跌幅)"
    r"|(?:一周|一个月|三个月|六个月|\d+(?:天|周|月)).{0,8}"
    r"(?:收益|上涨|下跌|涨幅|跌幅)"
    r"|(?:1W|1M|3M|6M|MFE|MAE|forward\s+return|subsequent\s+return)",
    re.IGNORECASE,
)
REALIZED_FUTURE_OUTCOME_RE = re.compile(
    r"(?:(?:发帖|推荐).{0,4}后|此后|后来|随后|事后|最终|之后).{0,16}"
    r"(?:收益|涨幅|跌幅|上涨|下跌)"
    r"|(?:一周|一个月|三个月|六个月|\d+(?:天|周|月))后.{0,8}"
    r"(?:收益|涨幅|跌幅|上涨|下跌)"
    r"|(?:1W|1M|3M|6M|MFE|MAE|forward\s+return|subsequent\s+return)",
    re.IGNORECASE,
)
TRADING_DIRECTIVE_RE = re.compile(
    r"(?:建议|应当|应该|可以|适合).{0,8}(?:买入|卖出|加仓|减仓|建仓|满仓|"
    r"重仓|轻仓|空仓)"
    r"|(?:不宜|避免|谨慎|优先|优选|继续|暂时|先|需|需要|建议|应|应当|应该|"
    r"可以|适合).{0,8}(?:追高|抄底|观望|等待|关注|持有|买入|卖出|加仓|减仓|"
    r"建仓|布局|介入)"
    r"|(?:^|[，。；：、\s])(?:买入|卖出|加仓|减仓|建仓|布局|介入|持有|"
    r"观望|追高|抄底)(?:$|[，。；！、\s])"
    r"|(?:止损价|目标价|仓位建议|buy\s+signal|sell\s+signal)",
    re.IGNORECASE,
)
PROXY_OVERCLAIM_RE = re.compile(
    r"(?:主力|大资金|资金).{0,6}(?:介入|流入|流出|吸筹|出货)"
    r"|(?:买盘|卖盘).{0,4}(?:主导|强于)",
    re.IGNORECASE,
)
PREDICTIVE_SIGNAL_RE = re.compile(
    r"(?:价格|股价|标的).{0,4}(?:将|会|必然|预计|预期).{0,8}"
    r"(?:上涨|下跌|涨停|跌停|走强|走弱)"
    r"|(?:买入|卖出|看涨|看跌).{0,4}(?:信号|机会)",
    re.IGNORECASE,
)


class EventResearchAIProvider(Protocol):
    model_name: str
    prompt_version: str

    def interpret_many(
        self,
        items: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]: ...


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _resolve_evidence_path(research: dict[str, Any], path: str) -> Any:
    if not path.startswith("lenses."):
        raise ValueError(f"evidence ref must start with lenses.: {path}")
    value: Any = research
    for token in path.split("."):
        if not isinstance(value, dict) or token not in value:
            raise ValueError(f"evidence ref does not exist: {path}")
        value = value[token]
    if value is None or value == "" or value == [] or value == {}:
        raise ValueError(f"evidence ref is empty: {path}")
    return value


def _normalize_evidence_path(path: Any) -> str:
    value = str(path).strip()
    if value.startswith("research."):
        value = value[len("research.") :]
    return value


def _evidence_catalog(value: Any, prefix: str) -> list[str]:
    if value is None or value == "" or value == [] or value == {}:
        return []
    paths = [prefix]
    if isinstance(value, dict):
        for key in sorted(value):
            paths.extend(_evidence_catalog(value[key], f"{prefix}.{key}"))
    return paths


def _lens_evidence_catalog(research: dict[str, Any]) -> dict[str, list[str]]:
    lenses = research.get("lenses")
    if not isinstance(lenses, dict):
        return {}
    return {
        str(lens): _evidence_catalog(value, f"lenses.{lens}")
        for lens, value in lenses.items()
        if lens in LENS_KEYS
    }


def _canonical_evidence_path(
    research: dict[str, Any],
    evidence_catalog: dict[str, list[str]],
    *,
    lens: str,
    path: Any,
) -> str:
    value = _normalize_evidence_path(path)
    if not value.startswith(f"lenses.{lens}."):
        raise ValueError(
            f"cross-lens evidence is not allowed for {lens}: {value}"
        )
    if value in evidence_catalog.get(lens, []):
        _resolve_evidence_path(research, value)
        return value
    if value.endswith(".candidate"):
        status_path = value.removesuffix(".candidate") + ".status"
        if status_path in evidence_catalog.get(lens, []):
            _resolve_evidence_path(research, status_path)
            return status_path
    leaf = value.rsplit(".", 1)[-1]
    candidates = [
        candidate
        for candidate in evidence_catalog.get(lens, [])
        if candidate.endswith(f".{leaf}")
    ]
    if len(candidates) == 1:
        _resolve_evidence_path(research, candidates[0])
        return candidates[0]
    _resolve_evidence_path(research, value)
    raise ValueError(
        f"evidence ref is not in the supplied non-empty catalog: {value}"
    )


def _validate_interpretation_text(
    value: str,
    lens: str,
    *,
    allow_conditional_future: bool = False,
) -> None:
    if REALIZED_FUTURE_OUTCOME_RE.search(value):
        raise ValueError(f"future outcome leakage in {lens}")
    if not allow_conditional_future and FUTURE_LEAKAGE_RE.search(value):
        raise ValueError(f"future outcome leakage in {lens}")
    if TRADING_DIRECTIVE_RE.search(value):
        raise ValueError(f"trading directive is not allowed in {lens}")
    if PROXY_OVERCLAIM_RE.search(value):
        raise ValueError(f"bar-data proxy overclaim is not allowed in {lens}")
    if PREDICTIVE_SIGNAL_RE.search(value):
        raise ValueError(f"predictive trading signal is not allowed in {lens}")


def validate_interpretation_payload(
    payload: dict[str, Any],
    research: dict[str, Any],
) -> dict[str, Any]:
    interpretations = payload.get("interpretations")
    if not isinstance(interpretations, list):
        raise ValueError("interpretations must be a list")
    by_lens: dict[str, dict[str, Any]] = {}
    evidence_catalog = _lens_evidence_catalog(research)
    for item in interpretations:
        if not isinstance(item, dict):
            raise ValueError("interpretation must be an object")
        lens = str(item.get("lens") or "")
        if lens not in LENS_KEYS or lens in by_lens:
            raise ValueError(f"unexpected or duplicate lens: {lens}")
        confidence = float(item.get("confidence"))
        if not 0 <= confidence <= 1:
            raise ValueError(f"confidence outside [0,1] for {lens}")
        hypothesis = str(item.get("hypothesis") or "").strip()
        if not hypothesis:
            raise ValueError(f"hypothesis is empty for {lens}")
        evidence_refs = item.get("evidence_refs")
        if not isinstance(evidence_refs, list) or not evidence_refs:
            raise ValueError(f"evidence_refs are required for {lens}")
        normalized_evidence_refs = [
            _canonical_evidence_path(
                research,
                evidence_catalog,
                lens=lens,
                path=path,
            )
            for path in evidence_refs
        ]
        counter = item.get("counter_evidence")
        invalidation = item.get("invalidation")
        if not isinstance(counter, list) or not isinstance(invalidation, list):
            raise ValueError(f"counter_evidence and invalidation must be arrays for {lens}")
        for text in [hypothesis, *(str(value) for value in counter)]:
            _validate_interpretation_text(text, lens)
        for text in (str(value) for value in invalidation):
            _validate_interpretation_text(
                text,
                lens,
                allow_conditional_future=True,
            )
        by_lens[lens] = {
            "lens": lens,
            "hypothesis": hypothesis,
            "confidence": confidence,
            "evidence_refs": normalized_evidence_refs,
            "counter_evidence": [str(value) for value in counter],
            "invalidation": [str(value) for value in invalidation],
        }
    missing = LENS_KEYS - set(by_lens)
    if missing:
        raise ValueError("interpretations omitted lenses: " + ", ".join(sorted(missing)))
    return {"interpretations": [by_lens[key] for key in sorted(by_lens)]}


def interpretation_input_hash(
    event: dict[str, Any],
    research_snapshot_id: str,
    *,
    prompt_version: str = AI_PROMPT_VERSION,
) -> str:
    payload = {
        "event_id": event.get("event_id"),
        "symbol": event.get("symbol"),
        "security_name": event.get("security_name"),
        "posted_at": event.get("posted_at"),
        "direction": event.get("direction"),
        "thesis": event.get("thesis"),
        "research_snapshot_id": research_snapshot_id,
        "prompt_version": prompt_version,
    }
    return hashlib.sha256(_json(payload).encode("utf-8")).hexdigest()


def interpretation_id(
    *,
    event_id: str,
    research_snapshot_id: str,
    provider: str,
    prompt_version: str,
    input_hash: str,
    payload: dict[str, Any],
) -> str:
    value = {
        "event_id": event_id,
        "research_snapshot_id": research_snapshot_id,
        "provider": provider,
        "prompt_version": prompt_version,
        "input_hash": input_hash,
        "payload": payload,
    }
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _prompt_items(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    values = []
    for item in items:
        research = item["research"]
        values.append(
            {
                "event": {
                    "event_id": item["event"]["event_id"],
                    "symbol": item["event"]["symbol"],
                    "security_name": item["event"].get("security_name") or "",
                    "posted_at": item["event"]["posted_at"],
                    "direction": item["event"].get("direction") or "",
                    "thesis": item["event"].get("thesis") or "",
                },
                "research": {
                    "version": research.get("version"),
                    "as_of_trade_date": research.get("as_of_trade_date"),
                    "lenses": research.get("lenses", {}),
                    "evidence_catalog": _lens_evidence_catalog(research),
                    "data_lineage": research.get("data_lineage", {}),
                    "warnings": research.get("warnings", []),
                },
            }
        )
    return values


def _instruction(schema: str) -> str:
    return (
        "You interpret point-in-time market evidence for a KOL recommendation audit. "
        "This is research documentation, not investment advice. Return exactly one result "
        "for every event_id and follow the JSON Schema. Write concise Chinese hypotheses. "
        "Never infer from future returns, targets, positions, or data absent from the supplied "
        "research snapshot. Each evidence_refs item must be an exact dotted path beginning "
        "with lenses.<current_lens>. (for example, lenses.price_action.conclusion), without "
        "a research. prefix, and must appear verbatim in that lens's evidence_catalog. "
        "Do not cite empty or missing data. Do not use "
        "one lens as evidence for another. Treat wave counts, Gann, "
        "Wyckoff phases and ICT structures as hypotheses, not facts. ICT price-liquidity "
        "proxies are not true order flow. Do not fabricate CVD or option walls. "
        "latest_three_bar_gap belongs only to the ICT lens, not price_action. "
        "For an undetermined candidate cite its non-empty status field, never a null "
        "candidate field. "
        "Preserve disagreement between lenses and do not create an aggregate score, "
        "ranking, buy/sell instruction, stop loss or position recommendation. Use "
        "descriptive research language only: never tell the reader to watch, wait, avoid "
        "chasing, hold, enter, exit or otherwise take or defer a trading action. Bar data "
        "does not identify participant identity or aggressor side: never claim main-force "
        "or large-capital inflow/outflow, capital entry, or buyer/seller dominance. Do not "
        "state a standalone action such as buy, sell, hold or wait; do not predict that price "
        "will rise or fall; and do not label any observation as a bullish, bearish, buy or "
        "sell signal.\n\n"
        "JSON Schema:\n"
        + schema
    )


class DeepSeekEventResearchInterpreter:
    model_name = OPENCODE_GO_MODEL
    provider_name = "opencode-go"
    prompt_version = AI_PROMPT_VERSION
    api_url = OPENCODE_GO_API_URL

    def __init__(
        self,
        schema_path: Path,
        credentials: DeepSeekCredentialStore | None = None,
        *,
        timeout_seconds: float = 240,
        client: httpx.Client | None = None,
    ):
        self.schema_path = Path(schema_path)
        self.credentials = credentials or DeepSeekCredentialStore()
        self.timeout_seconds = max(1, timeout_seconds)
        self.client = client

    def interpret_many(
        self,
        items: list[dict[str, Any]],
    ) -> dict[str, dict[str, Any]]:
        if not 1 <= len(items) <= 5:
            raise ValueError("event research batches must contain between 1 and 5 events")
        schema = self.schema_path.read_text(encoding="utf-8")
        messages = [
            {"role": "system", "content": _instruction(schema)},
            {
                "role": "user",
                "content": "Return JSON only.\n"
                + _json({"events": _prompt_items(items)}),
            },
        ]
        headers = {"Authorization": f"Bearer {self.credentials.load()}"}
        for attempt in range(2):
            body = {
                "model": self.model_name,
                "messages": messages,
                "response_format": {"type": "json_object"},
                "thinking": {"type": "disabled"},
                "temperature": 0.2,
                "stream": False,
            }
            try:
                if self.client is None:
                    response = httpx.post(
                        self.api_url,
                        headers=headers,
                        json=body,
                        timeout=self.timeout_seconds,
                    )
                else:
                    response = self.client.post(
                        self.api_url,
                        headers=headers,
                        json=body,
                        timeout=self.timeout_seconds,
                    )
            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                raise ModelProviderUnavailableError(
                    f"OpenCode Go connection failed: {exc.__class__.__name__}"
                ) from exc
            if response.status_code in {401, 402, 403, 408, 409, 429} or response.status_code >= 500:
                raise ModelProviderUnavailableError(
                    f"OpenCode Go API unavailable (HTTP {response.status_code})"
                )
            if response.status_code >= 400:
                raise RuntimeError(
                    f"OpenCode Go request rejected (HTTP {response.status_code})"
                )
            try:
                content = response.json()["choices"][0]["message"]["content"]
                raw = _parse_json_object_content(content)
            except (KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError) as exc:
                if attempt == 0:
                    messages.append(
                        {
                            "role": "user",
                            "content": (
                                "The previous response was not valid JSON. "
                                "Return one corrected JSON object only."
                            ),
                        }
                    )
                    continue
                raise RuntimeError(
                    "OpenCode Go returned invalid event research JSON"
                ) from exc
            try:
                return _split_and_validate(raw, items)
            except ValueError as exc:
                if attempt == 1:
                    raise
                messages.append(
                    {
                        "role": "user",
                        "content": (
                            "The previous response was rejected by the deterministic "
                            f"validator: {str(exc)[:500]}. Return corrected JSON only; "
                            "do not relax or work around the stated policy."
                        ),
                    }
                )
        raise RuntimeError("OpenCode Go event research retry exhausted")


def _split_and_validate(
    raw: dict[str, Any],
    items: list[dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    requested = {str(item["event"]["event_id"]): item for item in items}
    results: dict[str, dict[str, Any]] = {}
    values = raw.get("results")
    if not isinstance(values, list):
        raise ValueError("event research result must contain results")
    for value in values:
        if not isinstance(value, dict):
            raise ValueError("event research result item must be an object")
        event_id = str(value.get("event_id") or "")
        if event_id not in requested or event_id in results:
            raise ValueError(f"unexpected or duplicate event_id: {event_id}")
        results[event_id] = validate_interpretation_payload(
            {"interpretations": value.get("interpretations")},
            requested[event_id]["research"],
        )
    missing = set(requested) - set(results)
    if missing:
        raise ValueError(
            "event research result omitted events: " + ", ".join(sorted(missing))
        )
    return results


def build_event_research_interpreter(
    schema_path: Path,
    workspace: Path,
    *,
    deepseek_credentials: DeepSeekCredentialStore | None = None,
) -> DeepSeekEventResearchInterpreter:
    del workspace
    return DeepSeekEventResearchInterpreter(
        schema_path,
        deepseek_credentials,
    )
