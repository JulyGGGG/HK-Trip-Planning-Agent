"""Own one GPU allocation and dispatch bounded CPU Slurm steps inside it.

System Python 3.5+ only. Never submits jobs or cancels an allocation. A committed
inbox request is claimed durably before srun. An interrupted claim stays UNKNOWN
rather than replaying a possibly running task.
"""
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time


RUN_RE = re.compile(r'^run-[a-f0-9]{24}$')
ATTEMPT_RE = re.compile(r'^a[1-9][0-9]*$')


def atomic(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + '.tmp')
    with tmp.open('w', encoding='utf-8') as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(str(tmp), str(path))


def read_json(path):
    if path.is_symlink() or path.stat().st_size > 200000:
        raise ValueError('Invalid input file: ' + str(path))
    value = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(value, dict):
        raise ValueError('Expected JSON object')
    return value


class Child:
    def __init__(self, name, command, cwd, env, log_path, on_line=None):
        self.name = name
        self.log_path = log_path
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        self.stream = self.log_path.open('w', encoding='utf-8')
        self.on_line = on_line
        self.process = subprocess.Popen(command, cwd=str(cwd), env=env,
            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            start_new_session=True)
        self.thread = threading.Thread(target=self.pump)
        self.thread.daemon = True
        self.thread.start()

    def pump(self):
        try:
            for raw_line in self.process.stdout:
                line = raw_line.decode('utf-8', errors='replace')
                self.stream.write(line)
                self.stream.flush()
                sys.stdout.write('[' + self.name + '] ' + line)
                sys.stdout.flush()
                if self.on_line:
                    self.on_line(line)
        finally:
            self.process.stdout.close()
            self.stream.close()

    def terminate(self, force=False):
        if self.process.poll() is None:
            try:
                os.killpg(self.process.pid, signal.SIGKILL if force else signal.SIGTERM)
            except ProcessLookupError:
                pass

    def finish(self):
        self.thread.join(timeout=5)


class Supervisor:
    def __init__(self):
        self.work = Path(os.environ['TACC_WORKDIR']).resolve()
        self.user = Path(os.environ['TACC_USERDIR']).resolve()
        self.job = str(os.environ['SLURM_JOB_ID'])
        if not re.fullmatch(r'[0-9]+', self.job):
            raise ValueError('Invalid allocation ID')
        self.service_id = os.environ.get('MODEL_SERVICE_ID', self.work.name)
        session = os.environ.get('TACC_PROJECT_SESSION', 'hk-trip-planner')
        self.root = self.user / session
        self.run_root = self.user / os.environ.get('TACC_PROJECT_RUNS', session + '/runs')
        self.inbox = self.root / 'inbox'
        self.inbox.mkdir(parents=True, exist_ok=True)
        self.status_path = self.root / 'services' / (self.job + '.json')
        self.hostname = socket.gethostname()
        self.base_url = 'http://' + self.hostname + ':8000/v1'
        self.environment = self.user / 'agent-envs' / 'storm-e2e-v1' / 'ready.json'
        self.started = False
        self.model = None
        self.setup = None
        self.active = {}
        self.stopping = False
        self.failure = None
        self.created_at = time.time()
        help_result = subprocess.run(['srun', '--help'], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, universal_newlines=True, timeout=15)
        self.overlap = '--overlap' in help_result.stdout
        self.expected_requirements = hashlib.sha256(
            (self.work / 'workflow' / 'requirements-client.txt').read_bytes()).hexdigest()

    def model_line(self, line):
        if 'Application startup complete' in line:
            self.started = True
        if 'address already in use' in line.lower():
            self.failure = 'Model failed to bind its port; no foreign service will be used'

    def ready(self):
        if not self.model or self.model.process.poll() is not None or not self.started or self.failure:
            return False
        if not self.setup or self.setup.process.poll() != 0:
            return False
        try:
            report = read_json(self.environment)
            return (report.get('semantic_retrieval_passed') is True and
                report.get('storm_commit') == 'fb951af7744dab086e34962e9bc6fe878e145f83' and
                report.get('requirements_sha256') == self.expected_requirements)
        except (OSError, ValueError):
            return False

    def publish(self):
        atomic(self.status_path, {'allocation_job_id': self.job,
            'model_service_id': self.service_id, 'execution_mode': 'coallocated',
            'base_url': self.base_url, 'startup_reported': self.started,
            'environment_ready': self.ready(), 'failure': self.failure,
            'draining': time.time() - self.created_at >= 2700,
            'stopping': self.stopping, 'active_runs': sorted(self.active),
            'srun_overlap_supported': self.overlap, 'created_at': self.created_at,
            'updated_at': time.time(),
            'model_returncode': self.model.process.poll() if self.model else None,
            'setup_returncode': self.setup.process.poll() if self.setup else None})

    def launch(self):
        model_env = dict(os.environ, TACC_WORKDIR=str(self.work))
        self.model = Child('model', ['bash', str(self.work / 'model-service.sh')],
            self.work, model_env, self.work / 'model-service.log', self.model_line)
        setup_work = self.work / 'workflow'
        setup_env = dict(os.environ, TACC_WORKDIR=str(setup_work), CUDA_VISIBLE_DEVICES='')
        self.setup = Child('environment', ['bash', str(setup_work / 'setup_env.sh')],
            setup_work, setup_env, self.work / 'environment-setup.log')
        self.publish()

    def identify(self, directory):
        if directory.is_symlink() or directory.resolve().parent != self.inbox.resolve():
            raise ValueError('Inbox directory escapes session')
        request = read_json(directory / 'request.json')
        run_id, attempt = request.get('run_id', ''), request.get('attempt_id', '')
        if not isinstance(run_id, str) or not RUN_RE.fullmatch(run_id):
            raise ValueError('Invalid run ID')
        if not isinstance(attempt, str) or not ATTEMPT_RE.fullmatch(attempt):
            raise ValueError('Invalid attempt ID')
        if directory.name != run_id + '-' + attempt:
            raise ValueError('Inbox name does not match identity')
        output = self.run_root / run_id / attempt
        if self.run_root.resolve() not in output.resolve().parents:
            raise ValueError('Output directory escapes run root')
        output.mkdir(parents=True, exist_ok=True)
        return request, output

    def reject(self, directory, message):
        try:
            request, output = self.identify(directory)
            path = output / 'execution.json'
            if not path.exists():
                atomic(path, {'run_id': request['run_id'], 'attempt_id': request['attempt_id'],
                    'allocation_job_id': self.job, 'model_service_id': self.service_id,
                    'state': 'FAILED', 'step_id': None, 'returncode': None,
                    'finished_at': time.time(), 'error': message})
        except (OSError, ValueError):
            pass
        atomic(directory / 'rejected.json', {'allocation_job_id': self.job,
            'error': message, 'rejected_at': time.time()})

    def dispatch(self, directory):
        marker = read_json(directory / 'dispatch.json')
        if marker.get('service_job_id', self.job) != self.job:
            return  # A marker from a different allocation must be left to its owner.
        request, output = self.identify(directory)
        receipt_path = output / 'execution.json'
        if receipt_path.exists():
            # This includes interrupted STARTING receipts: never replay blindly.
            return
        if time.time() - self.created_at >= 2700:
            raise ValueError('SERVICE_DRAINING: insufficient allocation lifetime for a new task')
        config = read_json(directory / 'model-config.json')
        if (str(config.get('service_job_id')) != self.job or
            config.get('model_service_id') != self.service_id or
            str(config.get('base_url', '')).rstrip('/') != self.base_url or
            config.get('model') != 'qwen3.8-27b'):
            raise ValueError('STALE_SERVICE_BINDING: allocation, instance or address differs')
        for field in ('run_id', 'attempt_id'):
            if marker.get(field, request[field]) != request[field]:
                raise ValueError('Dispatch marker does not match its request')
        receipt = {'run_id': request['run_id'], 'attempt_id': request['attempt_id'],
            'allocation_job_id': self.job, 'model_service_id': self.service_id,
            'state': 'STARTING', 'step_id': None, 'returncode': None,
            'started_at': time.time(), 'execution_mode': 'coallocated'}
        if (directory / 'cancel.json').exists():
            receipt.update(state='CANCELLED', finished_at=time.time(),
                cancellation_stage='before_start')
            atomic(receipt_path, receipt)
            return
        atomic(receipt_path, receipt)  # Durable claim before the process side effect.
        task_work = self.work / 'tasks' / directory.name
        try:
            shutil.copytree(str(self.work / 'workflow'), str(task_work),
                ignore=shutil.ignore_patterns('storm-src', '__pycache__', '*.log', 'native-diagnostics.json'))
            shutil.copyfile(str(directory / 'request.json'), str(task_work / 'request.json'))
            shutil.copyfile(str(directory / 'model-config.json'), str(task_work / 'model-config.json'))
            command = ['srun', '--jobid=' + self.job, '--ntasks=1', '--cpus-per-task=2',
                '--gres=none', '--immediate=20']
            if self.overlap:
                command.append('--overlap')
            command.extend(['bash', str(task_work / 'job.sh')])
            env = dict(os.environ, TACC_WORKDIR=str(task_work), CUDA_VISIBLE_DEVICES='')
            child = Child(directory.name, command, task_work, env, output / 'execution.log')
            receipt.update(state='RUNNING', srun_pid=child.process.pid)
            atomic(receipt_path, receipt)
            self.active[directory.name] = {'child': child, 'receipt': receipt,
                'path': receipt_path, 'output': output, 'inbox': directory,
                'task_work': task_work, 'cancel_requested_at': None}
        except Exception as exc:
            receipt.update(state='FAILED', error=type(exc).__name__ + ': ' + str(exc),
                finished_at=time.time())
            atomic(receipt_path, receipt)

    def refresh_step(self, task):
        paths = [task['output'] / name for name in ('progress.json', 'result.json')]
        paths.append(task['task_work'] / 'native-diagnostics.json')
        for path in paths:
            if not path.exists():
                continue
            try:
                data = read_json(path)
                step = str(data.get('workflow_step_id', data.get('step_id', '')))
                if re.fullmatch(r'[0-9]+', step):
                    task['receipt']['step_id'] = step
                    return
            except (OSError, ValueError):
                pass

    def poll_tasks(self):
        for key, task in list(self.active.items()):
            self.refresh_step(task)
            code = task['child'].process.poll()
            if code is not None:
                task['child'].finish()
                task['receipt'].update(returncode=code, finished_at=time.time(),
                    state='CANCELLED' if task['cancel_requested_at'] else ('COMPLETED' if code == 0 else 'FAILED'))
                atomic(task['path'], task['receipt'])
                del self.active[key]
                continue
            if self.stopping or (task['inbox'] / 'cancel.json').exists():
                if not task['cancel_requested_at']:
                    task['cancel_requested_at'] = time.time()
                    task['receipt'].update(state='CANCELLING', cancel_requested_at=time.time())
                    task['child'].terminate()
                    step = task['receipt'].get('step_id')
                    if step and re.fullmatch(r'[0-9]+', step):
                        try:
                            cancel = subprocess.run(['scancel', '--signal=TERM', self.job + '.' + step],
                                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                universal_newlines=True, timeout=10)
                            task['receipt']['step_cancel'] = {'target': self.job + '.' + step,
                                'returncode': cancel.returncode, 'stderr': cancel.stderr[:1000]}
                        except (OSError, subprocess.TimeoutExpired) as exc:
                            task['receipt']['step_cancel_error'] = str(exc)
                elif time.time() - task['cancel_requested_at'] > 15:
                    # Kill only this srun process group; it forwards to its own step.
                    task['child'].terminate(force=True)
            atomic(task['path'], task['receipt'])

    def scan(self):
        if not self.ready() or self.stopping:
            return
        for directory in sorted(self.inbox.iterdir()):
            if len(self.active) >= 2:
                break
            if not directory.is_dir() or directory.is_symlink() or directory.name in self.active:
                continue
            if not (directory / 'dispatch.json').exists() or (directory / 'rejected.json').exists():
                continue
            try:
                self.dispatch(directory)
            except (OSError, ValueError) as exc:
                self.reject(directory, type(exc).__name__ + ': ' + str(exc))

    def shutdown(self):
        self.stopping = True
        # Stop task steps first. Model/environment termination never sends a
        # whole-allocation scancel, so it cannot affect another user's jobs.
        deadline = time.time() + 20
        while self.active and time.time() < deadline:
            self.poll_tasks()
            time.sleep(0.5)
        for task in self.active.values():
            task['child'].terminate(force=True)
            task['receipt'].update(state='UNKNOWN', error='Supervisor stopped before task termination confirmed')
            atomic(task['path'], task['receipt'])
        for child in (self.setup, self.model):
            if child:
                child.terminate()
        deadline = time.time() + 10
        while time.time() < deadline and any(c and c.process.poll() is None for c in (self.setup, self.model)):
            time.sleep(0.2)
        for child in (self.setup, self.model):
            if child:
                child.terminate(force=True)
                child.finish()
        self.publish()

    def run(self):
        def stop(signum, frame):
            self.stopping = True
        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            self.launch()
            while not self.stopping:
                if self.model.process.poll() is not None:
                    self.failure = 'Model service exited with code ' + str(self.model.process.returncode)
                    self.stopping = True
                if self.setup.process.poll() not in (None, 0):
                    self.failure = 'Environment preparation exited with code ' + str(self.setup.process.returncode)
                    self.stopping = True
                if self.failure:
                    self.stopping = True
                self.poll_tasks()
                self.scan()
                self.publish()
                time.sleep(2)
        finally:
            self.shutdown()
        return 1 if self.failure else 0


if __name__ == '__main__':
    raise SystemExit(Supervisor().run())
