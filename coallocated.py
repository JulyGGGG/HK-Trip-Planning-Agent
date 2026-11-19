"""One allocation: Qwen service plus separately accounted CPU Slurm steps.

Inputs are staged first and dispatch.json is uploaded last. An uncertain commit
is reconciled from execution.json; it is never blindly resent. Cancelling a run
only publishes its cancel marker, never scancels the shared allocation.
"""
import hashlib
import shutil
import time
import uuid

from orchestrator import ROOT, RUN_ROOT, SESSION, SCHED_TERMINAL, Worker, atomic, load


class CoallocatedWorker(Worker):
    @property
    def staging_root(self):
        return ROOT / 'staging'

    def stage_service(self, stage, service_id):
        shutil.copytree(str(ROOT / 'combined'), str(stage), ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copytree(str(ROOT / 'workflow'), str(stage / 'workflow'), ignore=shutil.ignore_patterns('__pycache__'))
        shutil.copyfile(str(ROOT / 'service' / 'job.sh'), str(stage / 'model-service.sh'))
        shutil.copyfile(str(ROOT / 'service' / 'accounting.py'), str(stage / 'accounting.py'))
        conf = stage / 'tuxiv.conf'
        conf.write_text(conf.read_text().replace('qwen-combined-serve', service_id))

    def stage_request(self, run):
        return dict(run['request'], run_id=run['run_id'], attempt_id=run['attempt_id'])

    def ensure_service(self):
        service = load(self.service_path)
        if service:
            if service.get('submission_state') != 'NOT_SUBMITTED':
                if service.get('failure'):
                    return service
                if service.get('readiness') not in {'STOPPED', 'UNAVAILABLE'} or service.get('scheduler_state') not in SCHED_TERMINAL:
                    return service
            atomic(self.data / 'services' / (service['model_service_id'] + '.json'), service)
        service_id = 'qwen-combined-' + uuid.uuid4().hex[:12]
        stage = self.staging_root / service_id
        self.stage_service(stage, service_id)
        atomic(stage / 'service-identity.json', {'model_service_id': service_id, 'owner': SESSION})
        service = {'model_service_id': service_id, 'owner': SESSION, 'readiness': 'SUBMITTING',
                   'created_at': time.time(), 'model': 'qwen3.8-27b', 'log_prefix': service_id,
                   'execution_mode': 'coallocated'}
        self.submit(stage, service, self.service_path)
        if service.get('job_id'):
            service.update(service_job_id=service['job_id'], readiness='STARTING')
        else:
            service['readiness'] = 'UNKNOWN'
        atomic(self.service_path, service)
        return service

    def poll_service(self, service):
        service = super().poll_service(service)
        job_id = service.get('service_job_id')
        if not job_id:
            return service
        snapshot = self.read_json(SESSION + '/services/' + job_id + '.json')
        if snapshot and snapshot.get('model_service_id') == service.get('model_service_id'):
            service['supervisor'] = snapshot
            if service.get('scheduler_state') in SCHED_TERMINAL:
                pass  # Final allocation accounting outranks its last supervisor snapshot.
            elif snapshot.get('failure'):
                service.update(failure=snapshot['failure'], readiness='UNAVAILABLE')
            elif snapshot.get('draining') or snapshot.get('stopping'):
                service['readiness'] = 'DRAINING'
            atomic(self.service_path, service)
        return service

    def tick(self):
        super().tick()
        service = load(self.service_path, {})
        if service.get('failure'):
            for run in self.store.list_pending_runs():
                if not run.get('scheduler', {}).get('workflow_job_id'):
                    self.update(run, status='FAILED', error=service['failure'], model_service=service)

    def environment_ready(self):
        ready = self.read_json('agent-envs/storm-e2e-v1/ready.json')
        expected = hashlib.sha256((ROOT / 'workflow' / 'requirements-client.txt').read_bytes()).hexdigest()
        if ready and ready.get('semantic_retrieval_passed') is True and ready.get('storm_commit') == 'fb951af7744dab086e34962e9bc6fe878e145f83' and ready.get('requirements_sha256') == expected:
            atomic(self.data / 'environment-ready.json', ready)
            return True
        return False  # The supervisor prepares the environment inside its allocation.

    def bind_submit(self, run, service):
        latest = load(self.service_path, {})
        if latest.get('model_service_id') != service['model_service_id'] or latest.get('readiness') not in {'READY', 'STARTUP_REPORTED'}:
            return
        name = run['run_id'] + '-' + run['attempt_id']
        stage = self.staging_root / name
        stage.mkdir(parents=True, exist_ok=True)
        if (stage / 'dispatch.json').exists():
            self.update(run, status='UNKNOWN', error='Existing dispatch marker requires reconciliation')
            return
        atomic(stage / 'request.json', self.stage_request(run))
        config = {k: service[k] for k in ('model_service_id', 'service_job_id', 'base_url', 'model')}
        config['chat_template_kwargs'] = {'enable_thinking': False}
        atomic(stage / 'model-config.json', config)
        record = {'run_id': run['run_id'], 'attempt_id': run['attempt_id'], 'state': 'UPLOADING',
                  'execution_mode': 'coallocated', 'job_id': service['service_job_id'],
                  'staging': str(stage), 'started_at': time.time()}
        ledger = self.data / 'submissions' / (name + '.json')
        atomic(ledger, record)
        scheduler = {'workflow_job_id': service['service_job_id'], 'workflow_step_id': None,
                     'execution_kind': 'slurm_step', 'state': 'PENDING'}
        self.update(run, status='SUBMITTING', model_service=service, scheduler=scheduler, submission=record)
        try:
            rc, output = self.command(['upload', str(stage), SESSION + '/inbox/'], timeout=180)
            if rc or '[Tcloud Error]' in output:
                raise RuntimeError('Input upload failed: ' + output[-1000:])
            atomic(stage / 'dispatch.json', {'run_id': run['run_id'], 'attempt_id': run['attempt_id'],
                   'service_job_id': service['service_job_id'], 'committed_at': time.time()})
            record['state'] = 'COMMITTING'
            atomic(ledger, record)
            rc, output = self.command(['upload', str(stage / 'dispatch.json'), SESSION + '/inbox/' + name + '/'], timeout=180)
            if rc or '[Tcloud Error]' in output:
                raise RuntimeError('Dispatch commit uncertain: ' + output[-1000:])
            record.update(state='COMMITTED', committed_at=time.time())
            atomic(ledger, record)
            self.update(run, status='QUEUED', submission=record)
        except Exception as exc:
            record.update(state='UNKNOWN', error=str(exc))
            atomic(ledger, record)
            self.update(run, status='UNKNOWN', error=str(exc), submission=record)

    def monitor_run(self, run, service):
        binding = run.get('model_service', {})
        allocation = binding.get('service_job_id')
        if not allocation:
            self.update(run, status='UNKNOWN', error='Missing immutable allocation binding')
            return
        remote = RUN_ROOT + '/' + run['run_id'] + '/' + run['attempt_id']
        receipt = self.read_json(remote + '/execution.json')
        valid_receipt = receipt and receipt.get('run_id') == run['run_id'] and receipt.get('attempt_id') == run['attempt_id'] and str(receipt.get('allocation_job_id')) == allocation
        if receipt and not valid_receipt:
            self.update(run, status='UNKNOWN', error='Execution receipt identity mismatch')
            return
        if run.get('cancel_requested') and not run.get('cancel_sent'):
            name = run['run_id'] + '-' + run['attempt_id']
            marker = self.data / 'cancellations' / name / 'cancel.json'
            atomic(marker, {'run_id': run['run_id'], 'attempt_id': run['attempt_id'], 'requested_at': time.time()})
            rc, out = self.command(['upload', str(marker), SESSION + '/inbox/' + name + '/'], timeout=90)
            if rc or '[Tcloud Error]' in out:
                self.update(run, status='CANCELLING', error='Cancellation marker upload failed; retrying')
                return
            self.update(run, status='CANCELLING', cancel_sent={'at': time.time(), 'scope': 'only_this_cpu_step'})
        progress = self.read_json(remote + '/progress.json')
        fields = {}
        if progress and (progress.get('run_id') != run['run_id'] or progress.get('attempt_id') != run['attempt_id']):
            progress = None
        if progress and progress.get('run_id') == run['run_id'] and progress.get('attempt_id') == run['attempt_id']:
            fields['workflow'] = progress
            if progress.get('model_ready') and service.get('service_job_id') == allocation:
                if service.get('readiness') in {'READY', 'STARTUP_REPORTED'}:
                    service.update(readiness='READY', checked_at=time.time())
                    atomic(self.service_path, service)
        bound_service = service if service.get('service_job_id') == allocation else load(self.data / 'services' / (binding['model_service_id'] + '.json'), binding)
        fields['model_service'] = bound_service
        step_id = str((receipt or {}).get('step_id') or (progress or {}).get('workflow_step_id') or '')
        key = allocation + '.' + step_id if step_id.isdigit() else None
        account = self.accounting.get(key, {}) if key else {}
        scheduler = dict(account, workflow_job_id=allocation, workflow_step_id=step_id or None,
                         execution_kind='slurm_step', state=account.get('state') or (receipt or {}).get('state') or 'UNKNOWN')
        fields['scheduler'] = scheduler
        if receipt:
            dest = self.store.artifact_dir(run['run_id'], run['attempt_id'])
            atomic(dest / 'execution.json', receipt)
            fields['execution'] = receipt
        manifest = self.read_json(remote + '/result.json')
        if manifest:
            if str(manifest.get('workflow_job_id')) != allocation or (key and str(manifest.get('workflow_step_id')) != step_id):
                self.update(run, status='UNKNOWN', error='Result manifest allocation/step identity mismatch')
                return
            self.collect(run, remote, manifest)
            fields.update(result=manifest, metrics=dict(manifest.get('metrics', {})), status='FINALIZING')
        if receipt and receipt.get('state') in {'COMPLETED', 'FAILED', 'CANCELLED'}:
            if not key and receipt.get('state') in {'FAILED', 'CANCELLED'}:
                # Failed before any CPU step was created; supervisor confirms no process survives.
                fields.update(status=receipt['state'], error=receipt.get('error') or 'Execution ended before creating a step')
            elif account.get('state') in SCHED_TERMINAL:
                if receipt.get('state') == 'CANCELLED':
                    fields['status'] = 'CANCELLED'
                elif receipt.get('state') == 'COMPLETED' and receipt.get('returncode') == 0 and manifest and manifest.get('workflow_state') == 'COMPLETED' and account.get('state') == 'COMPLETED' and account.get('exit_code') == '0:0':
                    fields.update(status='SUCCEEDED', error=None)
                else:
                    fields.update(status='FAILED', error=(manifest or {}).get('failure_reason') or receipt.get('error') or 'CPU step failed')
            else:
                fields.update(status='UNKNOWN', error='CPU execution ended; awaiting independent Slurm step accounting')
        elif receipt and receipt.get('state') == 'RUNNING' and not run.get('cancel_requested'):
            fields['status'] = 'RUNNING'
        allocation_account = self.accounting.get(allocation, {})
        if allocation_account.get('state') in SCHED_TERMINAL and fields.get('status') not in {'SUCCEEDED', 'FAILED', 'CANCELLED'}:
            fields.update(status='FAILED', error='Bound allocation ended before validated workflow completion; retry creates a new attempt')
        self.update(run, **fields)
