"""Pure local tests: Child/srun are mocked; no remote or model operations."""
import importlib.util
import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

spec = importlib.util.spec_from_file_location('combined_supervisor', Path(__file__).with_name('supervisor.py'))
supervisor = importlib.util.module_from_spec(spec)
spec.loader.exec_module(supervisor)


class FakeProcess:
    pid = 999999
    returncode = None
    def poll(self):
        return self.returncode


class FakeChild:
    calls = []
    def __init__(self, name, command, cwd, env, log_path, on_line=None):
        self.process = FakeProcess()
        self.terminated = []
        FakeChild.calls.append((name, command, cwd, env, log_path))
    def terminate(self, force=False):
        self.terminated.append(force)
    def finish(self):
        pass


class SupervisorCase(unittest.TestCase):
    def setUp(self):
        self.temp_root = Path(__file__).resolve().parent / '.test-tmp'
        self.temp_root.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=str(self.temp_root))
        self.base = Path(self.temp.name)
        self.s = object.__new__(supervisor.Supervisor)
        self.s.user = self.base / 'user'
        self.s.run_root = self.s.user / 'hk-trip-planner' / 'runs'
        self.s.work = self.base / 'work'
        self.s.inbox = self.s.user / 'hk-trip-planner' / 'inbox'
        self.s.inbox.mkdir(parents=True)
        (self.s.work / 'workflow').mkdir(parents=True)
        (self.s.work / 'workflow' / 'job.sh').write_text('# fixture')
        self.s.job = '28001'
        self.s.service_id = 'qwen-e2e-fixture'
        self.s.base_url = 'http://10-0-1-11:8000/v1'
        self.s.created_at = time.time()
        self.s.overlap = True
        self.s.active = {}
        self.s.stopping = False
        FakeChild.calls = []
        self.child_patch = patch.object(supervisor, 'Child', FakeChild)
        self.child_patch.start()

    def tearDown(self):
        self.child_patch.stop()
        self.assertEqual(self.base.resolve().parent, self.temp_root.resolve())
        self.temp.cleanup()

    def inbox(self, suffix='1'):
        run = 'run-' + suffix.zfill(24)
        request = {'run_id': run, 'attempt_id': 'a1', 'workflow': 'hk_trip'}
        config = {'service_job_id': self.s.job, 'model_service_id': self.s.service_id,
                  'base_url': self.s.base_url, 'model': 'qwen3.8-27b'}
        directory = self.s.inbox / (run + '-a1')
        directory.mkdir()
        supervisor.atomic(directory / 'request.json', request)
        supervisor.atomic(directory / 'model-config.json', config)
        supervisor.atomic(directory / 'dispatch.json', {})
        return directory, self.s.run_root / run / 'a1'

    def test_committed_dispatch_launches_one_cpu_step_and_persists_identity(self):
        directory, output = self.inbox()
        self.s.dispatch(directory)
        self.assertEqual(len(FakeChild.calls), 1)
        args = FakeChild.calls[0][1]
        self.assertIn('--jobid=28001', args)
        self.assertIn('--gres=none', args)
        self.assertIn('--overlap', args)
        self.assertNotIn('--exclusive', args)
        receipt = supervisor.read_json(output / 'execution.json')
        self.assertEqual(receipt['state'], 'RUNNING')
        self.assertEqual(receipt['allocation_job_id'], '28001')

    def test_existing_claim_is_never_replayed_after_supervisor_restart(self):
        directory, output = self.inbox()
        supervisor.atomic(output / 'execution.json', {'state': 'STARTING'})
        self.s.dispatch(directory)
        self.assertFalse(FakeChild.calls)

    def test_cancel_before_start_never_creates_step(self):
        directory, output = self.inbox()
        supervisor.atomic(directory / 'cancel.json', {})
        self.s.dispatch(directory)
        self.assertEqual(supervisor.read_json(output / 'execution.json')['state'], 'CANCELLED')
        self.assertFalse(FakeChild.calls)

    def test_stale_binding_rejected_before_any_process(self):
        directory, output = self.inbox()
        config = supervisor.read_json(directory / 'model-config.json')
        config['service_job_id'] = '27999'
        supervisor.atomic(directory / 'model-config.json', config)
        with self.assertRaisesRegex(ValueError, 'STALE_SERVICE_BINDING'):
            self.s.dispatch(directory)
        self.assertFalse(FakeChild.calls)
        self.assertFalse((output / 'execution.json').exists())

    def test_draining_service_refuses_new_tasks(self):
        directory, output = self.inbox()
        self.s.created_at -= 2800
        with self.assertRaisesRegex(ValueError, 'SERVICE_DRAINING'):
            self.s.dispatch(directory)
        self.assertFalse(FakeChild.calls)

    def test_scan_requires_commit_marker_and_limits_two_active_steps(self):
        first, _ = self.inbox('1')
        second, _ = self.inbox('2')
        third, _ = self.inbox('3')
        fourth, _ = self.inbox('4')
        (first / 'dispatch.json').unlink()
        with patch.object(self.s, 'ready', return_value=True):
            self.s.scan()
        self.assertEqual(len(FakeChild.calls), 2)
        self.assertNotIn(first.name, self.s.active)
        self.assertNotIn(fourth.name, self.s.active)

    def test_native_diagnostics_provides_step_when_workflow_has_no_progress(self):
        directory, output = self.inbox()
        self.s.dispatch(directory)
        task = self.s.active[directory.name]
        supervisor.atomic(task['task_work'] / 'native-diagnostics.json', {'workflow_step_id': '2'})
        self.s.refresh_step(task)
        self.assertEqual(task['receipt']['step_id'], '2')

    def test_cancel_targets_only_the_selected_step(self):
        first, _ = self.inbox('1')
        second, _ = self.inbox('2')
        self.s.dispatch(first)
        self.s.dispatch(second)
        first_task = self.s.active[first.name]
        first_task['receipt']['step_id'] = '2'
        supervisor.atomic(first / 'cancel.json', {})
        with patch.object(supervisor.subprocess, 'run') as command:
            command.return_value.returncode = 0
            command.return_value.stderr = ''
            self.s.poll_tasks()
        self.assertEqual(command.call_args[0][0], ['scancel', '--signal=TERM', '28001.2'])
        self.assertEqual(first_task['child'].terminated, [False])
        self.assertFalse(self.s.active[second.name]['child'].terminated)


if __name__ == '__main__':
    unittest.main()
