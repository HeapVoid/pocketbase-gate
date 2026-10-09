"""Disposable PocketBase fixtures. The pool owns startup, reset and shutdown."""
import json
import os
from pathlib import Path
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request


class PocketBaseFixture:
    def __init__(self, project, session, name):
        self.project, self.session, self.name = project, session, name
        self.definition = project.fixtures[name]
        self.directory = session.directory / ('fixture-' + secrets.token_hex(8))
        self.directory.mkdir()
        self.owner = secrets.token_hex(24)
        self.password = secrets.token_urlsafe(24)
        self.child = None
        self.output = None
        self.token = None
        self.used = 0

    def request(self, path, body=None, auth=True, timeout=5):
        headers = {'Content-Type': 'application/json'}
        if auth and self.token:
            headers['Authorization'] = self.token
        request = urllib.request.Request(self.url + path,
            data=json.dumps(body).encode() if body is not None else None, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return json.load(response)
        except urllib.error.HTTPError as error:
            payload = json.loads(error.read())
            raise RuntimeError('PocketBase ' + str(error.code) + ' on ' + path + ': ' + payload.get('message', error.reason)) from error

    def context(self, check):
        path = self.directory / ('context-' + check + '.json')
        descriptor = {'check': check, 'url': self.url, 'adminToken': self.token,
                      'adminEmail': 'admin@pbgate.test', 'adminPassword': self.password,
                      'dataDirectory': str(self.directory / 'data')}
        with os.fdopen(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600), 'w') as output:
            json.dump(descriptor, output)
        return {'PBGATE_CONTEXT': str(path)}

    def start(self, shared=False, deadline=None):
        started = time.monotonic()
        definition, session = self.definition, self.session
        binary = str((self.project.root / definition['binary']).resolve())
        if not Path(binary).is_file():
            raise ValueError('PocketBase binary is missing: ' + binary)
        for field in ('hooks', 'migrations'):
            target = self.directory / field
            if definition.get(field):
                source = self.project.root / definition[field]
                if not source.is_dir():
                    raise ValueError('Fixture ' + field + ' directory is missing: ' + str(source))
                shutil.copytree(source, target)
            else:
                target.mkdir()
        if shared:
            shutil.copy2(Path(__file__).with_name('fixture-control.js'), self.directory / 'hooks/pbgate.pb.js')
        self.environment = dict(definition.get('env', {}), PBGATE_FIXTURE_OWNER=self.owner,
            PBGATE_NATIVE_STORE_KEYS=json.dumps(definition.get('nativeStoreKeys', [])))
        if session.profile == 'quiet':
            self.environment['GOMAXPROCS'] = '2'
        paths = ['--dir', str(self.directory / 'data'), '--hooksDir', str(self.directory / 'hooks'),
                 '--migrationsDir', str(self.directory / 'migrations')]
        startup_limit = definition.get('startupTimeoutSeconds', 60)
        deadline = min(deadline or started + startup_limit, started + startup_limit)
        def remaining():
            value = deadline - time.monotonic()
            if value <= 0:
                raise subprocess.TimeoutExpired(['PocketBase fixture', self.name], startup_limit)
            return value
        preparation_log = session.logs / ('fixture-' + self.name + '-prepare.log')
        for args in (['migrate', 'up'], ['superuser', 'upsert', 'admin@pbgate.test', self.password]):
            code, _ = session.run([binary, *args, *paths], self.project.root, preparation_log,
                self.environment, remaining(), admission=False)
            if code:
                raise RuntimeError('PocketBase preparation failed; see ' + str(preparation_log))
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.url = 'http://127.0.0.1:' + str(port)
        self.output = (session.logs / ('fixture-' + self.name + '-' + self.owner[:8] + '.log')).open('ab')
        self.child = session.spawn([binary, 'serve', *paths, '--hooksWatch=false', '--http', '127.0.0.1:' + str(port),
            '--hooksPool=' + str(definition.get('hooksPool', 8))], self.project.root, self.output, self.environment)
        while True:
            remaining()
            session.check_resources(started)
            if self.child.poll() is not None:
                raise RuntimeError('PocketBase exited during startup; see ' + str(self.output.name))
            try:
                self.request('/api/health', auth=False, timeout=min(5, remaining()))
                break
            except (urllib.error.URLError, TimeoutError):
                if time.monotonic() >= deadline:
                    raise subprocess.TimeoutExpired(['PocketBase fixture', self.name], startup_limit)
                session.cancelled.wait(.05)
        self.token = self.request('/api/collections/_superusers/auth-with-password',
            {'identity':'admin@pbgate.test', 'password':self.password}, auth=False, timeout=min(5, remaining()))['token']
        if definition.get('seed'):
            code, _ = session.run(definition['seed'], self.project.root, preparation_log,
                dict(self.environment, **self.context('seed')), remaining(), admission=False)
            if code:
                raise RuntimeError('Fixture seed failed; see ' + str(preparation_log))
        if shared:
            if (self.directory / 'data/storage').exists():
                shutil.copytree(self.directory / 'data/storage', self.directory / 'storage-baseline')
            self.request('/__pbgate__', {'owner':self.owner, 'operation':'checkpoint'}, timeout=min(5, remaining()))
        return time.monotonic() - started

    def reset(self, timeout=5):
        start = time.monotonic()
        self.request('/__pbgate__', {'owner':self.owner, 'operation':'reset'}, timeout=timeout)
        # Shared scenarios keep schema and cron configuration stable. Uploads
        # created by a scenario are discarded after the exact DB restoration.
        shutil.rmtree(self.directory / 'data/storage', ignore_errors=True)
        if (self.directory / 'storage-baseline').exists():
            shutil.copytree(self.directory / 'storage-baseline', self.directory / 'data/storage')
        self.used += 1
        return time.monotonic() - start

    def close(self):
        try:
            if self.child:
                self.session.stop(self.child)
                self.child = None
        finally:
            if self.output:
                self.output.close()
                self.output = None
            shutil.rmtree(self.directory, ignore_errors=True)


class PocketBasePool:
    def __init__(self, project, session):
        self.project, self.session = project, session
        self.instances = {}

    def inputs(self, name, observations=None):
        fixture, content = self.project.fixtures[name], self.session.content
        paths = [*fixture.get('inputs', []), *(fixture[key] for key in ('hooks', 'migrations') if fixture.get(key))]
        paths.extend(arg for arg in fixture.get('seed', []) if not Path(arg).is_absolute() and (self.project.root / arg).is_file())
        result = content.inventory(self.project.root, paths, observations)
        result['binary'] = content.file((self.project.root / fixture['binary']).resolve(), observations)
        return result

    def run(self, check, log):
        spec, session = check.definition, self.session
        waited = session.monitor.admit()
        start = time.monotonic()
        deadline = start + spec['timeoutSeconds']
        shared = spec['isolation'] == 'shared'
        fixture = self.instances.get(check.fixture) if shared else None
        reused = fixture is not None
        if not fixture:
            fixture = PocketBaseFixture(self.project, session, check.fixture)
            if shared:
                self.instances[check.fixture] = fixture
        startup = reset = 0
        try:
            if not reused:
                startup = fixture.start(shared, deadline)
            env = dict(spec.get('env', {}), **fixture.context(check.id))
            remaining = spec['timeoutSeconds'] - (time.monotonic() - start)
            if remaining <= 0:
                raise subprocess.TimeoutExpired(spec['command'], spec['timeoutSeconds'])
            code, metrics = session.run(spec['command'], self.project.root, log, env, remaining, admission=False)
            if shared:
                reset = fixture.reset(timeout=min(5, max(.01, deadline - time.monotonic())))
            metrics.update(wall_seconds=round(time.monotonic()-start, 4), admission_seconds=round(waited, 4),
                           fixture={'name':check.fixture, 'reused':reused, 'startupSeconds':round(startup, 4), 'resetSeconds':round(reset, 4)})
            return code, metrics
        except BaseException:
            if shared:
                self.instances.pop(check.fixture, None)
            fixture.close()
            raise
        finally:
            if not shared:
                fixture.close()

    def close(self):
        failures = []
        for fixture in self.instances.values():
            try:
                fixture.close()
            except Exception as error:
                failures.append(str(error))
        self.instances.clear()
        if failures:
            raise RuntimeError('Fixture shutdown failed: ' + '; '.join(failures))
