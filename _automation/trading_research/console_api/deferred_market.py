"""Keep research startup independent of the local market database."""
from pathlib import Path
import threading
from filelock import FileLock
from market_data import MarketStore


class MarketUnavailableError(RuntimeError):
    pass


class DeferredMarketStore:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.root.mkdir(parents=True,exist_ok=True)
        self.raw_root = self.root / 'raw'
        self.warehouse_root = self.root / 'warehouse'
        self.audit_root = self.root / 'audits'
        self.manifest_root = self.root / 'manifests'
        self.manifest_path = self.manifest_root / 'runs.jsonl'
        self.db_path = self.root / 'market.duckdb'
        self.lock_path = self.root / '.market.lock'
        self._store = None
        self._guard = threading.Lock()

    def lock(self, *, timeout=30):
        self.root.mkdir(parents=True,exist_ok=True)
        return FileLock(str(self.lock_path),timeout=timeout)

    def _get(self):
        if self._store is None:
            with self._guard:
                if self._store is None:
                    try:
                        self._store = MarketStore(self.root, migration_timeout=0.1)
                    except Exception as exc:
                        raise MarketUnavailableError('行情库暂不可用；研究原文和主题索引可继续使用。') from exc
        return self._store

    def instrument_map(self):
        try:
            return self._get().instrument_map()
        except MarketUnavailableError:
            return {}

    def __getattr__(self, name):
        if name.startswith('_'):
            raise AttributeError(name)
        def call(*args, **kwargs):
            return getattr(self._get(),name)(*args,**kwargs)
        return call
