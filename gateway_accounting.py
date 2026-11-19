"""Read only project-registered jobs through the existing verified SSH identity."""
import argparse
import json
from pathlib import Path
import re
import time
import uuid

from orchestrator import atomic, SESSION
from service.accounting import FIELDS, parse_records
from trip.settings import gateway_address, tcloud_identity


def registered_jobs(root):
    """Submission ledgers establish scope; generated artifacts cannot grant it."""
    data = root / 'data'
    paths = list(data.glob('*.json')) + list((data / 'services').glob('*.json'))
    paths += list((data / 'submissions').glob('*.json'))
    registered = set()
    for path in paths:
        if path.is_symlink():
            continue
        try:
            value = json.loads(path.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            continue
        if not isinstance(value, dict):
            continue
        owned_service = value.get('owner') == SESSION and value.get('model_service_id')
        submitted = value.get('submission_state') == 'SUBMITTED'
        own_stage = False
        try:
            Path(value.get('staging', '')).resolve().relative_to((root / 'staging').resolve())
            own_stage = True
        except (ValueError, OSError):
            pass
        ledger = submitted and own_stage and (
            value.get('kind') in {'probe', 'environment'} or
            (path.parent == data / 'submissions' and value.get('run_id') and value.get('attempt_id')))
        if owned_service or ledger:
            for field in ('job_id', 'service_job_id'):
                job_id = str(value.get(field, ''))
                if re.fullmatch(r'[0-9]+', job_id):
                    registered.add(job_id)
    return registered


def read_channel(channel, timeout=30, max_bytes=2_000_000):
    """Drain both streams so a full stderr window cannot block stdout EOF."""
    deadline = time.monotonic() + timeout
    stdout, stderr = bytearray(), bytearray()
    while True:
        if time.monotonic() >= deadline:
            raise TimeoutError('Accounting SSH channel deadline exceeded')
        if channel.recv_ready():
            stdout.extend(channel.recv(32768))
        if channel.recv_stderr_ready():
            stderr.extend(channel.recv_stderr(32768))
        if len(stdout) + len(stderr) > max_bytes:
            raise ValueError('Accounting output exceeded bounded size')
        if channel.exit_status_ready() and not channel.recv_ready() and not channel.recv_stderr_ready():
            return stdout.decode('utf-8', errors='replace'), stderr.decode('utf-8', errors='replace'), channel.recv_exit_status()
        time.sleep(0.05)


def connect_gateway(identity=None):
    import paramiko
    identity = identity or tcloud_identity()
    host, port = gateway_address()
    client = paramiko.SSHClient()
    try:
        client.load_system_host_keys()
        client.set_missing_host_key_policy(paramiko.RejectPolicy())
        client.connect(host, port=port, username=identity.username,
                       key_filename=str(identity.key), look_for_keys=False, allow_agent=False,
                       timeout=12, banner_timeout=15, auth_timeout=15)
        return client
    except Exception:
        client.close()
        raise


def query(root, job_ids, record=True):
    result = {'queried_at': time.time(), 'job_ids': job_ids, 'returncode': 1,
              'source': 'read-only sacct on verified TACC SSH gateway', 'records': []}
    client = channel = None
    try:
        if not job_ids or not all(re.fullmatch(r'[0-9]+', job) for job in job_ids):
            raise ValueError('Expected numeric job IDs')
        if not set(job_ids).issubset(registered_jobs(root)):
            raise ValueError('Jobs are outside the project submission registry')
        identity = tcloud_identity()
        client = connect_gateway(identity)
        stdin, out, _ = client.exec_command(
            'sacct -n -P -j ' + ','.join(job_ids) + ' -o ' + FIELDS, timeout=20)
        stdin.close()
        channel = out.channel
        stdout, stderr, returncode = read_channel(channel)
        result.update(stdout=stdout, stderr=stderr, returncode=returncode)
        if returncode == 0:
            result['records'] = parse_records(stdout, job_ids, queried_at=result['queried_at'],
                                              expected_user=identity.username)
    except Exception as exc:
        # Do not include authentication exception text, config values or key paths.
        result.update(returncode=1, records=[], error_type=type(exc).__name__,
                      error='Accounting query failed; no terminal state inferred')
    finally:
        for resource in (channel, client):
            if resource is not None:
                try:
                    resource.close()
                except Exception:
                    pass  # Preserve the query result/error even if close fails.
    result['finished_at'] = time.time()
    path = None
    if record:
        path = root / 'data' / ('gateway-accounting-' + str(int(result['queried_at'])) + '-' + uuid.uuid4().hex[:8] + '.json')
        atomic(path, result)
    return path, result


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('job_ids', nargs='+')
    args = parser.parse_args()
    path, result = query(Path(__file__).resolve().parent, args.job_ids)
    print(json.dumps({'path': str(path), 'returncode': result['returncode'],
                      'records': result['records'], 'error_type': result.get('error_type')}))
    return result['returncode']


if __name__ == '__main__':
    raise SystemExit(main())
