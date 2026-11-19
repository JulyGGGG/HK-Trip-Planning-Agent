"""Local regression tests for independent Slurm accounting."""
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

E2E = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(E2E))
import gateway_accounting as gateway
from orchestrator import SESSION, atomic, load


class GatewayCase(unittest.TestCase):
    def setUp(self):
        parent = E2E / 'evidence' / 'test-tmp'
        parent.mkdir(parents=True, exist_ok=True)
        temporary = tempfile.TemporaryDirectory(dir=parent)
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        atomic(self.root / 'data' / 'service.json', {
            'owner': SESSION, 'model_service_id': 'qwen-test', 'service_job_id': '9001',
        })

    def test_registry_accepts_ledgers_but_not_arbitrary_artifacts(self):
        atomic(self.root / 'data' / 'probe-fake.json', {'probe_job_id': '9999'})
        atomic(self.root / 'data' / 'artifacts' / 'diagnostics.json', {'owner': SESSION,
               'model_service_id': 'fake', 'service_job_id': '9998'})
        atomic(self.root / 'data' / 'probe-submit.json', {'job_id': '9002', 'kind': 'probe',
               'submission_state': 'SUBMITTED', 'staging': str(self.root / 'staging' / 'probe')})
        self.assertEqual(gateway.registered_jobs(self.root), {'9001', '9002'})

    def test_unknown_scope_query_is_recorded_without_opening_ssh(self):
        path, result = gateway.query(self.root, ['9999'])
        self.assertEqual(result['returncode'], 1)
        self.assertEqual(result['records'], [])
        self.assertEqual(result['error_type'], 'ValueError')
        self.assertEqual(load(path), result)

    def test_ssh_error_is_sanitized_and_client_closed_with_host_verification(self):
        home = self.root / 'home'
        key = home / 'private-fixture-key'
        atomic(home / '.tcloud' / '.userconfig', {'username': 'test-student', 'authfile': str(key)})
        module = Mock()
        client = module.SSHClient.return_value
        client.connect.side_effect = RuntimeError('DO NOT PUBLISH credential details')
        with patch.dict(sys.modules, {'paramiko': module}), patch.object(gateway.Path, 'home', return_value=home):
            path, result = gateway.query(self.root, ['9001'])
        client.load_system_host_keys.assert_called_once_with()
        client.set_missing_host_key_policy.assert_called_once_with(module.RejectPolicy.return_value)
        self.assertEqual(client.connect.call_args.kwargs['username'], 'test-student')
        client.close.assert_called_once_with()
        self.assertNotIn('DO NOT PUBLISH', path.read_text())
        self.assertNotIn(key.name, path.read_text())
        self.assertEqual(result['error_type'], 'RuntimeError')

class ChannelCase(unittest.TestCase):
    def test_channel_drains_both_streams_and_bounds_output_and_time(self):
        channel = Mock()
        channel.recv_ready.side_effect = [True, False]
        channel.recv_stderr_ready.side_effect = [True, False]
        channel.recv.return_value = b'rows'
        channel.recv_stderr.return_value = b'warning'
        channel.exit_status_ready.return_value = True
        channel.recv_exit_status.return_value = 0
        self.assertEqual(gateway.read_channel(channel), ('rows', 'warning', 0))
        with patch.object(gateway.time, 'monotonic', side_effect=[0, 31]):
            with self.assertRaises(TimeoutError):
                gateway.read_channel(Mock())
        channel = Mock()
        channel.recv.return_value = b'x' * 20
        channel.recv_stderr.return_value = b''
        with self.assertRaises(ValueError):
            gateway.read_channel(channel, max_bytes=10)


if __name__ == '__main__':
    unittest.main()
