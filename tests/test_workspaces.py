import tempfile
import time
import unittest
from pathlib import Path

from cc_remote.workspaces import WorkRegistry, WorkStores


class WorkRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / "provider" / "work"
        self.store = WorkRegistry(self.root, "claude")

    def tearDown(self):
        self.tmp.cleanup()

    def test_project_sources_plugins_are_materialized_into_private_session(self):
        project_id = self.store.create_project("季度复盘", "整理业务结果")
        self.store.add_source(
            project_id, "file", "原始数据", filename="report.csv",
            content=b"name,value\nA,1\n",
        )
        self.store.add_source(
            project_id, "link", "说明文档", uri="https://example.com/spec",
        )
        self.store.create_plugin("输出规范", "结论先行并附数据来源", project_id)

        record = self.store.create_session(project_id)
        workspace = Path(record.cwd)
        context = (workspace / "WORK.md").read_text(encoding="utf-8")
        self.assertIn("季度复盘", context)
        self.assertIn("资料库/report.csv", context)
        self.assertIn("https://example.com/spec", context)
        self.assertIn("结论先行", context)
        self.assertEqual((workspace / "资料库" / "report.csv").read_bytes(),
                         b"name,value\nA,1\n")
        self.assertEqual(workspace.stat().st_mode & 0o777, 0o700)
        self.assertEqual((workspace / "WORK.md").stat().st_mode & 0o777, 0o600)

    def test_dashboard_never_exposes_provider_storage_paths(self):
        project_id = self.store.create_project("知识库")
        self.store.add_source(
            project_id, "file", "secret", filename="secret.txt",
            content=b"safe copy only",
        )
        dashboard = self.store.dashboard()
        self.assertEqual(len(dashboard["sources"]), 1)
        self.assertNotIn("stored_path", dashboard["sources"][0])
        self.assertNotIn(str(self.root), repr(dashboard))

    def test_schedule_claim_is_atomic_and_records_result(self):
        schedule_id = self.store.create_schedule(
            "日报", "生成日报", time.time() - 1, repeat_seconds=3600)
        first = self.store.claim_due_schedules(time.time())
        second = self.store.claim_due_schedules(time.time())
        self.assertEqual([row["schedule_id"] for row in first], [schedule_id])
        self.assertEqual(second, [])
        self.store.complete_schedule(schedule_id, "session-1", None)
        schedule = self.store.dashboard()["schedules"][0]
        self.assertEqual(schedule["last_session_id"], "session-1")
        self.assertTrue(schedule["enabled"])

    def test_delete_session_removes_only_registry_owned_random_directory(self):
        record = self.store.create_session()
        self.store.bind_session(record.work_id, "session-1")
        outside = Path(self.tmp.name) / "keep.txt"
        outside.write_text("keep", encoding="utf-8")
        self.store.delete("session-1")
        self.assertFalse(Path(record.cwd).parent.exists())
        self.assertEqual(outside.read_text(encoding="utf-8"), "keep")

    def test_engine_stores_are_strictly_separate(self):
        stores = WorkStores(self.root / "claude", self.root / "codex")
        claude = stores.for_engine("claude").create_session()
        stores.for_engine("claude").bind_session(claude.work_id, "same-id")
        self.assertEqual(stores.classify("claude", "same-id", claude.cwd), "work")
        self.assertEqual(stores.classify("codex", "same-id", claude.cwd), "code")
        with self.assertRaises(ValueError):
            stores.for_engine("other")

    def test_explicit_folder_grants_are_persisted_and_revocable(self):
        record = self.store.create_session()
        self.store.bind_session(record.work_id, "session-1")
        external = Path(self.tmp.name) / "external"
        external.mkdir()
        self.store.set_grant("session-1", str(external), "read")
        self.assertEqual(self.store.grants(record.work_id), [
            {"path": str(external.resolve()), "mode": "read"},
        ])
        policy = Path(self.store.ensure_claude_policy(
            self.store.get_by_session("session-1")))
        self.assertIn(str(external.resolve()), policy.read_text(encoding="utf-8"))
        self.store.set_grant("session-1", str(external), "none")
        self.assertEqual(self.store.grants(record.work_id), [])


if __name__ == "__main__":
    unittest.main()
