from __future__ import annotations

import importlib.util
import tempfile
import unittest
from pathlib import Path

import yaml


MODULE_PATH = Path(__file__).resolve().parents[1] / "tools" / "experiment.py"
SPEC = importlib.util.spec_from_file_location("experiment_tool", MODULE_PATH)
experiment_tool = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(experiment_tool)


class ExperimentToolTest(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        (self.root / "experiments/main").mkdir(parents=True)
        (self.root / "runs").mkdir()
        self.config_path = self.root / "experiments/main/demo.yaml"
        self.config = {
            "experiment": {"id": "main/demo", "name": "Demo", "type": "main", "version": 1},
            "spec": {
                "stages": [{"id": "prepare", "name": "Prepare"}, {"id": "evaluate", "name": "Evaluate"}],
                "completion": {
                    "required_stages": ["prepare", "evaluate"],
                    "required_outputs": ["runs/<run_id>/<execution_id>/results.json"],
                },
            },
        }
        self.config_path.write_text(yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")

    def tearDown(self) -> None:
        self.temporary.cleanup()

    def test_validate_reports_missing_version(self) -> None:
        data = {"experiment": {"id": "main/x", "name": "X", "type": "main"}, "spec": {}}
        errors, _ = experiment_tool.validate_experiment(data)
        self.assertIn("MISSING_VERSION", {entry["code"] for entry in errors})

    def test_create_run_freezes_multiple_unique_experiments(self) -> None:
        second = self.root / "experiments/main/second.yaml"
        second_data = {"experiment": {"id": "main/second", "name": "Second", "type": "main", "version": 1}, "spec": {"stages": []}}
        second.write_text(yaml.safe_dump(second_data, sort_keys=False), encoding="utf-8")
        result = experiment_tool.create_run(
            self.root, "Comparison", [str(self.config_path), str(second)], "fixed-run", "2026-08-27T00:00:00Z"
        )
        self.assertEqual(["exec-001", "exec-002"], result["execution_ids"])
        document = yaml.safe_load((self.root / result["run_file"]).read_text(encoding="utf-8"))
        self.assertEqual(self.config, document["executions"][0]["config_snapshot"])
        self.assertEqual(experiment_tool.canonical_sha256(self.config), document["executions"][0]["experiment"]["sha256"])

    def test_create_run_rejects_duplicate_experiment_ids(self) -> None:
        duplicate = self.root / "experiments/main/duplicate.yaml"
        duplicate.write_text(self.config_path.read_text(encoding="utf-8"), encoding="utf-8")
        with self.assertRaisesRegex(experiment_tool.ToolError, "重复 Experiment ID"):
            experiment_tool.create_run(self.root, "Duplicate", [str(self.config_path), str(duplicate)], "duplicate-run")

    def test_stage_state_machine_and_finish(self) -> None:
        experiment_tool.create_run(self.root, "Demo", [str(self.config_path)], "demo-run", "2026-08-27T00:00:00Z")
        with self.assertRaisesRegex(experiment_tool.ToolError, "不允许"):
            experiment_tool.update_stage(self.root, "demo-run", "exec-001", "prepare", "completed")
        experiment_tool.update_stage(self.root, "demo-run", "exec-001", "prepare", "running", "2026-08-27T00:01:00Z")
        experiment_tool.update_stage(self.root, "demo-run", "exec-001", "prepare", "completed", "2026-08-27T00:02:00Z")
        experiment_tool.update_stage(self.root, "demo-run", "exec-001", "evaluate", "running", "2026-08-27T00:03:00Z")
        experiment_tool.update_stage(self.root, "demo-run", "exec-001", "evaluate", "completed", "2026-08-27T00:04:00Z")
        with self.assertRaisesRegex(experiment_tool.ToolError, "不满足完成条件"):
            experiment_tool.finish_run(self.root, "demo-run", "completed")
        result_file = self.root / "runs/demo-run/exec-001/results.json"
        result_file.parent.mkdir(parents=True)
        result_file.write_text("{}\n", encoding="utf-8")
        result = experiment_tool.finish_run(self.root, "demo-run", "completed", "2026-08-27T00:05:00Z")
        self.assertEqual("completed", result["status"])
        self.assertTrue((self.root / result["experiment_log"]).is_file())

    def test_failed_run_can_finish_without_outputs(self) -> None:
        experiment_tool.create_run(self.root, "Demo", [str(self.config_path)], "failed-run")
        result = experiment_tool.finish_run(self.root, "failed-run", "failed")
        self.assertEqual("failed", result["status"])
        document = yaml.safe_load((self.root / "runs/failed-run/run.yaml").read_text(encoding="utf-8"))
        self.assertEqual("failed", document["executions"][0]["status"])

    def test_no_stage_experiment_can_finish(self) -> None:
        self.config["spec"] = {}
        self.config_path.write_text(yaml.safe_dump(self.config, sort_keys=False), encoding="utf-8")
        experiment_tool.create_run(self.root, "No stages", [str(self.config_path)], "no-stages")
        experiment_tool.finish_run(self.root, "no-stages", "completed")
        document = yaml.safe_load((self.root / "runs/no-stages/run.yaml").read_text(encoding="utf-8"))
        self.assertEqual("completed", document["executions"][0]["status"])


if __name__ == "__main__":
    unittest.main()
