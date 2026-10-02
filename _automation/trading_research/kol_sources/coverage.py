"""Durable account/surface coverage, including empty and failed captures."""
from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta
from kol_tracker import SHANGHAI, now_iso


def record_surfaces(store, run_id, kol, *, result=None, requested=0, surfaces=(), error='', origin='live_capture', recorded_at=''):
    details = (result.surfaces or {}) if result else {}
    names = tuple(dict.fromkeys((*surfaces, *details))) or ('timeline',)
    timestamp = recorded_at or now_iso()
    with store.connect() as db:
        for name in names:
            detail = details.get(name, {})
            status = str(detail.get('status') or ('success' if result and name == 'timeline' else 'unknown'))
            if error:
                status = 'failed'
            elif status in {'success', 'empty'}:
                status = 'bounded' if detail.get('bounded') or (result and not result.exhausted and name == 'timeline') else 'range_complete' if detail.get('exhausted') or status == 'empty' else 'unknown'
            count = int(detail.get('received_count') or (len(result.posts) if result and name == 'timeline' else 0))
            warnings = [str(x)[:250] for x in detail.get('warnings', [])][:10]
            if error:
                warnings = [error]
            db.execute('INSERT INTO collection_surface_runs '
                '(run_id,kol_id,platform,surface,status,requested_count,received_count,pages,exhausted,'
                'historical_complete,earliest_posted_at,latest_posted_at,warnings_json,origin,recorded_at) '
                'VALUES(?,?,?,?,?,?,?,?,?,0,?,?,?,?,?) ON CONFLICT(run_id,kol_id,surface) DO UPDATE SET '
                'status=excluded.status,warnings_json=excluded.warnings_json',
                (run_id, kol['id'], kol['platform'], name, status, requested, count, int(detail.get('pages') or 0),
                 int(bool(detail.get('exhausted'))), str(detail.get('earliest_posted_at') or ''),
                 str(detail.get('latest_posted_at') or ''), json.dumps(warnings, ensure_ascii=False), origin, timestamp))
            if origin == 'live_capture':
                observed = datetime.fromisoformat(timestamp).astimezone(SHANGHAI)
                day = (observed.date()+timedelta(days=int(observed.hour >= 9))).isoformat()
                key = f"coverage:{kol['id']}:{name}"
                if status in {'bounded','range_complete'}:
                    db.execute("UPDATE research_items SET payload_json=json_set(payload_json,'$.resolved_at',?) "
                        "WHERE kind='coverage_gap' AND theme_id=? AND delivery_date=?",(timestamp,key,day))
                else:
                    payload = {'author_name':kol.get('display_name') or kol['handle'],'url':kol.get('profile_url') or '',
                        'posted_at':'','observed_at':timestamp,'edited':False,'status':status,'surface':name,'run_id':run_id,
                        'evidence':[{'field':'coverage','text':f'{name}：{status}，本次来源面覆盖未完成。'}]}
                    dedupe = hashlib.sha256(f'{day}:{key}:{status}'.encode()).hexdigest()
                    db.execute('INSERT OR IGNORE INTO research_items(dedupe_key,kind,theme_id,theme_name,payload_json,detected_at,delivery_date) '
                        "VALUES(?,'coverage_gap',?,?,?,?,?)",(dedupe,key,(kol.get('display_name') or kol['handle'])+' · '+name,
                        json.dumps(payload,ensure_ascii=False),timestamp,day))


def normalization_gap(store, run_id, kol_id, surface):
    with store.connect() as db:
        db.execute("UPDATE collection_surface_runs SET status='partial',warnings_json=? "
            "WHERE run_id=? AND kol_id=? AND surface=?", ('["normalization_rejected"]', run_id, kol_id, surface))


def surface_coverage(store, *, now=None):
    current = now or datetime.now(SHANGHAI)
    with store.connect() as db:
        rows = db.execute('SELECT r.*,k.handle,k.display_name FROM collection_surface_runs r '
            'JOIN kols k ON k.id=r.kol_id WHERE k.status=\'active\' AND r.id=(SELECT MAX(x.id) '
            'FROM collection_surface_runs x WHERE x.kol_id=r.kol_id AND x.surface=r.surface) ORDER BY r.kol_id,r.surface').fetchall()
        kols = db.execute("SELECT id,platform,handle,display_name FROM kols WHERE status='active'").fetchall()
    lookup = {(row['kol_id'], row['surface']): dict(row) for row in rows}
    values = []
    cutoff = (current - timedelta(hours=36)).isoformat(timespec='seconds')
    for kol in kols:
        for name in ('answers', 'articles', 'ideas') if kol['platform'].casefold() == 'zhihu' else ('timeline',):
            row = lookup.get((kol['id'], name))
            value = row or {**dict(kol), 'kol_id': kol['id'], 'surface': name, 'status': 'never', 'recorded_at': '', 'received_count': 0, 'historical_complete': 0, 'origin': 'unknown'}
            value['warnings'] = json.loads(value.pop('warnings_json', '[]'))
            value['fresh'] = bool(value['recorded_at'] and value['recorded_at'] >= cutoff)
            values.append(value)
    gaps = [v for v in values if v['status'] not in {'bounded', 'range_complete'} or not v['fresh']]
    return {'status': 'degraded' if gaps else 'ready' if values else 'unknown', 'target_surfaces': len(values),
        'fresh_surfaces': sum(v['fresh'] for v in values), 'gap_count': len(gaps),
        'historical_complete': False, 'items': values,
        'note': '按账号和来源面记录；空结果仅表示本次范围结束，不能证明历史完整。'}


def bootstrap_surface_coverage(store):
    """Recover only metadata that still exists. Never invent lost captures."""
    with store.connect() as db:
        rows = db.execute("SELECT s.raw_json,s.fetched_at,p.kol_id,k.* FROM post_sources s JOIN posts p ON p.post_id=s.post_id "
            "JOIN kols k ON k.id=p.kol_id WHERE p.platform='Zhihu' AND s.provider='zhihu-local' "
            "ORDER BY s.fetched_at DESC").fetchall()
    from .core import ProviderFetchResult
    seen = set()
    for row in rows:
        kol_id = row['kol_id']
        if kol_id in seen:
            continue
        data = json.loads(row['raw_json'])
        details = data.get('surfaces') or {}
        if not details:
            continue
        seen.add(kol_id)
        result = ProviderFetchResult('zhihu-local', [], [], [], surfaces=details)
        record_surfaces(store, 'legacy:' + row['fetched_at'], {**dict(row), 'id': kol_id}, result=result,
            surfaces=('answers','articles','ideas'), origin='legacy_snapshot', recorded_at=row['fetched_at'])
    return len(seen)
