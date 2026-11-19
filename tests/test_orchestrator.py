"""Deterministic worker fault tests. All CLI commands are intercepted locally.

These prove controller behavior, never actual TACC or model availability.
"""
from __future__ import annotations

import json
import hashlib
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch


E2E = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E))
import orchestrator as orch
from trip.store import TripStore
from trip_requests import trip_request


class CommandCase(unittest.TestCase):
    def worker(self):
        return object.__new__(orch.Worker)

    def test_posix_command_uses_own_session_and_normalizes_output(self):
        process = Mock(returncode=7)
        process.communicate.return_value = ('\x1b[32mtext\x1b[0m\r\x00\n', None)
        with patch.object(orch.os, 'name', 'posix'), \
                patch.object(orch.subprocess, 'Popen', return_value=process) as popen:
            self.assertEqual(self.worker().command(['ps'], timeout=12), (7, 'text\n'))
        self.assertTrue(popen.call_args.kwargs['start_new_session'])
        process.communicate.assert_called_once_with(timeout=12)

    def test_posix_timeout_kills_children_and_preserves_original_timeout(self):
        error = orch.subprocess.TimeoutExpired(['tcloud', 'upload'], 12, output='partial')
        process = Mock(pid=123)
        process.communicate.side_effect = [error, ('partial plus drained', None)]
        with patch.object(orch.os, 'name', 'posix'), \
                patch.object(orch.subprocess, 'Popen', return_value=process), \
                patch.object(orch.signal, 'SIGKILL', 9, create=True), \
                patch.object(orch.os, 'killpg', create=True) as killpg:
            with self.assertRaises(orch.subprocess.TimeoutExpired) as raised:
                self.worker().command(['upload'], timeout=12)
        self.assertIs(raised.exception, error)
        self.assertEqual(error.output, 'partial plus drained')
        killpg.assert_called_once_with(123, 9)
        self.assertEqual(process.communicate.call_args_list[-1].kwargs, {'timeout': 5})

    def test_posix_timeout_cleanup_is_bounded_when_pipe_remains_open(self):
        error = orch.subprocess.TimeoutExpired(['tcloud'], 12)
        process = Mock(pid=123)
        process.communicate.side_effect = [error, orch.subprocess.TimeoutExpired(['tcloud'], 5)]
        with patch.object(orch.os, 'name', 'posix'), \
                patch.object(orch.subprocess, 'Popen', return_value=process), \
                patch.object(orch.signal, 'SIGKILL', 9, create=True), \
                patch.object(orch.os, 'killpg', create=True, side_effect=ProcessLookupError):
            with self.assertRaises(orch.subprocess.TimeoutExpired) as raised:
                self.worker().command(['cat'], timeout=12)
        self.assertIs(raised.exception, error)
        process.stdout.close.assert_called_once_with()
        process.wait.assert_called_once_with(timeout=5)

    def test_non_posix_command_retains_supported_subprocess_path(self):
        with patch.object(orch.os, 'name', 'nt'), \
                patch.object(orch.subprocess, 'run', return_value=Mock(returncode=0, stdout='ok\r\x00')) as run:
            self.assertEqual(self.worker().command(['ps'], timeout=4), (0, 'ok'))
        self.assertEqual(run.call_args.kwargs['timeout'], 4)


class FakeWorker(orch.Worker):
    """No unhandled command can fall through to a process or network call."""

    tacc_user = "test-student"

    def __init__(self, store):
        super().__init__(store)
        self.commands = []
        self.states = {}
        self.files = {}
        self.submit_exception = None
        self.next_job_id = 10001

    def command(self, args, timeout=60):
        args = list(args)
        self.commands.append(args)
        if args[0] == "submit":
            if self.submit_exception:
                raise self.submit_exception
            job_id = str(self.next_job_id)
            self.next_job_id += 1
            self.states[job_id] = "PD"
            return 0, "Submitted batch job " + job_id + "\n"
        if args[0] == "ps":
            job_id = str(args[2])
            state = self.states.get(job_id)
            if not state:
                return 0, "slurm_load_jobs error: Invalid job id specified\n[Tcloud Error] failed\n"
            return 0, "JOBID PARTITION NAME USER ST TIME NODES NODELIST\n%s tacc test test-student %s 0:01 1 node\n" % (job_id, state)
        if args[0] == "cat":
            if args[1] in self.files:
                value = self.files[args[1]]
                return 0, json.dumps(value) if isinstance(value, dict) else value
            return 0, "[Tcloud Error] File not found"
        if args[0] == "cancel":
            return 0, ""
        raise AssertionError("Unexpected fake command: %r" % args)

    def submissions(self):
        return [args for args in self.commands if args[0] == "submit"]


class WorkerCase(unittest.TestCase):
    def setUp(self):
        self.temp_root = E2E / "evidence" / "test-tmp"
        self.temp_root.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="worker-", dir=self.temp_root)
        self.root = Path(self.temp.name)
        self.store = TripStore(self.root / "data")
        self.worker = FakeWorker(self.store)
        self.root_patch = patch.object(orch, "ROOT", self.root)
        self.root_patch.start()
        for kind, name in [("service", "qwen-public-serve"), ("workflow", "storm-e2e-workflow")]:
            directory = self.root / kind
            directory.mkdir()
            (directory / "tuxiv.conf").write_text("job:\n  name: " + name, encoding="utf-8")

    def tearDown(self):
        self.root_patch.stop()
        self.root.resolve().relative_to(self.temp_root.resolve())
        self.temp.cleanup()

    def create(self, key="request-1", **fields):
        state, _ = self.store.create_run(trip_request(), key)
        return self.store.update_run(state["run_id"], **fields) if fields else state

    def service(self, readiness="READY", job_id="9001", service_id="service-one"):
        service = {"owner": orch.SESSION, "model_service_id": service_id,
                   "service_job_id": job_id, "job_id": job_id, "readiness": readiness,
                   "scheduler_state": "RUNNING", "model": "qwen3.8-27b", "base_url": "http://node:8000/v1",
                   "log_prefix": service_id, "submission_state": "SUBMITTED"}
        orch.atomic(self.worker.service_path, service)
        self.worker.states[job_id] = "R"
        self.worker.files["slurm_log/%s-%s.log" % (service_id, job_id)] = (
            "SERVICE_JOB_ID=%s\nSERVICE_BASE_URL=http://node:8000/v1\nApplication startup complete\n" % job_id)
        return service

    def workflow(self, service=None, key="request-1", job_id="20001", status="RUNNING"):
        service = service or self.service()
        self.worker.states[job_id] = "R" if status == "RUNNING" else "PD"
        return self.create(key, status=status, scheduler={"workflow_job_id": job_id, "state": status},
                           model_service=service)

    def manifest(self, run):
        remote = f"{orch.RUN_ROOT}/{run['run_id']}/{run['attempt_id']}"
        manifest = {"run_id": run["run_id"], "attempt_id": run["attempt_id"],
                    "workflow_state": "COMPLETED", "artifacts": [{"path": "report.md", "kind": "report"}]}
        self.worker.files[remote + "/result.json"] = manifest
        self.worker.files[remote + "/report.md"] = "# Fixture report\nEvidence [1]."
        return manifest

    def tick_without_platform_readiness(self):
        with patch.object(self.worker, "refresh_accounting"), patch.object(self.worker, "environment_ready", return_value=True):
            self.worker.tick()

    def test_cli_zero_exit_and_missing_job_is_unknown(self):
        self.assertEqual(self.worker.queue("99999"), "UNKNOWN")

    def test_independent_terminal_cache_survives_stale_watcher_and_restart(self):
        record = {'job_id': '9001', 'user': 'test-student', 'state': 'TIMEOUT',
                  'observed_at': 200, 'exit_code': '0:0', 'source': 'probe-9002.json'}
        path = self.worker.data / 'terminal-accounting.json'
        orch.atomic(path, {'schema_version': 1, 'records': [record]})
        self.worker.files[orch.SESSION + '/accounting.json'] = {
            'queried_at': 100, 'records': [dict(record, state='RUNNING', observed_at=100)],
            'stdout': '9001|test-student|run|RUNNING|0:0|60|20|cpu=20|\n'}
        self.worker.refresh_accounting()
        self.assertEqual(self.worker.accounting['9001'], record)
        self.assertEqual(orch.load(path)['records'], [record])
        restarted = FakeWorker(self.store)
        restarted.refresh_accounting()  # No remote file is available after restart.
        self.assertEqual(restarted.accounting['9001'], record)

    def test_terminal_cache_ignores_nonterminal_foreign_and_malformed_records(self):
        valid = {'job_id': '9001.2', 'user': 'test-student', 'state': 'COMPLETED', 'observed_at': 200}
        records = [valid, dict(valid, job_id='9002', user='another-user'),
                   dict(valid, job_id='9003', state='RUNNING'),
                   dict(valid, job_id='../9004'), dict(valid, job_id='9005', observed_at=None)]
        orch.atomic(self.worker.data / 'terminal-accounting.json', {'records': records})
        self.worker.refresh_accounting()
        self.assertEqual(self.worker.accounting, {'9001.2': valid})

    def test_newer_terminal_observation_updates_cache_and_does_not_lose_step_fields(self):
        old = {'job_id': '9001.2', 'user': 'test-student', 'state': 'COMPLETED', 'observed_at': 100}
        orch.atomic(self.worker.data / 'terminal-accounting.json', {'records': [old]})
        newer = dict(old, observed_at=200, exit_code='0:0', max_rss='1000K', step_id='2')
        self.worker.files[orch.SESSION + '/accounting.json'] = {
            'queried_at': 200, 'records': [newer], 'stdout': ''}
        self.worker.refresh_accounting()
        self.assertEqual(self.worker.accounting['9001.2'], newer)
        self.assertEqual(orch.load(self.worker.data / 'terminal-accounting.json')['records'], [newer])

    def test_service_creation_is_reused_across_worker_restarts(self):
        first = self.worker.ensure_service()
        second_worker = FakeWorker(self.store)
        second = second_worker.ensure_service()
        self.assertEqual(first["model_service_id"], second["model_service_id"])
        self.assertEqual(len(self.worker.submissions()), 1)
        self.assertEqual(second_worker.submissions(), [])

    def test_interrupted_service_submission_is_not_automatically_retried(self):
        self.worker.submit_exception = TimeoutError("response lost after possible submit")
        first = self.worker.ensure_service()
        self.assertEqual(first["submission_state"], "UNKNOWN")
        self.assertEqual(first["readiness"], "UNKNOWN")
        restarted = FakeWorker(self.store)
        self.assertEqual(restarted.ensure_service()["model_service_id"], first["model_service_id"])
        self.assertEqual(restarted.submissions(), [])

    def test_repeated_ticks_do_not_submit_same_workflow_again(self):
        self.service()
        state = self.create()
        self.tick_without_platform_readiness()
        after = self.store.get_run(state["run_id"])
        self.assertEqual(after["status"], "QUEUED")
        self.assertIsNotNone(after["scheduler"]["workflow_job_id"])
        self.tick_without_platform_readiness()
        self.assertEqual(len(self.worker.submissions()), 1)

    def test_submit_timeout_stays_unknown_across_ticks_without_duplicate(self):
        self.service()
        state = self.create()
        self.worker.submit_exception = TimeoutError("uncertain submission")
        self.tick_without_platform_readiness()
        self.assertEqual(self.store.get_run(state["run_id"])["status"], "UNKNOWN")
        self.tick_without_platform_readiness()
        self.assertEqual(len(self.worker.submissions()), 1)

    def test_recover_job_id_from_ledger_without_resubmitting(self):
        service = self.service()
        run = self.create(status="SUBMITTING", model_service=service)
        path = self.store.data_dir / "submissions" / (run["run_id"] + "-a1.json")
        orch.atomic(path, {"run_id": run["run_id"], "attempt_id": "a1", "job_id": "20001"})
        self.worker.states["20001"] = "R"
        self.worker.monitor_run(run, service)
        after = self.store.get_run(run["run_id"])
        self.assertEqual(after["scheduler"]["workflow_job_id"], "20001")
        self.assertEqual(after["status"], "RUNNING")
        self.assertEqual(self.worker.submissions(), [])

    def test_cancel_one_workflow_never_cancels_shared_service(self):
        service = self.service()
        first = self.workflow(service, key="first", job_id="20001")
        second = self.workflow(service, key="second", job_id="20002")
        cancelled = self.store.request_cancel(first["run_id"])
        self.worker.monitor_run(cancelled, service)
        cancels = [args for args in self.worker.commands if args[0] == "cancel"]
        self.assertEqual(cancels, [["cancel", "-j", "20001"]])
        self.assertEqual(self.store.get_run(second["run_id"])["status"], "RUNNING")
        with self.assertRaises(RuntimeError):
            self.worker.stop_service(service)

    def test_cleanup_never_cancels_foreign_service(self):
        service = self.service()
        service["owner"] = "another-application"
        self.worker.stop_service(service)
        self.assertFalse(any(args[0] == "cancel" for args in self.worker.commands))

    def test_result_without_scheduler_confirmation_is_not_success(self):
        service = self.service()
        run = self.workflow(service)
        self.worker.states.pop("20001")
        self.manifest(run)
        self.worker.monitor_run(run, service)
        self.assertEqual(self.store.get_run(run["run_id"])["status"], "UNKNOWN")

    def test_success_requires_matching_application_and_accounting_evidence(self):
        service = self.service()
        run = self.workflow(service)
        self.manifest(run)
        self.worker.accounting["20001"] = {"state": "COMPLETED", "exit_code": "0:0", "observed_at": time.time()}
        self.worker.monitor_run(run, service)
        self.assertEqual(self.store.get_run(run["run_id"])["status"], "SUCCEEDED")
        self.assertTrue((self.store.artifact_dir(run["run_id"], "a1") / "report.md").exists())

    def test_nonzero_scheduler_exit_cannot_be_hidden_by_success_manifest(self):
        service = self.service()
        run = self.workflow(service)
        self.manifest(run)
        self.worker.accounting["20001"] = {"state": "COMPLETED", "exit_code": "1:0", "observed_at": time.time()}
        self.worker.monitor_run(run, service)
        self.assertEqual(self.store.get_run(run["run_id"])["status"], "FAILED")

    def test_failure_manifest_collects_legacy_log_and_hashed_diagnostics(self):
        service = self.service()
        run = self.workflow(service)
        remote = f"{orch.RUN_ROOT}/{run['run_id']}/a1"
        files = {'execution.log': 'Traceback: injected tool failure\n',
                 'failure.txt': 'INJECTED_TOOL_FAILURE\n',
                 'metrics.json': '{"model_calls": 1}\n'}
        manifest = {'run_id': run['run_id'], 'attempt_id': 'a1', 'workflow_state': 'FAILED',
                    'failure_reason': 'INJECTED_TOOL_FAILURE', 'artifacts': []}
        for name, content in files.items():
            item = {'kind': 'diagnostic', 'path': name}
            # Historical execution.log had no hash; new stable files do.
            if name != 'execution.log':
                item['sha256'] = hashlib.sha256(content.encode()).hexdigest()
            manifest['artifacts'].append(item)
            self.worker.files[remote + '/' + name] = content
        self.worker.files[remote + '/result.json'] = manifest
        self.worker.accounting['20001'] = {'state': 'FAILED', 'exit_code': '1:0',
                                          'observed_at': time.time()}
        self.worker.monitor_run(run, service)
        after = self.store.get_run(run['run_id'])
        self.assertEqual(after['status'], 'FAILED')
        self.assertEqual(after['error'], 'INJECTED_TOOL_FAILURE')
        self.assertEqual({x['path'] for x in self.store.artifacts(run['run_id'])},
                         set(files) | {'result.json'})
        dest = self.store.artifact_dir(run['run_id'], 'a1')
        for name, content in files.items():
            self.assertEqual((dest / name).read_text(encoding='utf-8'), content)
        self.assertEqual(orch.load(dest / 'result.json'), manifest)

    def test_progress_from_other_attempt_cannot_update_display(self):
        service = self.service()
        run = self.workflow(service)
        self.worker.files[f"{orch.RUN_ROOT}/{run['run_id']}/a1/progress.json"] = {
            "run_id": run["run_id"], "attempt_id": "a2", "phase": "WRITING", "model_ready": True}
        self.worker.monitor_run(run, service)
        self.assertEqual(self.store.get_run(run["run_id"])["workflow"]["phase"], "WAITING")

    def test_draining_service_cannot_be_reactivated_from_old_startup_log(self):
        service = self.service(readiness="DRAINING")
        after = self.worker.poll_service(service)
        self.assertEqual(after["readiness"], "DRAINING")

    def test_unknown_scheduler_never_reads_service_log_even_when_draining(self):
        for readiness, expected in [('DRAINING', 'DRAINING'), ('READY', 'UNKNOWN')]:
            with self.subTest(readiness=readiness):
                service = self.service(readiness=readiness)
                self.worker.states.pop('9001')
                self.worker.commands.clear()
                after = self.worker.poll_service(service)
                self.assertEqual(after['scheduler_state'], 'UNKNOWN')
                self.assertEqual(after['readiness'], expected)
                self.assertFalse(any(args[0] == 'cat' for args in self.worker.commands))

    def test_monitor_timeout_does_not_starve_later_run(self):
        service = self.service()
        first = self.workflow(service, key='first', job_id='20001')
        second = self.workflow(service, key='second', job_id='20002')
        self.manifest(second)
        self.worker.accounting['20002'] = {'state': 'COMPLETED', 'exit_code': '0:0',
                                          'observed_at': time.time()}
        original_monitor = self.worker.monitor_run

        def monitor(run, bound_service):
            if run['run_id'] == first['run_id']:
                raise orch.subprocess.TimeoutExpired(['tcloud', 'cat'], 60)
            return original_monitor(run, bound_service)

        with patch.object(self.worker, 'monitor_run', side_effect=monitor):
            self.tick_without_platform_readiness()
        self.assertEqual(self.store.get_run(first['run_id'])['status'], 'RUNNING')
        self.assertEqual(self.store.get_run(second['run_id'])['status'], 'SUCCEEDED')
        log = (self.worker.data / 'worker-errors.log').read_text(encoding='utf-8')
        self.assertIn('run=' + first['run_id'] + ' attempt=a1 TimeoutExpired', log)
        self.assertEqual(self.worker.submissions(), [])

    def test_monitor_does_not_swallow_process_control_exceptions(self):
        service = self.service()
        self.workflow(service)
        for error in (KeyboardInterrupt(), SystemExit(2)):
            with self.subTest(error=type(error).__name__), \
                    patch.object(self.worker, 'monitor_run', side_effect=error):
                with self.assertRaises(type(error)):
                    self.tick_without_platform_readiness()

    def test_stale_running_accounting_does_not_mask_missing_job(self):
        service = self.service()
        run = self.workflow(service)
        self.worker.states.pop("20001")
        self.worker.accounting["20001"] = {"state": "RUNNING", "exit_code": "0:0", "observed_at": time.time() - 3600}
        self.worker.monitor_run(run, service)
        after = self.store.get_run(run["run_id"])
        self.assertEqual(after["scheduler"]["state"], "UNKNOWN")
        self.assertNotEqual(after["status"], "SUCCEEDED")

    def test_idle_controller_reconciles_a_draining_service(self):
        service = self.service(readiness="DRAINING")
        self.worker.states.pop("9001")
        self.worker.accounting["9001"] = {"state": "CANCELLED", "exit_code": "0:15", "observed_at": time.time()}
        with patch.object(self.worker, "refresh_accounting"):
            self.worker.tick()
        after = orch.load(self.worker.service_path)
        self.assertIn(after["readiness"], {"STOPPED", "UNAVAILABLE"})
        self.assertEqual(after["scheduler_state"], "CANCELLED")

    def test_replaced_service_does_not_orphan_old_queued_workflow(self):
        old_service = self.service(readiness="STOPPED")
        old_run = self.workflow(old_service, key="old", status="QUEUED")
        self.worker.states.pop("9001")
        self.worker.accounting["9001"] = {"state": "CANCELLED", "exit_code": "0:15", "observed_at": time.time()}
        self.create(key="new")

        def replace_service():
            return self.service(job_id="9002", service_id="service-two")

        with patch.object(self.worker, "ensure_service", side_effect=replace_service), \
                patch.object(self.worker, "refresh_accounting"), \
                patch.object(self.worker, "environment_ready", return_value=True):
            self.worker.tick()
        after = self.store.get_run(old_run["run_id"])
        self.assertEqual(after["model_service"]["service_job_id"], "9001")
        self.assertTrue(after["cancel_requested"], "old job must stop before an attempt can bind the replacement")


if __name__ == "__main__":
    unittest.main()
