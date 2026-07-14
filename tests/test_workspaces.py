import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

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

    def test_unbound_records_are_indexed_by_canonical_private_cwd(self):
        unbound = self.store.create_session()
        bound = self.store.create_session()
        self.store.bind_session(bound.work_id, "session-bound")

        records = self.store.unbound_records_by_cwd()

        self.assertEqual(list(records), [os.path.realpath(unbound.cwd)])
        self.assertEqual(records[os.path.realpath(unbound.cwd)].work_id,
                         unbound.work_id)

    def test_artifacts_list_only_user_deliverables_inside_private_workspace(self):
        record = self.store.create_session()
        self.store.bind_session(record.work_id, "session-1")
        workspace = Path(record.cwd)
        (workspace / "report.md").write_text("# result", encoding="utf-8")
        slides = workspace / "output" / "deck.pptx"
        slides.parent.mkdir()
        slides.write_bytes(b"presentation")
        (workspace / ".private.txt").write_text("hidden", encoding="utf-8")
        (workspace / "资料库").mkdir(exist_ok=True)
        (workspace / "资料库" / "source.csv").write_text("input", encoding="utf-8")
        outside = Path(self.tmp.name) / "outside.txt"
        outside.write_text("outside", encoding="utf-8")
        (workspace / "linked.txt").symlink_to(outside)

        artifacts = self.store.artifacts("session-1")

        self.assertEqual({item["path"] for item in artifacts}, {
            "report.md", "output/deck.pptx",
        })
        by_path = {item["path"]: item for item in artifacts}
        self.assertTrue(by_path["report.md"]["previewable"])
        self.assertEqual(by_path["report.md"]["kind"], "document")
        self.assertTrue(by_path["output/deck.pptx"]["previewable"])
        self.assertEqual(by_path["output/deck.pptx"]["kind"], "presentation")

    def test_claude_policy_copies_only_runtime_provider_settings(self):
        config_dir = Path(self.tmp.name) / "claude-config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text(json.dumps({
            "model": "claude-sonnet-4-5",
            "env": {
                "ANTHROPIC_BASE_URL": "https://provider.example/v1",
                "ANTHROPIC_AUTH_TOKEN": "provider-token",
                "ANTHROPIC_MODEL": "provider-model",
                "UNRELATED_SECRET": "must-not-cross",
            },
            "hooks": {"UserPromptSubmit": [{"hooks": [{"command": "inject-memory"}]}]},
            "permissions": {"allow": ["Read(~/private/**)"]},
            "enabledPlugins": {"global-memory": True},
            "theme": "dark",
        }), encoding="utf-8")
        record = self.store.create_session()

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
            policy = Path(self.store.ensure_claude_policy(record))

        payload = json.loads(policy.read_text(encoding="utf-8"))
        self.assertEqual(payload["model"], "claude-sonnet-4-5")
        self.assertEqual(payload["env"], {
            "ANTHROPIC_BASE_URL": "https://provider.example/v1",
            "ANTHROPIC_AUTH_TOKEN": "provider-token",
            "ANTHROPIC_MODEL": "provider-model",
        })
        self.assertNotIn("hooks", payload)
        self.assertNotIn("enabledPlugins", payload)
        self.assertNotIn("UNRELATED_SECRET", repr(payload))
        self.assertEqual(payload["sandbox"]["filesystem"]["allowRead"],
                         [record.cwd])
        self.assertEqual(payload["sandbox"]["filesystem"]["allowWrite"],
                         [record.cwd])
        self.assertEqual(policy.stat().st_mode & 0o777, 0o600)

    def test_claude_policy_ignores_invalid_or_oversized_user_settings(self):
        config_dir = Path(self.tmp.name) / "claude-config"
        config_dir.mkdir()
        (config_dir / "settings.json").write_text("{" + "x" * 1_100_000,
                                                   encoding="utf-8")
        record = self.store.create_session()

        with patch.dict(os.environ, {"CLAUDE_CONFIG_DIR": str(config_dir)}):
            policy = Path(self.store.ensure_claude_policy(record))

        payload = json.loads(policy.read_text(encoding="utf-8"))
        self.assertNotIn("model", payload)
        self.assertNotIn("env", payload)


if __name__ == "__main__":
    unittest.main()
