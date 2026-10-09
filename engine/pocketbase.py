"""Disposable PocketBase fixtures. The pool owns startup, reset and shutdown."""
import json
import os
from pathlib import Path
import platform
import secrets
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from dependencies import digest


class PocketBaseFixture:
    def __init__(self, project, session, name, dependencies=None):
        self.project, self.session, self.name = project, session, name
        self.definition = project.fixtures[name]
        self.dependencies = dependencies
        self.directory = session.directory / ('fixture-' + secrets.token_hex(8))
        self.directory.mkdir()
        self.owner = secrets.token_hex(24)
        self.password = secrets.token_urlsafe(24)
        self.child = None
        self.output = None
        self.token = None
        self.used = 0

    def baseline_key(self):
        content, root, definition = self.session.content, self.project.root, self.definition
        hooks = definition.get('hooks')
        environment = self.session.environment(self.environment)
        ignored = {'TMPDIR','PWD','OLDPWD','SHLVL','_','PBGATE_FIXTURE_OWNER','PBGATE_CONTEXT','PBGATE_PROCESS_OWNER'}
        inputs = {'format':1, 'phase':definition.get('baseline','initial'),
            'binary':content.file(root / definition['binary']),
            'migrations':content.inventory(root, [definition['migrations']]) if definition.get('migrations') else {},
            'hooks':self.dependencies.preparation(hooks) if hooks and self.dependencies else content.inventory(root,[hooks]) if hooks else {},
            'declared':content.inventory(root,definition.get('inputs', [])),
            'environment':{key:digest(value) for key,value in environment.items() if key not in ignored},
            'calendar':datetime.now(timezone.utc).date().isoformat(),
            'platform':[platform.system(),platform.release(),platform.machine()],
            'analyzer':content.file(Path(__file__).resolve().parents[1] / 'src/dependencies.js'),
            'driver':content.inventory(Path(__file__).parent,['*.py','*.js'])}
        return 'fixture-' + digest(inputs)

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
        for name in ('npm_lifecycle_event','npm_lifecycle_script'):
            self.environment[name] = ''
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
        baseline = definition.get('baseline','initial')
        key = self.baseline_key() if baseline else None
        metadata = session.receipts.restore_directory(key, self.directory / 'data') if key else None
        self.baseline = {'hit':bool(metadata), 'phase':baseline, 'key':key}
        if metadata:
            self.password = metadata['password']
        else:
            for args in (['migrate', 'up', *paths], ['superuser', 'upsert', *paths, '--', 'admin@pbgate.test', self.password]):
                code, _ = session.run([binary, *args], self.project.root, preparation_log,
                    self.environment, remaining(), admission=False)
                if code:
                    raise RuntimeError('PocketBase preparation failed; see ' + str(preparation_log))
            if baseline == 'initial':
                self.publish_baseline(key)
        with socket.socket() as listener:
            listener.bind(('127.0.0.1', 0))
            port = listener.getsockname()[1]
        self.url = 'http://127.0.0.1:' + str(port)
        self.output = (session.logs / ('fixture-' + self.name + '-' + self.owner[:8] + '.log')).open('ab')
        def serve():
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
                    session.cancelled.wait(.05)
            self.token = self.request('/api/collections/_superusers/auth-with-password',
                {'identity':'admin@pbgate.test', 'password':self.password}, auth=False, timeout=min(5, remaining()))['token']
        serve()
        if baseline == 'schema' and not metadata:
            session.stop(self.child)
            self.child = None
            self.publish_baseline(key)
            serve()
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

    def publish_baseline(self, key):
        if self.baseline_key() != key:
            raise RuntimeError('Fixture preparation inputs changed; baseline was not saved')
        self.session.receipts.save_directory(key, self.directory / 'data', {'password':self.password},
            self.directory / ('baseline-' + secrets.token_hex(8)))

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
    def __init__(self, project, session, dependencies=None):
        self.project, self.session, self.dependencies = project, session, dependencies
        self.instances = {}

    def inputs(self, name, observations=None, scope=None):
        fixture, content = self.project.fixtures[name], self.session.content
        paths = [*fixture.get('inputs', []), *(fixture[key] for key in ('hooks', 'migrations') if fixture.get(key))]
        if scope and scope['scope'] == 'precise':
            paths = [path for path in paths if not any(path == directory or path.startswith(directory + '/') for directory in (scope['sources'],scope['outdir']))]
        paths.extend(self.project.command_files(fixture.get('seed', [])))
        result = content.inventory(self.project.root, paths, observations)
        result['binary'] = content.file((self.project.root / fixture['binary']).resolve(), observations)
        result['calendar'] = datetime.now(timezone.utc).date().isoformat()
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
            fixture = PocketBaseFixture(self.project, session, check.fixture, self.dependencies)
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
                           fixture={'name':check.fixture, 'reused':reused, 'baseline':fixture.baseline, 'startupSeconds':round(startup, 4), 'resetSeconds':round(reset, 4)})
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
