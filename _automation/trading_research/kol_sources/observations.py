"""Immutable source observations; the original approval evidence stays intact."""
from __future__ import annotations

import json
import hashlib
from dataclasses import asdict
from datetime import datetime
from typing import Any

from .core import PROVIDER_PRIORITY, PostRecord
from kol_tracker import SHANGHAI

BODY_FIELDS = ('text', 'article_title', 'article_text', 'quoted_text', 'quoted_author', 'quoted_id', 'post_type')


def source_update_time(raw: dict) -> str:
    for key in ('updatedAtISO', 'updated_at', 'updated'):
        value = raw.get(key)
        if isinstance(value, str):
            try:
                parsed = datetime.fromisoformat(value.replace('Z', '+00:00'))
                if parsed.tzinfo is not None:
                    return parsed.isoformat(timespec='seconds')
            except ValueError:
                pass
    return ''


def evidence_time(post: dict) -> str:
    """A later edit is not evidence at the original publication time."""
    raw = post.get('raw_payload')
    if not isinstance(raw,dict):
        raw = json.loads(post.get('raw_json') or '{}')
    updated = source_update_time(raw)
    changed = bool(post.get('observation_body_changed'))
    date = updated or (post.get('observation_fetched_at') if changed else '') or post['posted_at']
    result = max((date,post['posted_at']),key=lambda value: datetime.fromisoformat(value.replace('Z','+00:00')))
    return datetime.fromisoformat(result.replace('Z','+00:00')).astimezone(SHANGHAI).isoformat(timespec='seconds')


def _snapshot(post: PostRecord | dict) -> dict:
    data = asdict(post) if isinstance(post, PostRecord) else dict(post)
    if 'raw_payload' not in data:
        data['raw_payload'] = json.loads(data.get('raw_json') or '{}')
    return {key: data.get(key) for key in (*BODY_FIELDS, 'post_id', 'platform', 'handle', 'author_name',
        'url', 'posted_at', 'posted_at_utc', 'content_hash', 'canonical_provider', 'fetched_at', 'raw_payload')}


def record_observation(db, post: PostRecord, original: dict | None, timestamp: str) -> int:
    """Append a source version and project a trusted latest body in one transaction."""
    previous = db.execute('SELECT o.* FROM post_observation_heads h JOIN post_observations o '
        'ON o.id=h.observation_id WHERE h.post_id=?', (post.post_id,)).fetchone()
    if previous is None and original is not None:
        baseline = _snapshot(original)
        raw = baseline.get('raw_payload') or {}
        db.execute('INSERT OR IGNORE INTO post_observations '
            '(post_id,provider,content_hash,snapshot_json,source_updated_at,fetched_at,accepted,reason) '
            'VALUES(?,?,?,?,?,?,1,?)', (post.post_id, original['canonical_provider'], original['content_hash'],
            json.dumps(baseline, ensure_ascii=False), source_update_time(raw), original['fetched_at'], 'legacy_original'))
        baseline_id = db.execute('SELECT id FROM post_observations WHERE post_id=? AND provider=? '
            'AND content_hash=? AND source_updated_at=?', (post.post_id, original['canonical_provider'],
            original['content_hash'], source_update_time(raw))).fetchone()[0]
        db.execute('INSERT OR IGNORE INTO post_observation_heads VALUES(?,?)', (post.post_id, baseline_id))
        previous = db.execute('SELECT * FROM post_observations WHERE id=?', (baseline_id,)).fetchone()
    data = _snapshot(post)
    if original is not None:
        body_changed = any(str(data.get(key) or '') != str(original.get(key) or '') for key in BODY_FIELDS)
        # Provider/parser hash revisions are not changes to the original body.
        data['content_hash'] = hashlib.sha256(json.dumps({key:data.get(key) or '' for key in BODY_FIELDS},ensure_ascii=False,sort_keys=True).encode()).hexdigest() if body_changed else original['content_hash']
    observation_hash = data['content_hash']
    raw = data.get('raw_payload') or {}
    updated = source_update_time(raw)
    reason = 'trusted_source'
    accepted = bool(str(post.text or post.article_text).strip())
    if original is not None:
        same_author = post.platform.casefold() == str(original['platform']).casefold() and post.handle.casefold() == str(original['handle']).casefold()
        try:
            source_date = datetime.fromisoformat(post.posted_at_utc.replace('Z', '+00:00'))
            original_date = datetime.fromisoformat(str(original['posted_at_utc']).replace('Z', '+00:00'))
            same_date = abs((source_date-original_date).total_seconds()) <= 60
        except (TypeError, ValueError):
            same_date = False
        minimum_provider = previous['provider'] if previous is not None else original['canonical_provider']
        trusted_provider = PROVIDER_PRIORITY.get(post.canonical_provider, 0) >= PROVIDER_PRIORITY.get(minimum_provider, 0)
        accepted = accepted and same_author and same_date and trusted_provider
        if not accepted:
            reason = 'identity_time_or_provider_conflict'
    if accepted and previous is not None and previous['source_updated_at'] and updated:
        if datetime.fromisoformat(updated) < datetime.fromisoformat(previous['source_updated_at']):
            accepted, reason = False, 'older_source_version'
    if accepted and previous is not None and post.fetched_at < previous['fetched_at']:
        accepted, reason = False, 'older_observation'
    if not str(post.text or post.article_text).strip():
        reason = 'empty_source'
    db.execute('INSERT OR IGNORE INTO post_observations '
        '(post_id,provider,content_hash,snapshot_json,source_updated_at,fetched_at,accepted,reason) '
        'VALUES(?,?,?,?,?,?,?,?)', (post.post_id, post.canonical_provider, observation_hash,
        json.dumps(data, ensure_ascii=False), updated, post.fetched_at, int(accepted), reason))
    row = db.execute('SELECT id,accepted FROM post_observations WHERE post_id=? AND provider=? '
        'AND content_hash=? AND source_updated_at=?', (post.post_id, post.canonical_provider, observation_hash, updated)).fetchone()
    if accepted and row['accepted']:
        # An old already-seen body without edit metadata must not roll back a
        # newer version. A distinct later source edit can restore the body.
        if previous is None or row['id'] >= previous['id']:
            db.execute('INSERT INTO post_observation_heads VALUES(?,?) ON CONFLICT(post_id) '
                'DO UPDATE SET observation_id=excluded.observation_id', (post.post_id, row['id']))
    return int(row['id'])


def project_observation(post: dict[str, Any], snapshot: str | None = None) -> dict[str, Any]:
    """Project only source fields; never replace workflow or approval state."""
    value = dict(post)
    encoded = snapshot or value.pop('observation_snapshot_json', None)
    value.pop('observation_snapshot_json', None)
    if not encoded:
        return value
    observed = json.loads(encoded)
    changed = observed.get('content_hash') != value.get('content_hash')
    value['observation_body_changed'] = changed
    if not changed:
        return value
    for key in (*BODY_FIELDS, 'content_hash', 'fetched_at'):
        if key in observed:
            value[key] = observed[key]
    value['raw_json'] = json.dumps(observed.get('raw_payload') or {}, ensure_ascii=False)
    if changed:
        # Existing model/OCR state belongs to the original evidence version.
        value['evidence_type'] = ''
        value['ocr_text'] = ''
    return value


def list_observations(store, post_id: str) -> list[dict]:
    with store.connect() as db:
        rows = db.execute('SELECT o.*,CASE WHEN h.observation_id=o.id THEN 1 ELSE 0 END is_current '
            'FROM post_observations o LEFT JOIN post_observation_heads h ON h.post_id=o.post_id '
            'WHERE o.post_id=? ORDER BY o.id DESC', (post_id,)).fetchall()
    result = []
    for row in rows:
        data = json.loads(row['snapshot_json'])
        result.append({**{key: row[key] for key in ('id','provider','content_hash','source_updated_at','fetched_at','accepted','reason','is_current')},
            **{key: data.get(key, '') for key in (*BODY_FIELDS, 'author_name','handle','url','posted_at')}})
    return result


def bootstrap_observations(store) -> dict:
    """Recover differing latest snapshots that were retained before versioning."""
    from dataclasses import replace
    from .normalization import normalise_zhihu_post, normalise_twitter_post, normalise_douyin_post
    from kol_tracker import now_iso
    with store.connect() as db:
        rows = db.execute('SELECT p.*,s.raw_json AS source_raw_json,s.fetched_at AS source_fetched_at '
            'FROM posts p JOIN post_sources s ON s.post_id=p.post_id AND s.provider=p.canonical_provider '
            'WHERE s.content_hash<>p.content_hash AND NOT EXISTS(SELECT 1 FROM post_observations o WHERE o.post_id=p.post_id)').fetchall()
    accepted = failed = metadata_only = 0
    for row in rows:
        try:
            kol = store.get_kol(row['kol_id'])
            normalise = {'zhihu':normalise_zhihu_post,'douyin':normalise_douyin_post}.get(row['platform'].casefold(),normalise_twitter_post)
            post = normalise(json.loads(row['source_raw_json']),kol,provider=row['canonical_provider'])
            post = replace(post,fetched_at=row['source_fetched_at'])
            if not any(str(getattr(post,key) or '') != str(row[key] or '') for key in BODY_FIELDS):
                metadata_only += 1
                continue
            with store.connect() as db:
                record_observation(db,post,dict(row),now_iso())
                head = db.execute('SELECT o.content_hash FROM post_observation_heads h JOIN post_observations o ON o.id=h.observation_id WHERE h.post_id=?',(post.post_id,)).fetchone()
                accepted += int(head is not None and head['content_hash'] != row['content_hash'])
        except (ValueError,TypeError,KeyError):
            failed += 1
    return {'examined':len(rows),'accepted_changes':accepted,'metadata_only':metadata_only,'unrecoverable':failed,
        'note':'仅恢复仍存在的最近源快照；被历史覆盖的中间版本无法重建。'}
