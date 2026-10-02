"""Durable local research worker and morning delivery; no market/model dependency."""
from __future__ import annotations

import hashlib
import json
import logging
import importlib.util
import threading
import time
from datetime import datetime, timedelta
from filelock import FileLock, Timeout
from kol_tracker import SHANGHAI
from kol_sources.observations import project_observation, evidence_time
from kol_sources.coverage import surface_coverage
from research_topics import discover_topics
from theme_leads import load_theme_catalog, theme_evidence

logger = logging.getLogger(__name__)


def _encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def delivery_date(timestamp, now):
    try:
        posted = datetime.fromisoformat(timestamp.replace('Z','+00:00')).astimezone(SHANGHAI)
    except (ValueError, AttributeError):
        return ''
    day = posted.date() + timedelta(days=int(posted.hour >= 9))
    return day.isoformat() if day in {now.date(), now.date()+timedelta(days=1)} else ''


class ResearchWorkflow:
    def __init__(self, store, *, now_provider=None):
        self.store = store
        self.now = now_provider or (lambda: datetime.now(SHANGHAI))
        self.stop_event = threading.Event()
        self.thread = None

    def seed(self, catalog):
        with self.store.connect() as db:
            state = db.execute("SELECT value FROM research_state WHERE name='catalog'").fetchone()
            if state and state['value'] == catalog.version:
                return
            # Catalog invalidation is durable, including posts with no existing hit.
            db.execute("INSERT INTO research_jobs(post_id) SELECT post_id FROM posts WHERE 1 "
                "ON CONFLICT(post_id) DO UPDATE SET generation=generation+1,status='queued',next_retry_at='',attempts=0,error=''")
            db.execute("INSERT INTO research_state VALUES('catalog',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value", (catalog.version,))

    def drain(self, *, limit=500, budget_seconds=20):
        try:
            with FileLock(str(self.store.path) + '.research-worker.lock', timeout=0):
                # Keep a connection open across short per-post transactions so
                # SQLite does not checkpoint the WAL after every last close.
                with self.store.connect():
                    return self._drain(limit, budget_seconds)
        except Timeout:
            return {'processed':0,'failed':0,'status':'busy'}

    def _drain(self, limit, budget_seconds):
        catalog = load_theme_catalog(store=self.store)
        self.seed(catalog)
        started = time.monotonic()
        now = self.now().astimezone(SHANGHAI)
        stamp = now.isoformat(timespec='seconds')
        with self.store.connect() as db:
            db.execute("INSERT INTO research_state VALUES('worker_heartbeat',?) ON CONFLICT(name) DO UPDATE SET value=excluded.value",(stamp,))
            jobs = db.execute("SELECT j.* FROM research_jobs j JOIN posts p ON p.post_id=j.post_id "
                "LEFT JOIN post_observation_heads h ON h.post_id=p.post_id LEFT JOIN post_observations o ON o.id=h.observation_id "
                "WHERE next_retry_at='' OR next_retry_at<=? ORDER BY MAX(p.posted_at,coalesce(o.fetched_at,'')) DESC,j.post_id LIMIT ?", (stamp,limit)).fetchall()
        processed = failed = 0
        for offset in range(0,len(jobs),32):
            if time.monotonic()-started >= budget_seconds or self.stop_event.is_set():
                break
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                for job in jobs[offset:offset+32]:
                    if time.monotonic()-started >= budget_seconds or self.stop_event.is_set():
                        break
                    db.execute('SAVEPOINT research_post')
                    try:
                        if self._process_job(db,job,catalog,stamp,now):
                            processed += 1
                    except Exception as exc:
                        db.execute('ROLLBACK TO SAVEPOINT research_post')
                        failed += 1
                        retry = (now+timedelta(seconds=min(3600,30*2**min(job['attempts'],7)))).isoformat(timespec='seconds')
                        db.execute("UPDATE research_jobs SET status='failed',attempts=attempts+1,next_retry_at=?,error=?,updated_at=? "
                            "WHERE post_id=? AND generation=?",(retry,str(exc)[:500],stamp,job['post_id'],job['generation']))
                    finally:
                        db.execute('RELEASE SAVEPOINT research_post')
        return {'processed':processed,'failed':failed,'status':'degraded' if failed else 'ready'}

    def _process_job(self,db,job,catalog,stamp,now):
        current = db.execute('SELECT generation FROM research_jobs WHERE post_id=?', (job['post_id'],)).fetchone()
        if current is None or current['generation'] != job['generation']:
            return False
        row = db.execute("SELECT p.*,coalesce(c.ocr_text,'') ocr_text,c.evidence_type,c.content_type,"
            "coalesce(c.updated_at,'') classification_updated_at,coalesce(o.id,0) observation_id,o.snapshot_json observation_snapshot_json,"
            "o.source_updated_at observation_updated_at,o.fetched_at observation_fetched_at,"
            "EXISTS(SELECT 1 FROM digest_attributions d WHERE d.source_post_id=p.post_id) is_aggregation "
            "FROM posts p LEFT JOIN classifications c ON c.post_id=p.post_id LEFT JOIN post_observation_heads h ON h.post_id=p.post_id "
            "LEFT JOIN post_observations o ON o.id=h.observation_id WHERE p.post_id=?", (job['post_id'],)).fetchone()
        post = project_observation(dict(row))
        old = {r['theme_id']:dict(r) for r in db.execute('SELECT * FROM theme_leads WHERE post_id=?',(job['post_id'],))}
        leads = theme_evidence(post, catalog)
        self.store._replace_theme_leads_in_connection(db, post, leads, catalog.version, stamp)
        changed = row['content_hash'] != post['content_hash']
        source_time = evidence_time(post)
        day = delivery_date(source_time or '', now)
        base = {k:post.get(k,'') for k in ('author_name','handle','url','posted_at','observation_id')}
        base.update({'observed_at':post.get('observation_fetched_at') or post['fetched_at'], 'source_updated_at':post.get('observation_updated_at') or '', 'edited':changed})
        for lead in leads:
            before = old.get(lead['theme_id'])
            withdrawal = any(e.get('context_role') == 'withdrawal' for e in lead['evidence'])
            def meaning(spans):
                return [{k:e.get(k) for k in ('field','text','matched_term','kind','source_role','context_role')} for e in spans]
            different = before and (meaning(json.loads(before['evidence_json'])) != meaning(lead['evidence']) or before['kind'] != lead['kind'])
            kind = 'withdrawal' if withdrawal else 'viewpoint_change' if different else 'theme_evidence'
            if before and not different and db.execute('SELECT 1 FROM research_items WHERE post_id=? AND theme_id=? LIMIT 1',(post['post_id'],lead['theme_id'])).fetchone():
                continue
            payload = {**base,'evidence':lead['evidence'],'evidence_kind':lead['kind'],'source_role':lead['source_role'], 'previous_kind':before['kind'] if before else ''}
            self._item(db, kind, post, lead['theme_id'], lead['theme_name'], payload, stamp, day)
        current_ids = {lead['theme_id'] for lead in leads}
        for theme_id in old.keys()-current_ids:
            before = old[theme_id]
            self._item(db,'evidence_removed',post,theme_id,before['theme_name'], {**base,'evidence':json.loads(before['evidence_json']),'previous_kind':before['kind']},stamp,day)
        db.execute('DELETE FROM theme_candidate_evidence WHERE post_id=?',(post['post_id'],))
        for candidate in discover_topics(post, catalog):
            db.execute('INSERT INTO theme_candidates(term,first_detected_at,last_detected_at) VALUES(?,?,?) '
                'ON CONFLICT(term) DO UPDATE SET last_detected_at=excluded.last_detected_at', (candidate['term'],stamp,stamp))
            record = db.execute('SELECT * FROM theme_candidates WHERE term=?',(candidate['term'],)).fetchone()
            db.execute('INSERT INTO theme_candidate_evidence VALUES(?,?,?,?,?,?) ON CONFLICT(candidate_id,post_id) DO UPDATE SET '
                'observation_id=excluded.observation_id,evidence_json=excluded.evidence_json,kind=excluded.kind,source_role=excluded.source_role',
                (record['id'],post['post_id'],post['observation_id'],_encode(candidate['evidence']),candidate['kind'],candidate['source_role']))
            if record['status'] == 'pending':
                self._item(db,'new_topic',post,'candidate:'+str(record['id']),candidate['term'],
                    {**base,'candidate_id':record['id'],'evidence':candidate['evidence'],'evidence_kind':candidate['kind'],'source_role':candidate['source_role']},stamp,day)
        db.execute('DELETE FROM research_jobs WHERE post_id=? AND generation=?', (post['post_id'],job['generation']))
        return True

    @staticmethod
    def _item(db, kind, post, theme_id, name, payload, stamp, day):
        semantic = _encode([kind,post['post_id'],theme_id,post['content_hash'],payload.get('evidence'),payload.get('source_role')])
        key = hashlib.sha256(semantic.encode()).hexdigest()
        db.execute('INSERT OR IGNORE INTO research_items(dedupe_key,kind,post_id,theme_id,theme_name,payload_json,detected_at,published_at,delivery_date) '
            'VALUES(?,?,?,?,?,?,?,?,?)', (key,kind,post['post_id'],theme_id,name,_encode(payload),stamp,post['posted_at'],day))

    def status(self):
        with self.store.connect() as db:
            row = db.execute("SELECT COUNT(*) pending,SUM(status='failed') failed,MIN(queued_at) oldest_pending_at FROM research_jobs").fetchone()
            heartbeat = db.execute("SELECT value FROM research_state WHERE name='worker_heartbeat'").fetchone()
            failures = db.execute("SELECT post_id,attempts,next_retry_at,error FROM research_jobs WHERE status='failed' LIMIT 10").fetchall()
        return {**dict(row),'proposal_status':'ready' if importlib.util.find_spec('jieba') else 'degraded',
            'last_worker_at':heartbeat['value'] if heartbeat else '', 'errors':[dict(r) for r in failures],
            'failed':int(row['failed'] or 0),'status':'degraded' if row['failed'] else 'indexing' if row['pending'] else 'ready'}

    def digest(self, day, *, limit=100, before_id=0):
        now = self.now().astimezone(SHANGHAI)
        if day not in {now.date().isoformat(),(now.date()+timedelta(days=1)).isoformat()}:
            raise ValueError('research digest supports today and next morning only')
        with self.store.connect() as db:
            rows = db.execute('SELECT * FROM research_items WHERE delivery_date=? AND (?=0 OR id<?) ORDER BY id DESC LIMIT ?', (day,before_id,before_id,limit+1)).fetchall()
            counts = db.execute("SELECT COUNT(*) total,SUM(acknowledged_at='') unread FROM research_items WHERE delivery_date=?",(day,)).fetchone()
        return {'review_date':day,'total':counts['total'],'unread':int(counts['unread'] or 0),
            'next_cursor':rows[limit-1]['id'] if len(rows)>limit else 0,
            'items':[{**{k:r[k] for k in r.keys() if k not in {'payload_json','dedupe_key'}},'payload':json.loads(r['payload_json'])} for r in rows[:limit]],
            'index':self.status(),'coverage':surface_coverage(self.store,now=now)}

    def acknowledge(self, item_id):
        now = self.now().astimezone(SHANGHAI)
        days = (now.date().isoformat(),(now.date()+timedelta(days=1)).isoformat())
        with self.store.connect() as db:
            result = db.execute("UPDATE research_items SET acknowledged_at=? WHERE id=? AND delivery_date IN (?,?)", (now.isoformat(timespec='seconds'),item_id,*days))
            if not result.rowcount:
                raise KeyError(item_id)
        return {'id':item_id,'acknowledged':True}

    def start(self):
        if self.thread and self.thread.is_alive():
            return
        self.stop_event.clear()
        def run():
            while not self.stop_event.is_set():
                try:
                    self.drain()
                except Exception:
                    logger.exception('Research worker failed; durable jobs remain queued')
                self.stop_event.wait(5)
        self.thread = threading.Thread(target=run,name='kol-research-index',daemon=True)
        self.thread.start()

    def stop(self):
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=10)
