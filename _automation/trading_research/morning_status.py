"""A coherent morning delivery projection, independent of UI and schedulers."""
from __future__ import annotations

import json
from datetime import datetime

from kol_tracker import SHANGHAI


def _decode(value, default):
    try:
        return json.loads(value or '')
    except (ValueError, TypeError):
        return default


def delivery_status(store, review_date: str, *, now: datetime | None = None) -> dict:
    current = now or datetime.now(SHANGHAI)
    current = current.replace(tzinfo=SHANGHAI) if current.tzinfo is None else current.astimezone(SHANGHAI)
    deadline = datetime.fromisoformat(f'{review_date}T09:00:00+08:00')
    with store.connect() as db:
        db.execute('BEGIN')
        runs = [dict(row) for row in db.execute('SELECT * FROM morning_runs WHERE review_date=? ORDER BY started_at DESC,run_id DESC', (review_date,))]
        targets = db.execute('SELECT * FROM morning_targets WHERE review_date=?', (review_date,)).fetchall()
        attempts = db.execute('''
            SELECT a.*,k.platform FROM fetch_attempts a
            JOIN kols k ON k.id=a.kol_id
            JOIN morning_fetch_links l ON l.fetch_run_id=a.run_id
            JOIN morning_runs r ON r.run_id=l.morning_run_id
            WHERE r.review_date=? AND a.provider NOT LIKE '%shadow%'
            ORDER BY a.created_at,a.id
        ''', (review_date,)).fetchall()
    latest = runs[0] if runs else {}
    final = next((r for r in runs if r['phase']=='final' and r['status']!='running'), None)
    progress = final or latest
    breakdown = {}
    coverage_source = ''
    known = False
    if targets:
        last_attempt = {row['kol_id']: row for row in attempts}
        for target in targets:
            group = breakdown.setdefault(target['platform'].lower(), dict(target=0,success=0,failed=0,pending=0,blocked=0,rate_limited=0,provider_failed=0))
            group['target'] += 1
            attempt = last_attempt.get(target['kol_id'])
            if attempt is None:
                group['pending'] += 1
            elif attempt['status'] in {'success','completed'}:
                group['success'] += 1
            else:
                group['failed'] += 1
                key = 'rate_limited' if 'rate' in attempt['error_code'] else 'blocked' if 'auth' in attempt['error_code'] else 'provider_failed'
                group[key] += 1
        known, coverage_source = True, 'account_ledger'
    else:
        # Legacy phases may contain aggregate totals without platform detail.
        # Keep the newest explicit snapshot for each platform. Older schedulers
        # wrote X and Zhihu in separate phases, so using only one phase drops a
        # platform while mixing phase aggregates creates contradictory totals.
        sources = []
        for run in runs:
            snapshot = _decode(run.get('platform_breakdown_json'), {})
            if not isinstance(snapshot, dict):
                continue
            for platform, values in snapshot.items():
                key = str(platform).lower()
                if key in breakdown or not isinstance(values, dict):
                    continue
                breakdown[key] = values
                sources.append(str(run['run_id']))
        if breakdown:
            known, coverage_source = True, ','.join(dict.fromkeys(sources))
    if known:
        target_count = sum(int(v.get('target',0)) for v in breakdown.values())
        success = sum(int(v.get('success',0)) for v in breakdown.values())
        failed = sum(int(v.get('failed',0)) for v in breakdown.values())
        coverage = success / target_count if target_count else 0.0
    else:
        source = next((r for r in runs if r['successful_kols'] or r['failed_kols']), progress)
        target_count = int(source.get('active_kols',0))
        success, failed = int(source.get('successful_kols',0)), int(source.get('failed_kols',0))
        # A legacy run stores one coherent aggregate snapshot. It is safe to
        # use that snapshot for coverage when it includes a target count;
        # otherwise the denominator is genuinely unknown.
        known = target_count > 0
        coverage_source = source.get('run_id','') if known else ''
        coverage = success / target_count if known else None
    errors = _decode(progress.get('errors_json'), [])
    completed = progress.get('completed_at','') if final else ''
    if final:
        completed_at = datetime.fromisoformat(completed or final['started_at'])
        completed_at = completed_at.replace(tzinfo=SHANGHAI) if completed_at.tzinfo is None else completed_at
        status = 'late' if completed_at > deadline else 'ready' if final['status']=='completed' and not errors and known and failed==0 and success==target_count else 'degraded'
    else:
        status = 'pending' if current <= deadline else 'missed'
    return dict(status=status,deadline=deadline.isoformat(),completed_at=completed,
                coverage=coverage,coverage_known=known,coverage_source=coverage_source,
                active_kols=target_count,successful_kols=success,failed_kols=failed,
                errors=errors,latest_run_id=progress.get('run_id',''),
                latest_phase=progress.get('phase',''),latest_status=progress.get('status','waiting'),
                stage=progress.get('stage','waiting'),progress_current=progress.get('progress_current',0),
                progress_total=progress.get('progress_total',0),platform_breakdown=breakdown)
