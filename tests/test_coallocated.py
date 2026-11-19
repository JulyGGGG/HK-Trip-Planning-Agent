"""Local fault tests for coallocated dispatch; no CLI, SSH or model is run."""
import json
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch


E2E = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E))
import coallocated as co
import orchestrator as orch
from trip.store import TripStore
from trip_requests import trip_request


class FakeCoallocated(co.CoallocatedWorker):
    tacc_user = 'test-student'

    def __init__(self, store):
        super().__init__(store)
        self.commands = []
        self.files = {}
        self.states = {}
        self.commit_error = None

    def command(self, args, timeout=60):
        args = list(args)
        self.commands.append(args)
        if args[0] == 'upload':
            if Path(args[1]).name == 'dispatch.json' and self.commit_error:
                raise self.commit_error
            return 0, 'Uploaded\n'
        if args[0] == 'cat':
            item = self.files.get(args[1])
            if item is None:
                return 0, '[Tcloud Error] missing'
            return 0, json.dumps(item) if isinstance(item, dict) else item
        if args[0] == 'ps':
            job = args[2]
            if job not in self.states:
                return 0, '[Tcloud Error] missing'
            return 0, 'JOBID PARTITION NAME USER ST TIME NODES NODELIST\n%s tacc test test-student %s 0:01 1 node\n' % (job, self.states[job])
        raise AssertionError('No real command is permitted: %r' % args)

    def uploads(self):
        return [row for row in self.commands if row[0] == 'upload']


class CoallocatedCase(unittest.TestCase):
    def setUp(self):
        self.temp_parent = E2E / 'evidence' / 'test-tmp'
        self.temp_parent.mkdir(parents=True, exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix='coallocated-', dir=str(self.temp_parent))
        self.root = Path(self.temp.name)
        self.store = TripStore(self.root / 'data')
        self.worker = FakeCoallocated(self.store)
        self.patches = [patch.object(co, 'ROOT', self.root), patch.object(orch, 'ROOT', self.root)]
        for item in self.patches:
            item.start()

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()
        self.root.resolve().relative_to(self.temp_parent.resolve())
        self.temp.cleanup()

    def service(self, job='9001', service_id='qwen-one', readiness='READY'):
        result = {'owner': orch.SESSION, 'model_service_id': service_id,
                  'service_job_id': job, 'readiness': readiness, 'scheduler_state': 'RUNNING',
                  'base_url': 'http://node:8000/v1', 'model': 'qwen3.8-27b',
                  'log_prefix': service_id, 'execution_mode': 'coallocated'}
        orch.atomic(self.worker.service_path, result)
        self.worker.states[job] = 'R'
        self.worker.files['slurm_log/%s-%s.log' % (service_id, job)] = (
            'SERVICE_JOB_ID=%s\nSERVICE_BASE_URL=http://node:8000/v1\nApplication startup complete\n' % job)
        return result

    def create(self, key='request-one', service=None, status='ACCEPTED'):
        run, _ = self.store.create_run(trip_request(), key)
        if service:
            run = self.store.update_run(run['run_id'], status=status, model_service=service,
                scheduler={'workflow_job_id': service['service_job_id'], 'workflow_step_id': None,
                           'execution_kind': 'slurm_step', 'state': 'PENDING'})
        return run

    def remote(self, run):
        return f"{co.RUN_ROOT}/{run['run_id']}/{run['attempt_id']}"

    def receipt(self, run, state='COMPLETED', step='2', returncode=0, **fields):
        item = {'run_id': run['run_id'], 'attempt_id': run['attempt_id'],
                'allocation_job_id': run['model_service']['service_job_id'],
                'model_service_id': run['model_service']['model_service_id'],
                'step_id': step, 'state': state, 'returncode': returncode}
        item.update(fields)
        self.worker.files[self.remote(run) + '/execution.json'] = item
        return item

    def manifest(self, run, **fields):
        item = {'run_id': run['run_id'], 'attempt_id': run['attempt_id'],
                'workflow_job_id': run['model_service']['service_job_id'], 'workflow_step_id': '2',
                'workflow_state': 'COMPLETED', 'artifacts': [{'path': 'report.md', 'kind': 'report'}]}
        item.update(fields)
        self.worker.files[self.remote(run) + '/result.json'] = item
        self.worker.files[self.remote(run) + '/report.md'] = '# Real-artifact format fixture\n'
        return item

    def accounting(self, key='9001.2', state='COMPLETED', exit_code='0:0'):
        self.worker.accounting[key] = {'state': state, 'exit_code': exit_code,
                                      'observed_at': time.time(), 'user': 'test-student'}

    def monitor(self, run, service):
        self.worker.monitor_run(self.store.get_run(run['run_id']), service)
        return self.store.get_run(run['run_id'])

    def tick(self, worker=None):
        worker = worker or self.worker
        with patch.object(worker, 'refresh_accounting'), patch.object(worker, 'environment_ready', return_value=True):
            worker.tick()

    def test_dispatch_commits_last_and_does_not_submit_a_second_allocation(self):
        service = self.service()
        run = self.create()
        self.worker.bind_submit(run, service)
        after = self.store.get_run(run['run_id'])
        uploads = self.worker.uploads()
        self.assertEqual(len(uploads), 2)
        self.assertTrue(Path(uploads[0][1]).is_dir())
        self.assertEqual(Path(uploads[1][1]).name, 'dispatch.json')
        self.assertEqual(after['status'], 'QUEUED')
        self.assertEqual(after['scheduler']['workflow_job_id'], '9001')
        self.assertEqual(after['submission']['state'], 'COMMITTED')
        self.assertFalse(any(row[0] == 'submit' for row in self.worker.commands))

    def test_repeated_tick_and_restart_do_not_reupload_committed_work(self):
        self.service()
        run = self.create()
        self.tick()
        self.tick()
        self.assertEqual(len(self.worker.uploads()), 2)
        restarted = FakeCoallocated(TripStore(self.root / 'data'))
        restarted.files = self.worker.files.copy()
        restarted.states = self.worker.states.copy()
        self.tick(restarted)
        self.assertEqual(restarted.uploads(), [])
        self.assertEqual(self.store.get_run(run['run_id'])['scheduler']['workflow_job_id'], '9001')

    def test_uncertain_dispatch_commit_is_not_retried_on_restart(self):
        service = self.service()
        run = self.create()
        self.worker.commit_error = TimeoutError('response lost after possible commit')
        self.worker.bind_submit(run, service)
        self.assertEqual(self.store.get_run(run['run_id'])['status'], 'UNKNOWN')
        restarted = FakeCoallocated(TripStore(self.root / 'data'))
        restarted.files = self.worker.files.copy()
        restarted.states = self.worker.states.copy()
        self.tick(restarted)
        self.assertEqual(restarted.uploads(), [])
        self.assertEqual(self.store.get_run(run['run_id'])['status'], 'UNKNOWN')

    def test_stale_service_snapshot_is_not_dispatched(self):
        old = self.service()
        self.service(job='9002', service_id='qwen-two')
        run = self.create()
        self.worker.bind_submit(run, old)
        self.assertEqual(self.worker.uploads(), [])
        self.assertEqual(self.store.get_run(run['run_id'])['status'], 'ACCEPTED')

    def test_draining_service_is_not_dispatched(self):
        service = self.service(readiness='DRAINING')
        run = self.create()
        self.worker.bind_submit(run, service)
        self.assertEqual(self.worker.uploads(), [])

    def test_terminal_allocation_outranks_stale_supervisor_snapshot(self):
        for state, expected in [('CANCELLED', 'STOPPED'), ('TIMEOUT', 'UNAVAILABLE')]:
            for snapshot in ({'draining': True}, {'stopping': True}, {'failure': 'old failure'}):
                with self.subTest(state=state, snapshot=snapshot):
                    service = self.service(readiness='DRAINING')
                    self.accounting('9001', state=state)
                    old = dict(snapshot, model_service_id=service['model_service_id'])
                    self.worker.files[orch.SESSION + '/services/9001.json'] = old
                    after = self.worker.poll_service(service)
                    self.assertEqual(after['scheduler_state'], state)
                    self.assertEqual(after['readiness'], expected)
                    self.assertNotIn('failure', after)
                    self.assertEqual(after['supervisor'], old)

    def test_cancel_uploads_only_run_marker_and_never_scancels_allocation(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        other = self.create(key='other', service=service, status='RUNNING')
        self.receipt(run, state='RUNNING', returncode=None)
        self.store.request_cancel(run['run_id'])
        after = self.monitor(run, service)
        self.assertEqual(after['status'], 'CANCELLING')
        self.assertEqual(len(self.worker.uploads()), 1)
        self.assertEqual(Path(self.worker.uploads()[0][1]).name, 'cancel.json')
        self.assertIn(run['run_id'] + '-a1/', self.worker.uploads()[0][2])
        self.assertEqual(self.store.get_run(other['run_id'])['status'], 'RUNNING')
        self.monitor(run, service)
        self.assertEqual(len(self.worker.uploads()), 1)
        self.assertFalse(any(row[0] in {'submit', 'cancel'} for row in self.worker.commands))

    def test_cancel_before_step_can_finish_from_supervisor_receipt(self):
        service = self.service()
        run = self.create(service=service, status='QUEUED')
        self.receipt(run, state='CANCELLED', step=None, returncode=None)
        after = self.monitor(run, service)
        self.assertEqual(after['status'], 'CANCELLED')

    def test_failure_before_step_does_not_require_nonexistent_step_accounting(self):
        service = self.service()
        run = self.create(service=service, status='QUEUED')
        self.receipt(run, state='FAILED', step=None, returncode=None, error='STALE_SERVICE_BINDING')
        after = self.monitor(run, service)
        self.assertEqual(after['status'], 'FAILED')
        self.assertIn('STALE_SERVICE_BINDING', after['error'])

    def test_success_requires_application_receipt_and_independent_step_accounting(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run)
        self.accounting()
        self.accounting('9001', state='RUNNING')
        after = self.monitor(run, service)
        self.assertEqual(after['status'], 'SUCCEEDED')
        self.assertEqual(after['scheduler']['workflow_step_id'], '2')
        self.assertEqual(after['model_service']['scheduler_state'], 'RUNNING')

    def test_manifest_and_receipt_without_step_accounting_stay_unknown(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run)
        self.assertEqual(self.monitor(run, service)['status'], 'UNKNOWN')

    def test_manifest_and_accounting_without_receipt_are_not_success(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.accounting()
        self.assertNotEqual(self.monitor(run, service)['status'], 'SUCCEEDED')

    def test_nonzero_step_exit_cannot_be_hidden_by_success_artifacts(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run)
        self.accounting(exit_code='1:0')
        self.assertEqual(self.monitor(run, service)['status'], 'FAILED')

    def test_nonzero_supervisor_returncode_cannot_be_hidden_by_accounting(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run, state='FAILED', returncode=1)
        self.accounting()
        self.assertEqual(self.monitor(run, service)['status'], 'FAILED')

    def test_failed_receipt_with_zero_code_is_still_not_success(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run, state='FAILED', returncode=0)
        self.accounting()
        self.assertNotEqual(self.monitor(run, service)['status'], 'SUCCEEDED')

    def test_other_attempt_receipt_cannot_finish_current_attempt(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run, attempt_id='a2')
        self.accounting()
        after = self.monitor(run, service)
        self.assertEqual(after['status'], 'UNKNOWN')
        self.assertIn('identity mismatch', after['error'])

    def test_other_allocation_receipt_cannot_finish_current_attempt(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run, allocation_job_id='9002')
        self.accounting()
        self.assertEqual(self.monitor(run, service)['status'], 'UNKNOWN')

    def test_manifest_job_and_step_must_match_current_execution(self):
        for mismatch in ({'workflow_job_id': '9002'}, {'workflow_step_id': '3'}):
            with self.subTest(mismatch=mismatch):
                service = self.service()
                run = self.create(key=json.dumps(mismatch), service=service, status='RUNNING')
                self.manifest(run, **mismatch)
                self.receipt(run)
                self.accounting()
                self.assertNotEqual(self.monitor(run, service)['status'], 'SUCCEEDED')

    def test_foreign_progress_cannot_supply_step_identity(self):
        service = self.service()
        run = self.create(service=service, status='RUNNING')
        self.manifest(run)
        self.receipt(run, step=None)
        self.worker.files[self.remote(run) + '/progress.json'] = {
            'run_id': run['run_id'], 'attempt_id': 'a2', 'workflow_step_id': '2', 'phase': 'FAKE'}
        self.accounting()
        after = self.monitor(run, service)
        self.assertNotEqual(after['status'], 'SUCCEEDED')
        self.assertNotEqual(after['workflow']['phase'], 'FAKE')

    def test_old_allocation_failure_never_rebinds_to_replacement(self):
        old = self.service()
        run = self.create(service=old, status='QUEUED')
        new = self.service(job='9002', service_id='qwen-two')
        self.accounting('9001', state='CANCELLED', exit_code='0:15')
        after = self.monitor(run, new)
        self.assertEqual(after['status'], 'FAILED')
        self.assertEqual(after['model_service']['service_job_id'], '9001')
        self.assertEqual(after['scheduler']['workflow_job_id'], '9001')
        self.assertEqual(self.worker.uploads(), [])


if __name__ == '__main__':
    unittest.main()
