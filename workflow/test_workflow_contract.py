"""Local tests for the trip step's progress and output manifest."""
import hashlib
import io
import json
import os
from pathlib import Path
import tempfile
import unittest
import zipfile
from contextlib import redirect_stdout
from unittest.mock import patch

from trip_job import publish
from workflow import Progress


class WorkflowContractTest(unittest.TestCase):
    def test_snapshot_retains_ready_flag_and_sequence(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            output = Path(temporary)
            progress = Progress(output, {"run_id": "run-123", "attempt_id": "a1"})
            try:
                progress.update("PREFLIGHT", "Ready", model_ready=True)
                first = json.loads((output / "progress.json").read_text())
                progress.update()
                last = json.loads((output / "progress.json").read_text())
                self.assertTrue(last["model_ready"])
                self.assertGreater(last["seq"], first["seq"])
                self.assertFalse((output / "progress.json.tmp").exists())
            finally:
                progress.close()

    def test_failure_manifest_hashes_only_stable_workflow_owned_files(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as temporary:
            root = Path(temporary)
            request = {"run_id": "run-" + "1" * 24, "attempt_id": "a1", "version": 1}
            contents = {
                "metrics.json": '{"model_calls":1}', "diagnostics.json": "{}", "evidence.json": "{}",
                "failure.txt": "Source timeout", "trace.jsonl": "{}\n",
                "execution.json": '{"state":"RUNNING"}', "execution.log": "still growing",
                "unrelated.txt": "exclude",
            }
            for name, content in contents.items():
                (root / name).write_text(content, encoding="utf-8")

            with patch.dict(os.environ, {"SLURM_JOB_ID": "9001", "SLURM_STEP_ID": "2"}), redirect_stdout(io.StringIO()):
                publish(root, request, {"model_service_id": "fixture"}, {}, None, "Source timeout")

            manifest = json.loads((root / "result.json").read_bytes())
            self.assertEqual(manifest["workflow_state"], "FAILED")
            self.assertEqual(manifest["failure_reason"], "Source timeout")
            expected = {"trace.jsonl", "metrics.json", "diagnostics.json", "failure.txt", "evidence.json"}
            self.assertEqual({item["path"] for item in manifest["artifacts"]}, expected)
            with zipfile.ZipFile(root / "artifacts.zip") as archive:
                self.assertEqual(set(archive.namelist()), expected)
                for item in manifest["artifacts"]:
                    blob = (root / item["path"]).read_bytes()
                    self.assertEqual(archive.read(item["path"]), blob)
                    self.assertEqual(item["sha256"], hashlib.sha256(blob).hexdigest())


if __name__ == "__main__":
    unittest.main()
