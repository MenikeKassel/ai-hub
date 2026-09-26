from __future__ import annotations

import json
import re
import unittest
from datetime import datetime
from pathlib import Path


ROOT = Path(__file__).resolve().parents[3]
SCRIPTS = ROOT / "scripts"


class KolTaskContractTests(unittest.TestCase):
    def test_collection_tasks_have_one_owner_and_safe_order(self) -> None:
        contract = json.loads((SCRIPTS / "kol-task-contract.json").read_text(encoding="utf-8"))
        tasks = {item["name"]: item for item in contract["tasks"]}
        self.assertEqual(len(contract["tasks"]), len(tasks))
        self.assertEqual("install-kol-recovery-tasks.ps1", contract["owner"])
        self.assertEqual(
            {"global_limit_24h": 180, "session_limit_24h": 90, "min_interval_seconds": 60},
            contract["x_policy_reference"],
        )
        evening = datetime.strptime(tasks["KOL_Zhihu_Fetch_Evening"]["at"], "%H:%M")
        market = datetime.strptime(contract["market_publication_at"], "%H:%M")
        self.assertGreaterEqual((market - evening).total_seconds(), 3600)
        self.assertIn("-NoFetch", tasks["KOL_Morning_Pipeline_0845"]["arguments"])

        recovery = (SCRIPTS / "install-kol-recovery-tasks.ps1").read_text(encoding="utf-8")
        research = (SCRIPTS / "install-research-data-tasks.ps1").read_text(encoding="utf-8")
        legacy = (SCRIPTS / "install-kol-post-fetch.ps1").read_text(encoding="utf-8")
        doctor = (SCRIPTS / "kol-task-doctor.ps1").read_text(encoding="utf-8")
        self.assertIn("$taskContract.tasks", recovery)
        self.assertIn("$taskContract.market_publication_at", research)
        self.assertIn("-CollectionOnly", legacy)
        self.assertIn("$contract.tasks", doctor)
        self.assertIn("x_policy_reference_drift", doctor)
        self.assertIn("/api/system/x-sessions", doctor)
        for name in tasks:
            self.assertIsNone(re.search(rf'Register-ResearchTask\s+"{re.escape(name)}"', research))
        self.assertNotIn("Register-ScheduledTask -TaskName $TaskName", legacy)
        self.assertNotIn("Unregister-ScheduledTask", research)
        for name in ("Market_Data_Sync_Daily", "FreeStockDB_Update_Daily", "KOL_Return_Tracker_Daily"):
            self.assertNotIn(f'"{name}"', recovery)


if __name__ == "__main__":
    unittest.main()
