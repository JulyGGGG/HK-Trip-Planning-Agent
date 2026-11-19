"""Single durable worker on the Linux controller.

Submission intent is persisted before invoking the CLI. Interrupted submissions
remain UNKNOWN and are never automatically resubmitted.
"""
from __future__ import annotations

import json
import hashlib
import os
from pathlib import Path
import re
import shutil
import signal
import subprocess
import time
import uuid

from app import TERMINAL
from trip.settings import tcloud_identity

ROOT = Path(__file__).resolve().parent
SESSION = os.environ.get('TACC_PROJECT_SESSION', 'hk-trip-planner')
RUN_ROOT = os.environ.get('TACC_PROJECT_RUNS', f'{SESSION}/runs')
STATES = {'PD': 'PENDING', 'R': 'RUNNING', 'CG': 'COMPLETING', 'CF': 'CONFIGURING'}
SCHED_TERMINAL = {'COMPLETED', 'CANCELLED', 'FAILED', 'TIMEOUT', 'OUT_OF_MEMORY',
                  'NODE_FAIL', 'PREEMPTED', 'BOOT_FAIL', 'DEADLINE'}


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    os.replace(str(tmp), str(path))


def load(path, default=None):
    return json.loads(path.read_text(encoding='utf-8')) if path.exists() else default


class Worker:
    @property
    def tacc_user(self):
        return tcloud_identity().username

    def __init__(self, store):
        self.store = store
        self.data = store.data_dir
        self.service_path = self.data / 'service.json'
        self.env_path = self.data / 'environment.json'
        self.accounting = {}
        self.last_active = time.time()
        self.last_probe = 0

    def command(self, args, timeout=60):
        argv = [os.getenv('TCLOUD_BIN', 'tcloud')] + list(args)
        if os.name != 'posix':
            p = subprocess.run(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                               universal_newlines=True, timeout=timeout)
            output = p.stdout
        else:
            # tcloud starts rsync/ssh children. Killing only tcloud leaves those
            # children holding stdout open, so communicate() can otherwise hang.
            p = subprocess.Popen(argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 universal_newlines=True, start_new_session=True)
            try:
                output, _ = p.communicate(timeout=timeout)
            except subprocess.TimeoutExpired as exc:
                try:
                    os.killpg(p.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                try:
                    output, _ = p.communicate(timeout=5)
                    exc.output = output
                except subprocess.TimeoutExpired:
                    # Bound cleanup even if an unexpected child escaped its group.
                    p.stdout.close()
                    try:
                        p.wait(timeout=5)
                    except subprocess.TimeoutExpired:
                        pass
                raise
        output = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', output).replace('\r', '').replace('\x00', '')
        return p.returncode, output

    def read_json(self, path, timeout=None):
        options = {'timeout': timeout} if timeout is not None else {}
        rc, output = self.command(['cat', path], **options)
        if rc or '[Tcloud Error]' in output:
            return None
        try:
            result = json.loads(output)
            return result if isinstance(result, dict) else None
        except ValueError:
            return None

    def queue(self, job_id):
        if not re.fullmatch(r'[0-9]+', str(job_id)):
            raise ValueError('invalid registered job ID')
        rc, output = self.command(['ps', '-j', str(job_id)])
        for line in output.splitlines():
            cols = line.split()
            if len(cols) >= 8 and cols[0] == str(job_id):
                return STATES.get(cols[4], cols[4])
        # squeue failure / absent is NOT a terminal success.
        return 'UNKNOWN'

    def submit(self, stage, record, record_path):
        record.update(submission_state='SUBMITTING', submitted_at=time.time(), staging=str(stage))
        atomic(record_path, record)
        try:
            rc, output = self.command(['submit', str(stage)], timeout=600)
        except Exception as exc:
            record.update(submission_state='UNKNOWN', error=type(exc).__name__ + ': ' + str(exc))
            atomic(record_path, record)
            return record
        record_path.with_suffix('.submit.log').write_text(output, encoding='utf-8')
        match = re.search(r'Submitted batch job ([0-9]+)', output)
        if match:
            record.update(job_id=match.group(1), submission_state='SUBMITTED', cli_returncode=rc)
        else:
            record.update(submission_state='UNKNOWN', cli_returncode=rc,
                          error='No definitive submitted job ID; reconcile before retry')
        atomic(record_path, record)
        return record

    def ensure_service(self):
        service = load(self.service_path)
        if service:
            if service.get('readiness') not in {'STOPPED', 'UNAVAILABLE'} or service.get('scheduler_state') not in SCHED_TERMINAL:
                return service
            atomic(self.data / 'services' / (service['model_service_id'] + '.json'), service)
        service_id = 'qwen-e2e-' + uuid.uuid4().hex[:12]
        stage = ROOT / 'staging' / service_id
        shutil.copytree(str(ROOT / 'service'), str(stage))
        conf = stage / 'tuxiv.conf'
        conf.write_text(conf.read_text().replace('qwen-public-serve', service_id))
        service = {'model_service_id': service_id, 'owner': SESSION, 'readiness': 'SUBMITTING',
                   'created_at': time.time(), 'model': 'qwen3.8-27b', 'log_prefix': service_id}
        self.submit(stage, service, self.service_path)
        if service.get('job_id'):
            service.update(service_job_id=service['job_id'], readiness='STARTING')
        else:
            service['readiness'] = 'UNKNOWN'
        atomic(self.service_path, service)
        return service

    def poll_service(self, service):
        job_id = service.get('service_job_id')
        if not job_id:
            return service
        account = self.accounting.get(job_id, {})
        fresh = time.time() - account.get('observed_at', 0) < 45
        scheduler = account.get('state') if fresh or account.get('state') in SCHED_TERMINAL else None
        scheduler = scheduler or self.queue(job_id)
        service['scheduler_state'] = scheduler
        if scheduler in SCHED_TERMINAL:
            service['readiness'] = 'STOPPED' if scheduler == 'CANCELLED' else 'UNAVAILABLE'
        elif scheduler == 'UNKNOWN':
            if service.get('readiness') != 'DRAINING':
                service['readiness'] = 'UNKNOWN'
        elif scheduler == 'RUNNING' and service.get('readiness') in {'READY', 'STARTUP_REPORTED', 'DRAINING'}:
            pass  # Startup evidence was already captured; do not reread a growing log every tick.
        else:
            rc, log = self.command(['cat', 'slurm_log/%s-%s.log' % (service['log_prefix'], job_id)])
            (self.data / ('service-%s.log' % job_id)).write_text(log, encoding='utf-8')
            marker = re.search(r'SERVICE_BASE_URL=(http://[a-zA-Z0-9.-]+:8000/v1)', log)
            own_marker = 'SERVICE_JOB_ID=' + job_id
            started = marker and own_marker in log and 'Application startup complete' in log
            if started and scheduler == 'RUNNING':
                service['base_url'] = marker.group(1)
                if service.get('readiness') not in {'READY', 'DRAINING', 'STOPPED', 'UNAVAILABLE'}:
                    service['readiness'] = 'STARTUP_REPORTED'
            if 'address already in use' in log.lower():
                service.update(readiness='UNAVAILABLE', error='own service failed to bind port')
        atomic(self.service_path, service)
        return service

    def refresh_accounting(self):
        self.accounting = {}
        terminal_path = self.data / 'terminal-accounting.json'
        expected_user = self.tacc_user

        def merge(record, terminal_only=False):
            if not isinstance(record, dict) or record.get('user') != expected_user:
                return
            key, state = record.get('job_id'), record.get('state')
            observed = record.get('observed_at')
            if not isinstance(key, str) or not re.fullmatch(r'[0-9]+(?:\.(?:[0-9]+|batch|extern))?', key):
                return
            if type(observed) not in (int, float) or observed <= 0:
                return
            if terminal_only and state not in SCHED_TERMINAL:
                return
            previous = self.accounting.get(key)
            if previous:
                if previous.get('state') in SCHED_TERMINAL and state not in SCHED_TERMINAL:
                    return  # A stopped watcher cannot revive a confirmed ended allocation.
                if previous.get('observed_at', 0) >= observed:
                    return
            self.accounting[key] = dict(record)

        cached = load(terminal_path, {})
        for record in cached.get('records', []):
            merge(record, terminal_only=True)
        data = self.read_json(SESSION + '/accounting.json')
        if not data:
            return
        atomic(self.data / 'accounting.json', data)
        for record in data.get('records', []):
            if isinstance(record, dict):
                merge(dict(record, observed_at=record.get('observed_at') or data.get('queried_at')))
        for line in data.get('stdout', '').splitlines():
            cols = line.split('|')
            if len(cols) < 9 or not cols[0].isdigit() or cols[1] != expected_user:
                continue
            state_parts = cols[3].split()
            merge({'job_id': cols[0], 'user': cols[1],
                'state': state_parts[0].rstrip('+') if state_parts else 'UNKNOWN', 'exit_code': cols[4],
                'elapsed_seconds': cols[5], 'allocated_cpus': cols[6], 'allocated_tres': cols[7],
                'max_rss': cols[8] or None, 'observed_at': data.get('queried_at')})
        atomic(terminal_path, {'schema_version': 1, 'records': [
            record for record in self.accounting.values() if record.get('state') in SCHED_TERMINAL]})

    def environment_ready(self):
        ready = self.read_json('agent-envs/storm-e2e-v1/ready.json')
        expected_hash = hashlib.sha256((ROOT / 'workflow' / 'requirements-client.txt').read_bytes()).hexdigest()
        if ready and ready.get('semantic_retrieval_passed') is True and ready.get('storm_commit') == 'fb951af7744dab086e34962e9bc6fe878e145f83' and ready.get('requirements_sha256') == expected_hash:
            atomic(self.data / 'environment-ready.json', ready)
            return True
        env = load(self.env_path)
        if not env:
            name = 'storm-setup-' + uuid.uuid4().hex[:12]
            stage = ROOT / 'staging' / name
            shutil.copytree(str(ROOT / 'workflow'), str(stage), ignore=shutil.ignore_patterns('__pycache__'))
            conf = (stage / 'tuxiv-setup.conf').read_text().replace('storm-env-setup', name)
            (stage / 'tuxiv.conf').write_text(conf)
            self.submit(stage, {'kind': 'environment', 'name': name}, self.env_path)
        return False

    def update(self, run, **fields):
        return self.store.update_run(run['run_id'], expected_attempt_id=run['attempt_id'], **fields)

    def bind_submit(self, run, service):
        # Refuse a service snapshot that was replaced, stopped, or lacks startup evidence.
        latest = load(self.service_path, {})
        if latest.get('model_service_id') != service['model_service_id'] or latest.get('readiness') not in {'STARTUP_REPORTED', 'READY'}:
            return
        name = run['run_id'] + '-' + run['attempt_id']
        stage = ROOT / 'staging' / name
        shutil.copytree(str(ROOT / 'workflow'), str(stage), ignore=shutil.ignore_patterns('storm-src', '__pycache__'))
        atomic(stage / 'request.json', dict(run['request'], run_id=run['run_id'], attempt_id=run['attempt_id']))
        config = {k: service[k] for k in ('model_service_id', 'service_job_id', 'base_url', 'model')}
        config['chat_template_kwargs'] = {'enable_thinking': False}
        atomic(stage / 'model-config.json', config)
        conf = stage / 'tuxiv.conf'
        conf.write_text(conf.read_text().replace('storm-e2e-workflow', name))
        self.update(run, status='SUBMITTING', model_service=service,
                    submission={'staging': str(stage), 'state': 'SUBMITTING'})
        ledger = self.data / 'submissions' / (name + '.json')
        result = self.submit(stage, {'run_id': run['run_id'], 'attempt_id': run['attempt_id']}, ledger)
        if result.get('job_id'):
            self.update(run, status='QUEUED', scheduler={'workflow_job_id': result['job_id'], 'state': 'PENDING'}, submission=result)
        else:
            self.update(run, status='UNKNOWN', submission=result, error=result.get('error'))

    def collect(self, run, remote, manifest):
        if manifest.get('run_id') != run['run_id'] or manifest.get('attempt_id') != run['attempt_id']:
            raise ValueError('manifest identity mismatch')
        dest = self.store.artifact_dir(run['run_id'], run['attempt_id'])
        dest.mkdir(parents=True, exist_ok=True)
        entries = manifest.get('artifacts', [])
        if not isinstance(entries, list):
            raise ValueError('bad artifacts')
        for item in entries:
            name = item.get('path', '')
            if not re.fullmatch(r'[a-zA-Z0-9_.-]+\.(?:json|jsonl|md|txt|log)', name):
                raise ValueError('unsafe artifact name')
            rc, output = self.command(['cat', remote + '/' + name])
            if rc or '[Tcloud Error]' in output:
                raise ValueError('artifact not yet readable: ' + name)
            if item.get('sha256') and hashlib.sha256(output.encode('utf-8')).hexdigest() != item['sha256']:
                raise ValueError('artifact hash mismatch: ' + name)
            (dest / name).write_text(output, encoding='utf-8')
        atomic(dest / 'result.json', manifest)  # publish completion manifest last

    def monitor_run(self, run, service):
        binding = run.get('model_service', {})
        if binding.get('model_service_id') and binding.get('model_service_id') != service.get('model_service_id'):
            service = load(self.data / 'services' / (binding['model_service_id'] + '.json'), binding)
            bound_account = self.accounting.get(binding.get('service_job_id'), {})
            bound_state = bound_account.get('state') if bound_account.get('state') in SCHED_TERMINAL else self.queue(binding['service_job_id'])
            if bound_state in SCHED_TERMINAL:
                service = dict(service, readiness='UNAVAILABLE', scheduler_state=bound_state)
        job_id = run.get('scheduler', {}).get('workflow_job_id')
        if not job_id:
            # Recover a completed submit whose DB update was interrupted.
            record = load(self.data / 'submissions' / (run['run_id'] + '-' + run['attempt_id'] + '.json'), {})
            if record.get('job_id'):
                job_id = record['job_id']
            elif run['status'] == 'SUBMITTING':
                self.update(run, status='UNKNOWN', error='Interrupted submission requires reconciliation')
                return
            else:
                return
        account = self.accounting.get(job_id, {})
        fresh = time.time() - account.get('observed_at', 0) < 45
        state = account.get('state') if fresh or account.get('state') in SCHED_TERMINAL else None
        state = state or self.queue(job_id)
        scheduler = dict(account, workflow_job_id=job_id, state=state)
        if run.get('cancel_requested'):
            if state in SCHED_TERMINAL:
                self.update(run, status='CANCELLED', scheduler=scheduler, metrics={'scheduler': account})
            else:
                if not run.get('cancel_sent'):
                    rc, output = self.command(['cancel', '-j', job_id])
                    self.update(run, status='CANCELLING', scheduler=scheduler, cancel_sent={'at': time.time(), 'rc': rc, 'output': output})
            return
        remote = RUN_ROOT + '/' + run['run_id'] + '/' + run['attempt_id']
        progress = self.read_json(remote + '/progress.json')
        fields = {'scheduler': scheduler}
        if progress and progress.get('run_id') == run['run_id'] and progress.get('attempt_id') == run['attempt_id']:
            fields['workflow'] = progress
            if progress.get('model_ready') and service.get('service_job_id') == run.get('model_service', {}).get('service_job_id'):
                service.update(readiness='READY', checked_at=time.time())
                atomic(self.service_path, service)
        fields['model_service'] = dict(run.get('model_service', {}), **{
            k: service.get(k) for k in ('readiness', 'scheduler_state')}) if service.get('model_service_id') == run.get('model_service', {}).get('model_service_id') else run.get('model_service', {})
        manifest = self.read_json(remote + '/result.json')
        if manifest:
            self.collect(run, remote, manifest)
            fields.update(result=manifest, metrics=manifest.get('metrics', {}), status='FINALIZING')
            if state in SCHED_TERMINAL:
                if state == 'COMPLETED' and account.get('exit_code') == '0:0' and manifest.get('workflow_state') == 'COMPLETED':
                    fields['status'] = 'SUCCEEDED'
                else:
                    fields.update(status='FAILED', error=manifest.get('error') or manifest.get('failure_reason') or 'Scheduler/application failure')
            elif state == 'UNKNOWN':
                fields['status'] = 'UNKNOWN'
        elif state in SCHED_TERMINAL:
            fields.update(status='FAILED', error='Job ended without validated result manifest')
        elif state == 'RUNNING':
            fields['status'] = 'RUNNING'
        # Service ended while CPU job queued/running: cancel the old attempt first.
        bound = run.get('model_service', {}).get('service_job_id')
        if bound and bound == service.get('service_job_id') and service.get('readiness') in {'STOPPED', 'UNAVAILABLE'} and state not in SCHED_TERMINAL:
            fields.update(cancel_requested=True, error='Bound model service ended; cancel old attempt before retry')
        self.update(run, **fields)

    def stop_service(self, service):
        if service.get('owner') != SESSION or not service.get('service_job_id'):
            return
        if self.store.list_pending_runs():
            raise RuntimeError('Refusing cleanup while runs may still use the service')
        service['readiness'] = 'DRAINING'
        atomic(self.service_path, service)
        rc, output = self.command(['cancel', '-j', service['service_job_id']])
        service.update(cancel_requested_at=time.time(), cancel_returncode=rc, cancel_output=output)
        atomic(self.service_path, service)

    def tick(self):
        runs = self.store.list_pending_runs()
        if not runs:
            service = load(self.service_path)
            if service and service.get('service_job_id'):
                self.refresh_accounting()
                service = self.poll_service(service)
            if service and service.get('readiness') in {'READY', 'STARTUP_REPORTED'} and time.time() - self.last_active > 300:
                self.stop_service(service)
            return
        self.last_active = time.time()
        self.refresh_accounting()
        service = load(self.service_path)
        service = self.poll_service(service) if service else {}
        # Only runs not yet submitted may request a new model instance.
        want_new = any(not r.get('scheduler', {}).get('workflow_job_id') and r['status'] not in {'SUBMITTING', 'UNKNOWN'} and not r.get('cancel_requested') for r in runs)
        if want_new:
            previous_instance = service.get('model_service_id')
            service = self.ensure_service()
            if service.get('model_service_id') != previous_instance:
                service = self.poll_service(service)
        env_ready = self.environment_ready() if want_new else False
        for run in sorted(runs, key=lambda r: r['created_at']):
            if run.get('scheduler', {}).get('workflow_job_id') or run['status'] in {'SUBMITTING', 'UNKNOWN'}:
                try:
                    self.monitor_run(run, service)
                except Exception as exc:
                    # A slow or unreadable artifact must not starve later runs.
                    # Preserve the current state for reconciliation next tick.
                    with (self.data / 'worker-errors.log').open('a', encoding='utf-8') as log:
                        log.write('%s run=%s attempt=%s %s: %s\n' % (
                            time.time(), run['run_id'], run['attempt_id'],
                            type(exc).__name__, exc))
                continue
            if run.get('cancel_requested'):
                self.update(run, status='CANCELLED')
                continue
            if service.get('readiness') in {'STARTUP_REPORTED', 'READY'} and env_ready:
                self.bind_submit(run, service)
            else:
                self.update(run, status='WAITING_MODEL', model_service=service,
                            workflow={'phase': 'WAITING', 'message': '等待模型启动与独立客户端环境验证'})
