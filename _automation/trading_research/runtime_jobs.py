"""Process-owned execution locks and recoverable local worker heartbeats."""
from __future__ import annotations

from contextlib import closing
import functools
import json
import os
import sqlite3
import threading
import time
from pathlib import Path

from filelock import FileLock, Timeout


def initialize_schema(path: Path, migrate_legacy, *, migrations: tuple[tuple[int, str], ...] = ()) -> None:
    """Serialize schema upgrades and run each numbered migration once."""
    with FileLock(str(path.with_suffix('.schema.lock')), timeout=30):
        with closing(sqlite3.connect(path)) as db:
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='workbench_schema_migrations'").fetchone()
            base_applied = bool(
                exists and db.execute('SELECT 1 FROM workbench_schema_migrations WHERE version=1').fetchone()
            )
        if not base_applied:
            migrate_legacy()
            with closing(sqlite3.connect(path)) as db:
                db.executescript('''
                CREATE TABLE IF NOT EXISTS workbench_schema_migrations (
                    version INTEGER PRIMARY KEY, applied_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP);
                CREATE TABLE IF NOT EXISTS morning_targets (
                    review_date TEXT NOT NULL, kol_id INTEGER NOT NULL, platform TEXT NOT NULL,
                    PRIMARY KEY(review_date,kol_id));
                CREATE TABLE IF NOT EXISTS morning_fetch_links (
                    morning_run_id TEXT PRIMARY KEY, fetch_run_id TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS idx_review_queue_scope ON recommendation_drafts(queue_scope,review_date,status,post_id);
                CREATE INDEX IF NOT EXISTS idx_review_queue_utc ON posts(posted_at_utc DESC,post_id DESC);
                CREATE INDEX IF NOT EXISTS idx_review_approved_date ON recommendation_drafts(substr(reviewed_at,1,10),status,post_id);
                INSERT OR IGNORE INTO workbench_schema_migrations(version) VALUES(1);
            ''')
        if migrations:
            with closing(sqlite3.connect(path)) as db:
                for version, statements in sorted(migrations):
                    if db.execute(
                        'SELECT 1 FROM workbench_schema_migrations WHERE version=?',
                        (version,),
                    ).fetchone():
                        continue
                    db.executescript(statements)
                    db.execute(
                        'INSERT INTO workbench_schema_migrations(version) VALUES(?)',
                        (version,),
                    )
                    db.commit()


def worker_paths(database: Path, kind: str) -> tuple[Path, Path]:
    base = database.parent / 'workers'
    return base / f'{kind}.lock', base / f'{kind}.json'


def worker_active(database: Path, kind: str) -> bool:
    lock_path, state_path = worker_paths(database, kind)
    if not lock_path.parent.exists():
        return False
    try:
        with FileLock(str(lock_path), timeout=0):
            # The OS has released the execution lock. An old PID/heartbeat
            # alone must not make an interrupted run look alive.
            return False
    except Timeout:
        return True


def read_workers(database: Path) -> list[dict]:
    items = []
    for kind in ('fetch', 'morning'):
        _, state_path = worker_paths(database, kind)
        try:
            value = json.loads(state_path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        value['active'] = worker_active(database, kind)
        if value.get('status') == 'running' and not value['active']:
            value['status'] = 'interrupted'
        items.append(value)
    return items


def owned_worker(kind: str):
    def decorate(function):
        @functools.wraps(function)
        def run(first, *args, **kwargs):
            if kwargs.get('dry_run'):
                return function(first, *args, **kwargs)
            store = getattr(first, 'post_store', first)
            lock_path, state_path = worker_paths(store.path, kind)
            lock_path.parent.mkdir(parents=True, exist_ok=True)
            lock = FileLock(str(lock_path), timeout=0)
            try:
                lock.acquire()
            except Timeout as exc:
                raise RuntimeError(f'{kind} worker is already running') from exc
            stop = threading.Event()
            value = {'kind': kind, 'pid': os.getpid(), 'started_at': time.time(),
                     'identity': f'{os.getpid()}:{time.monotonic_ns()}', 'status': 'running'}
            def save():
                value['heartbeat_at'] = time.time()
                temporary = state_path.with_suffix('.tmp')
                temporary.write_text(json.dumps(value), encoding='utf-8')
                os.replace(temporary, state_path)
            def heartbeat():
                while not stop.wait(15):
                    try:
                        save()
                    except OSError:
                        pass
            thread = threading.Thread(target=heartbeat, daemon=True, name=f'kol-{kind}-heartbeat')
            try:
                save()
                thread.start()
                result = function(first, *args, **kwargs)
                value['status'] = 'completed'
                return result
            except BaseException:
                value['status'] = 'failed'
                raise
            finally:
                stop.set()
                if thread.is_alive():
                    thread.join(timeout=2)
                try:
                    save()
                finally:
                    lock.release()
        return run
    return decorate
