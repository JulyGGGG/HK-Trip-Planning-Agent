"""Native Slurm accounting for registered application allocations and steps.

Python 3.5 compatible. An independent later probe must establish the service
allocation's own final state: this watcher stops with that allocation.
"""
import json
import getpass
import os
from pathlib import Path
import re
import subprocess
import time


JOB_ID = re.compile(r'^[0-9]+$')
RECORD_ID = re.compile(r'^([0-9]+)(?:\.([0-9]+|batch|extern))?$')
FIELDS = 'JobIDRaw,User,JobName,State,ExitCode,ElapsedRaw,AllocCPUS,AllocTRES,MaxRSS'


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(temporary), str(path))


def command(args, timeout=15):
    started = time.time()
    try:
        process = subprocess.run(args, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                 universal_newlines=True, timeout=timeout)
        return {'queried_at': started, 'returncode': process.returncode,
                'stdout': process.stdout, 'stderr': process.stderr}
    except Exception as exc:
        return {'queried_at': started, 'returncode': None, 'stdout': '', 'stderr': '',
                'error': type(exc).__name__ + ': ' + str(exc)}


def parse_records(stdout, registered_ids, own_job_id=None, queried_at=None,
                  expected_user=None):
    """Infer blank step users only from verified parent or current allocation.

    Two passes make the inference independent of sacct row order. An explicit
    foreign parent takes precedence over the current-allocation hint.
    """
    if not expected_user:
        expected_user = getpass.getuser()
    registered = set(str(value) for value in registered_ids if JOB_ID.match(str(value)))
    rows, owners, foreign = [], {}, set()
    for line in stdout.splitlines():
        columns = line.split('|')
        if len(columns) < 9:
            continue
        match = RECORD_ID.match(columns[0])
        if not match or match.group(1) not in registered:
            continue
        allocation_id, step_id = match.groups()
        rows.append((columns, allocation_id, step_id))
        if step_id is None and columns[1]:
            if columns[1] == expected_user:
                owners[allocation_id] = 'allocation_user'
            else:
                foreign.add(allocation_id)
    if str(own_job_id) in registered and str(own_job_id) not in foreign:
        owners.setdefault(str(own_job_id), 'current_slurm_allocation')
    records = []
    for columns, allocation_id, step_id in rows:
        reported_user = columns[1]
        if allocation_id in foreign or (reported_user and reported_user != expected_user):
            continue
        if reported_user == expected_user:
            ownership_source = 'reported_user'
        elif allocation_id in owners:
            ownership_source = owners[allocation_id]
        else:
            continue
        state_parts = columns[3].split()
        state = state_parts[0].rstrip('+') if state_parts else 'UNKNOWN'
        records.append({
            'job_id': columns[0], 'allocation_job_id': allocation_id,
            'step_id': step_id, 'user': expected_user, 'reported_user': reported_user or None,
            'job_name': columns[2], 'state': state, 'reported_state': columns[3],
            'exit_code': columns[4] or None, 'elapsed_seconds': columns[5] or None,
            'allocated_cpus': columns[6] or None, 'allocated_tres': columns[7] or None,
            'max_rss': columns[8] or None, 'ownership_source': ownership_source,
            'observed_at': queried_at,
        })
    return records


def registered_jobs(directory, own_job_id):
    ids = set([own_job_id])
    for path in directory.glob('*.json'):
        if not JOB_ID.match(path.stem):
            continue
        try:
            registration = json.loads(path.read_text(encoding='utf-8'))
            if isinstance(registration, dict) and str(registration.get('job_id')) == path.stem:
                ids.add(path.stem)
        except (ValueError, OSError):
            # An incompletely published registration is retried next poll.
            continue
    return sorted(ids, key=int)


def collect_metadata(own_job_id):
    help_result = command(['srun', '--help'])
    return {
        'schema_version': 1, 'recorded_at': time.time(), 'allocation_job_id': own_job_id,
        'partition': command(['scontrol', 'show', 'partition', 'tacc']),
        'node_state_summary': command(['sinfo', '-h', '-p', 'tacc', '-o', '%P|%a|%l|%D|%t']),
        'srun': {
            'help_returncode': help_result.get('returncode'),
            'help_error': help_result.get('error'),
            'overlap_supported': '--overlap' in help_result.get('stdout', ''),
            'exclusive_supported': '--exclusive' in help_result.get('stdout', ''),
            'immediate_supported': '--immediate' in help_result.get('stdout', ''),
            'version': command(['srun', '--version']),
        },
    }


def main():
    own = os.environ['SLURM_JOB_ID']
    expected_user = os.environ.get('TACC_ACCOUNT_USER') or getpass.getuser()
    if not JOB_ID.match(own):
        raise ValueError('Invalid current Slurm allocation ID')
    root = Path(os.environ['TACC_USERDIR']) / os.environ.get('TACC_PROJECT_SESSION', 'hk-trip-planner')
    jobs = root / 'jobs'
    jobs.mkdir(parents=True, exist_ok=True)
    atomic_json(jobs / (own + '.json'), {'job_id': own, 'kind': 'service'})
    metadata_name = 'accounting-metadata-' + own + '.json'
    atomic_json(root / metadata_name, collect_metadata(own))
    while True:
        ids = registered_jobs(jobs, own)
        result = command(['sacct', '-n', '-P', '-j', ','.join(ids), '-o', FIELDS])
        result.update(schema_version=2, job_ids=ids, watcher_allocation_job_id=own,
                      metadata_path=metadata_name)
        result['records'] = parse_records(result['stdout'], ids, own, result['queried_at'],
                                          expected_user) if result['returncode'] == 0 else []
        # Raw stdout remains unchanged for the existing consumer and auditing.
        atomic_json(root / 'accounting.json', result)
        time.sleep(10)


if __name__ == '__main__':
    main()
