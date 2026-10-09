"""Long-lived lifecycle workers inside the common process supervisor."""
import json
import select
import subprocess
import time
from pathlib import Path
from .runtime import ProcessSession, SystemSample, schedule, FOOTPRINT_LIMIT

class CatalogSession(ProcessSession):
    def __init__(self, state, profile='quiet', monitor=None, *, protocol=None, layout=None, read_only=False, policy=None, lock_directory=None):
        super().__init__(state, profile, policy=policy, monitor=monitor, layout=layout, read_only=read_only, lock_directory=lock_directory)
        self.protocol = protocol or {}
        self._backend_checks = {}
        self._lifecycle_workers = {}

    def environment(self, extra=None):
        environment = super().environment(extra)
        environment['PBGATE_SESSION'] = '1'
        return environment

    @property
    def fixture_logs(self):
        return self.logs

    def prepare_backend(self, cwd, commands):
        # Register all selected members before boot, but do not allocate a worker
        # or a fixture when every stage has a valid receipt.
        if self.profile != 'quiet':
            return
        names = [arg[8:] for command in commands if self.lifecycle_command(command)
                 for arg in command if arg.startswith('--check=')]
        if names:
            self._backend_checks[str(Path(cwd).resolve())] = list(dict.fromkeys(names))

    def lifecycle_command(self, command):
        # Catalog-wide diagnostic runs own one local pool. Release leaves keep
        # explicit checks so the session can register every route before boot.
        return bool(self.protocol) and command[1:1+len(self.protocol['runner'])] == self.protocol['runner'] and '--shared' not in command

    def worker_reply(self, worker, request, timeout=None):
        worker.stdin.write(json.dumps(request) + '\n')
        worker.stdin.flush()
        started = time.monotonic()
        deadline = started + timeout if timeout is not None else None
        admitted = 0
        while True:
            self.check_resources(started)
            remaining = max(0, deadline - time.monotonic()) if deadline is not None else None
            if remaining == 0:
                raise subprocess.TimeoutExpired(worker.args, timeout)
            ready, _, _ = select.select([worker.stdout], [], [], min(.25, remaining) if remaining is not None else .25)
            if not ready:
                continue
            line = worker.stdout.readline()
            if not line:
                raise RuntimeError('Lifecycle worker exited before completing its command')
            response = json.loads(line)
            if response.get('operation') != 'admit':
                return dict(response, admission_seconds=admitted) if admitted else response
            # A lifecycle stage contains several child scenarios. Reuse the
            # supervisor's condition-based admission before each fresh child,
            # with no timer running in that child and no pause of active checks.
            waited = self.monitor.admit()
            admitted += waited
            if deadline is not None:
                deadline += waited
            worker.stdin.write(json.dumps({'operation':'admitted'}) + '\n')
            worker.stdin.flush()

    def run_lifecycles(self, command, cwd, log, env, timeout, waited):
        start = time.monotonic()
        identity = str(Path(cwd).resolve())
        child = self._lifecycle_workers.get(identity)
        created = child is None
        if child is None:
            error_log = self.fixture_logs / 'lifecycle-worker.log'
            error_log.parent.mkdir(parents=True, exist_ok=True)
            with error_log.open('ab') as errors:
                child = self._processes.spawn(schedule([command[0], *self.protocol['worker']], self.profile),
                                         log=self.fixture_logs/'process-owner.log',
                                         cwd=cwd, env=self.environment(), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                         stderr=errors, text=True, bufsize=1)
            self._lifecycle_workers[identity] = child
        sampler = SystemSample()
        before = 0 if created else sampler.resources([child.pid])['groups'][child.pid]['cpu_seconds']
        try:
            response = self.worker_reply(child, dict(operation='run', args=command[1+len(self.protocol['runner']):], checks=self._backend_checks[identity],
                                                    log=str(log), env=env), timeout)
        except BaseException:
            self.stop(child)
            child.stdin.close()
            child.stdout.close()
            del self._lifecycle_workers[identity]
            raise
        resources = sampler.resources([child.pid])['groups'][child.pid]
        after = resources['cpu_seconds']
        internal_wait = response.get('admission_seconds', 0)
        metrics = dict(self.monitor.summary(start, child.pid), wall_seconds=round(time.monotonic()-start-internal_wait,4),
                       cpu_seconds=round(max(0,after-before),4), admission_seconds=round(waited+internal_wait,4), fixture_scope='verification_session')
        metrics['peak_physical_bytes'] = max(metrics['peak_physical_bytes'], resources['physical_bytes'])
        if max(self.monitor.summary(start)['peak_physical_bytes'], resources['physical_bytes']) > self.policy.get('maxMemoryBytes', FOOTPRINT_LIMIT):
            raise RuntimeError('Verification exceeded the 6 GiB physical memory target; see '+str(log))
        return response['code'], metrics

    def close_lifecycle_workers(self):
        for child in self._lifecycle_workers.values():
            try:
                if not self.cancelled.is_set() and child.poll() is None:
                    self.worker_reply(child, {'operation':'close'}, timeout=10)
                    # The acknowledgement means fixtures are stopped. Close the
                    # input pipe too: Bun keeps a referenced stdin reader alive.
                    child.stdin.close()
                    child.wait(timeout=5)
            finally:
                self.stop(child)
                child.stdin.close()
                child.stdout.close()
        self._lifecycle_workers.clear()

    def run(self, command, cwd, log, env=None, timeout=None, admission=True):
        timeout = self.policy.get('checkTimeoutSeconds', 120) if timeout is None else timeout
        log = Path(log)
        log.parent.mkdir(parents=True, exist_ok=True)
        environment = self.environment(env)
        metrics_name = self.protocol.get('metrics_env', 'PBGATE_REQUEST_METRICS')
        environment[metrics_name] = str(log.with_suffix('.requests.jsonl'))
        if self.lifecycle_command(command) and str(Path(cwd).resolve()) in self._backend_checks:
            waited = self.monitor.admit() if admission and not self.read_only else 0
            return self.run_lifecycles(command, cwd, log, environment, timeout, waited)
        return super().run(command, cwd, log, environment, timeout=timeout, admission=admission and not self.read_only)

    def __exit__(self, *error):
        try:
            self.close_lifecycle_workers()
        finally:
            super().__exit__(*error)
