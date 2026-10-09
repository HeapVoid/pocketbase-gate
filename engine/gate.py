#!/usr/bin/env python3
"""Declarative gate planning and execution. Adapters own stack preparation."""
import argparse
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import errno
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import time

from runtime import Content, ProcessSession, ReceiptCache, PROFILES, atomic_json
from dependencies import DependencyGraph

INTENTS = {'dev', 'release', 'diagnostic'}
DEFAULT_INPUTS = ['src', 'test', 'tests', 'scripts', 'package.json', 'bun.lock',
                  'bunfig.toml', 'types.d.ts', 'jsconfig.json', '.env', '.env.local', '.env.test']
OPERATIONAL_ENV = {'PWD', 'OLDPWD', 'SHLVL', '_', 'TMPDIR', 'PBGATE_CONTEXT', 'PBGATE_PROCESS_OWNER'}


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def relative(name, label, output=False):
    if not isinstance(name, str) or not name or Path(name).is_absolute() or '..' in Path(name).parts:
        raise ValueError(label + ' must be a project-relative path: ' + str(name))
    if output and (name in ('.', '*') or any(character in name for character in '*?[')):
        raise ValueError('Outputs must be explicit paths below the project root')
    if name == '.':
        raise ValueError('Declare input directories explicitly; the project root includes gate state')
    return name


def strings(value, label):
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ValueError(label + ' must be a list of nonempty strings')
    return list(dict.fromkeys(value))


@dataclass
class Check:
    id: str
    definition: dict
    default_inputs: bool = False

    @property
    def requires(self):
        return self.definition['requires']

    @property
    def outputs(self):
        return self.definition['outputs']

    @property
    def fixture(self):
        return self.definition.get('fixture')

    @property
    def writes(self):
        return self.definition['exclusive'] + ['output:' + path for path in self.outputs] + (
            ['fixture:' + self.fixture] if self.fixture and self.definition.get('isolation') == 'shared' else [])

    @property
    def reads(self):
        return ['output:' + path for path in self.definition['inputs'] + self.definition['tests']]


class Project:
    def __init__(self, root, document, config=None):
        self.root = Path(root).resolve()
        self.config = Path(config).resolve() if config else None
        self.document = document
        allowed = {'format', 'state', 'resources', 'inputSets', 'fixtures', 'checks', 'required', 'testInventory', 'tools'}
        if set(document) - allowed or document.get('format') != 1:
            raise ValueError('Use config format 1 and documented fields; unknown fields: ' + ', '.join(sorted(set(document) - allowed)))
        self.state = self.root / relative(document.get('state', '.pbgate'), 'state', output=True)
        if self.state.is_symlink() or not self.state.resolve().is_relative_to(self.root):
            raise ValueError('Gate state must belong to the project')
        self.policy = document.get('resources', {})
        rules = {'minIdlePercent': (0, 100), 'maxSwapBytesPerSecond': (0, 2**60),
                 'admissionTimeoutSeconds': (.01, 86400), 'maxMemoryBytes': (1, 2**60), 'cacheBytes': (1, 2**60)}
        for name, value in self.policy.items():
            if name not in rules or isinstance(value, bool) or not isinstance(value, (int, float)) or not rules[name][0] <= value <= rules[name][1]:
                raise ValueError('Invalid resource setting: ' + name)
        self.fixtures = document.get('fixtures', {})
        for name, fixture in self.fixtures.items():
            allowed_fixture = {'binary', 'hooks', 'migrations', 'inputs', 'env', 'seed', 'nativeStoreKeys', 'startupTimeoutSeconds', 'hooksPool', 'baseline'}
            if set(fixture) - allowed_fixture or not isinstance(fixture.get('binary'), str):
                raise ValueError('Invalid PocketBase fixture: ' + name)
            for key in ('hooks', 'migrations'):
                if fixture.get(key):
                    relative(fixture[key], key)
            for path in strings(fixture.get('inputs', []), 'fixture inputs'):
                relative(path, 'fixture input')
            if fixture.get('seed') is not None:
                strings(fixture['seed'], 'fixture seed command')
                if not fixture['seed']:
                    raise ValueError('Fixture seed command cannot be empty')
            strings(fixture.get('nativeStoreKeys', []), 'native store keys')
            if not .01 <= fixture.get('startupTimeoutSeconds', 60) <= 86400 or not 1 <= fixture.get('hooksPool', 8) <= 64:
                raise ValueError('Invalid fixture limits: ' + name)
            self.validate_env(fixture.get('env', {}))
            if fixture.get('baseline', 'initial') not in (False, 'initial', 'schema'):
                raise ValueError('Fixture baseline is initial, schema or false')
        self.checks = {}
        check_fields = {'id', 'command', 'inputs', 'inputSets', 'outputs', 'requires', 'intents', 'domains',
                        'kind', 'tests', 'fixture', 'isolation', 'exclusive', 'timeoutSeconds', 'cache', 'restore', 'env', 'dependencies'}
        for original in document.get('checks', []):
            check = dict(original)
            name = check.get('id', '')
            if not isinstance(name, str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]*', name) or name in self.checks:
                raise ValueError('Checks need unique stable IDs: ' + str(name))
            if set(check) - check_fields:
                raise ValueError('Unknown check fields: ' + ', '.join(sorted(set(check) - check_fields)))
            check['command'] = strings(check.get('command'), name + ' command')
            # Repeated argv entries are meaningful (e.g. --file a --file b).
            check['command'] = list(original['command'])
            if not check['command']:
                raise ValueError('A check command cannot be empty')
            for field, default in [('requires', []), ('outputs', []), ('tests', []), ('exclusive', []),
                                   ('intents', ['dev', 'release']), ('domains', []), ('inputSets', [])]:
                check[field] = strings(check.get(field, default), name + ' ' + field)
            if not check['intents'] or set(check['intents']) - INTENTS:
                raise ValueError('Every check needs explicit supported intents')
            check['inputs'] = strings(check.get('inputs', DEFAULT_INPUTS), name + ' inputs')
            for group in check['inputSets']:
                if group not in document.get('inputSets', {}):
                    raise ValueError('Unknown input set: ' + group)
                check['inputs'].extend(strings(document['inputSets'][group], 'input set'))
            check['inputs'] = list(dict.fromkeys(check['inputs']))
            for field in ('inputs', 'outputs', 'tests'):
                for path in check[field]:
                    relative(path, field, output=field == 'outputs')
                    if (self.root / path).resolve().is_relative_to(self.state.resolve()):
                        raise ValueError('Gate state cannot be a check input or output')
            for output in check['outputs']:
                if not (self.root / output).resolve().is_relative_to(self.root):
                    raise ValueError('Outputs cannot escape the project through symlinks')
            if check.get('fixture') not in (None, *self.fixtures):
                raise ValueError('Unknown fixture: ' + str(check['fixture']))
            if check.get('isolation', 'fresh') not in ('fresh', 'shared'):
                raise ValueError('Fixture isolation is fresh or shared')
            check['isolation'] = check.get('isolation', 'fresh')
            if check.get('fixture'):
                fixture = self.fixtures[check['fixture']]
                check['inputs'] = list(dict.fromkeys([*check['inputs'], *fixture.get('inputs', []),
                    *(fixture[field] for field in ('hooks', 'migrations') if fixture.get(field))]))
            check['kind'] = check.get('kind', 'test')
            if check['kind'] not in ('test', 'prepare'):
                raise ValueError('Check kind is test or prepare')
            check['timeoutSeconds'] = check.get('timeoutSeconds', 120)
            if not isinstance(check['timeoutSeconds'], (int, float)) or isinstance(check['timeoutSeconds'], bool) or not .01 <= check['timeoutSeconds'] <= 86400:
                raise ValueError('Invalid check timeout: ' + name)
            for field in ('cache', 'restore'):
                if field in check and not isinstance(check[field], bool):
                    raise ValueError(field + ' must be a boolean')
            self.validate_env(check.get('env', {}))
            if 'dependencies' in check:
                policy = check['dependencies']
                if (not isinstance(policy, dict) or set(policy) - {'mode','outdir','routes','roots'}
                        or policy.get('mode', 'routes') not in ('routes','models')):
                    raise ValueError('Invalid dependency policy: ' + name)
                relative(policy.get('outdir','public'), 'dependency output')
                strings(policy.get('routes', []), 'dependency routes')
                for path in strings(policy.get('roots', []), 'dependency roots'):
                    relative(path, 'dependency root')
            self.checks[name] = Check(name, check, 'inputs' not in original)
        if not self.checks:
            raise ValueError('Register at least one check')
        self.closure(self.checks)
        self.required = strings(document.get('required', [name for name, check in self.checks.items() if 'release' in check.definition['intents']]), 'required')
        if set(self.required) - self.checks.keys() or any('release' not in self.checks[name].definition['intents'] for name in self.required):
            raise ValueError('Required checks must have a release owner')
        self.tools = strings(document.get('tools', []), 'tools')
        self.validate_inventory()

    @staticmethod
    def validate_env(environment):
        if not isinstance(environment, dict) or any(not isinstance(key, str) or not isinstance(value, str) for key, value in environment.items()):
            raise ValueError('Environment must map names to strings')
        if set(environment) & {'PBGATE_CONTEXT', 'PBGATE_PROFILE', 'PBGATE_PROCESS_OWNER', 'BIMBA_NO_TYPECHECK_DAEMON', 'TMPDIR'}:
            raise ValueError('The gate owns its runtime environment fields')

    @classmethod
    def read(cls, config):
        path = Path(config).resolve()
        return cls(path.parent, json.loads(path.read_text()), path)

    def closure(self, names):
        ordered, visiting, done = [], set(), set()
        def visit(name):
            if name not in self.checks:
                raise ValueError('Unknown prerequisite: ' + name)
            if name in visiting:
                raise ValueError('Dependency cycle at ' + name)
            if name in done:
                return
            visiting.add(name)
            for dependency in self.checks[name].requires:
                visit(dependency)
            visiting.remove(name)
            done.add(name)
            ordered.append(self.checks[name])
        for name in names:
            visit(name)
        return ordered

    def plan(self, intent='release', selected=None, domains=None):
        if intent not in INTENTS:
            raise ValueError('Unsupported intent')
        if intent != 'release' and not (selected or domains):
            raise ValueError('Development and diagnostic runs require --check or --domain')
        if selected and set(selected) - self.checks.keys():
            raise ValueError('Unknown checks: ' + ', '.join(sorted(set(selected) - self.checks.keys())))
        known_domains = {domain for check in self.checks.values() for domain in check.definition['domains']}
        if domains and set(domains) - known_domains:
            raise ValueError('Unknown domains: ' + ', '.join(sorted(set(domains) - known_domains)))
        roots = selected or [name for name, check in self.checks.items() if intent in check.definition['intents']
            and (not domains or set(domains).intersection(check.definition['domains']))]
        if not roots:
            raise ValueError('No checks match')
        checks = self.closure(roots)
        if any(check.definition['intents'] == ['diagnostic'] for check in checks) and intent != 'diagnostic':
            raise ValueError('Diagnostic-only checks cannot enter a development or release plan')
        complete = intent == 'release' and not selected and not domains
        if complete and set(self.required) - {check.id for check in checks}:
            raise ValueError('The release plan does not cover all required checks')
        return Plan(intent, checks, complete)

    def validate_inventory(self):
        actual = {str(path.relative_to(self.root)) for pattern in strings(self.document.get('testInventory', []), 'test inventory')
                  for path in self.root.glob(relative(pattern, 'test inventory')) if path.is_file()}
        owners = {name: [] for name in actual}
        for check in self.checks.values():
            patterns = check.definition['tests'] + self.command_files(check.definition['command'])
            for pattern in patterns:
                paths = [path for path in self.root.glob(pattern) if path.is_file()]
                if pattern in check.definition['tests'] and not paths:
                    raise ValueError('Declared test is missing: ' + pattern)
                for path in paths:
                    name = str(path.relative_to(self.root))
                    if name in owners and check not in owners[name]:
                        owners[name].append(check)
        for name, checks in owners.items():
            if not checks:
                raise ValueError('Unregistered test: ' + name)
            if sum('release' in check.definition['intents'] for check in checks) > 1:
                raise ValueError('A physical test needs one release owner: ' + name)

    def command_files(self, command):
        files = []
        for argument in command:
            if Path(argument).is_absolute():
                continue
            try:
                if (self.root / argument).is_file():
                    files.append(argument)
            except OSError as error:
                if error.errno != errno.ENAMETOOLONG:
                    raise
        return files


@dataclass
class Plan:
    intent: str
    checks: list
    complete: bool

    def describe(self):
        return {'intent': self.intent, 'completeGate': self.complete,
                'checks': [check.definition for check in self.checks]}


class Gate:
    def __init__(self, project, plan, session, force=False):
        self.project, self.plan, self.session, self.force = project, plan, session, force
        self.identities = {}
        self.pending_receipts = []
        from pocketbase import PocketBasePool
        self.dependencies = DependencyGraph(project, session)
        self.fixtures = PocketBasePool(project, session, self.dependencies)
        self.environment = {name: hashlib.sha256(value.encode()).hexdigest() for name, value in session.environment().items()
                            if name not in OPERATIONAL_ENV}
        engine = Path(__file__).parent
        self.implementation_observed = {}
        self.implementation = session.content.inventory(engine, ['*.py', '*.js'], self.implementation_observed)
        self.implementation.update({'client:' + name: value for name, value in session.content.inventory(engine.parent / 'src', ['*.js'], self.implementation_observed).items()})
        self.implementation['parser'] = session.content.common_digest(engine.parent, ['node_modules/typescript'], self.implementation_observed)

    def check_inputs(self, check, observed=None):
        project, content = self.project, self.session.content
        spec = check.definition
        scope = self.dependencies.inputs(check, observed)
        paths = [*spec['inputs'], *spec['tests']]
        if scope and scope['scope'] == 'precise':
            paths = [path for path in paths if not any(path == directory or path.startswith(directory + '/')
                     for directory in (scope['sources'], scope['outdir']))]
            if check.default_inputs:
                paths = [path for path in paths if path not in ('scripts','test','tests')]
        elif scope:
            paths.extend(DEFAULT_INPUTS)
        paths.extend(project.command_files(spec['command']))
        inputs = content.inventory(project.root, paths, observed)
        if scope:
            inputs.update(scope['files'])
            inputs['dependencyScope'] = scope['scope']
        inputs['installedDependencies'] = content.common_digest(project.root, ['node_modules'], observed)
        for tool in [spec['command'][0], *project.tools]:
            executable = shutil.which(tool) or str(project.root / tool)
            inputs['tool:' + tool] = content.file(executable, observed)
        for argument in spec['command'][1:]:
            if Path(argument).is_absolute() and Path(argument).is_file():
                inputs['externalCommand:' + argument] = content.file(argument, observed)
        if check.fixture:
            fixture = project.fixtures[check.fixture]
            inputs.update({'fixture:' + name: value for name, value in self.fixtures.inputs(check.fixture, observed, scope).items()})
            inputs['fixtureRecipe'] = digest(fixture)
        inputs['implementation'] = digest(self.implementation)
        inputs['recipe'] = digest(spec)
        inputs['prerequisites'] = digest({name:(digest(self.project.checks[name].definition)
            if scope and scope['scope'] == 'precise' and self.project.checks[name].definition['kind'] == 'prepare'
            else self.identities[name]) for name in check.requires})
        return inputs

    def sources(self, observed):
        produced = {path for check in self.plan.checks for path in check.outputs}
        patterns = {path for check in self.plan.checks for path in [*check.definition['inputs'], *check.definition['tests']]
                    if not any(path == output or path.startswith(output + '/') for output in produced)}
        patterns.update(arg for check in self.plan.checks for arg in self.project.command_files(check.definition['command']))
        for check in self.plan.checks:
            if check.fixture:
                fixture = self.project.fixtures[check.fixture]
                patterns.update(fixture.get('inputs', []))
                patterns.update(fixture[key] for key in ('hooks', 'migrations') if fixture.get(key) and fixture[key] not in produced)
        if self.project.config:
            patterns.add(str(self.project.config.relative_to(self.project.root)))
        return self.session.content.inventory(self.project.root, sorted(patterns), observed)

    def execute(self, check, checkpoint):
        session, spec = self.session, check.definition
        observed = {}
        inputs = self.check_inputs(check, observed)
        parameters = {'profile': session.profile, 'policy': self.project.policy, 'environment': self.environment,
                      'platform': [platform.system(), platform.release(), platform.machine()]}
        key = ReceiptCache.key(inputs, [spec['command']], parameters)
        cacheable = spec.get('cache', True) and (self.plan.intent == 'release' or spec['kind'] == 'prepare')
        receipt = None if self.force or not cacheable else session.receipts.read(key, self.project.root, check.outputs)
        if not receipt and not self.force and cacheable and spec.get('restore'):
            if session.receipts.restore(key, self.project.root, check.outputs):
                receipt = session.receipts.read(key, self.project.root, check.outputs)
        if receipt:
            session.content.inventory(self.project.root, check.outputs, observed)
            checkpoint.protect(observed)
            self.identities[check.id] = {'key': key, 'outputs': receipt['outputs']}
            print(check.id + ': cached', flush=True)
            return {'id': check.id, 'key': key, 'cached': True, 'exitCode': 0, 'metrics': {}}
        if cacheable:
            session.receipts.invalidate(key)
        miss = session.receipts.explain(check.id, inputs, [spec['command']], parameters) if cacheable else {'reason': 'fresh_targeted_check'}
        print(check.id + ': running', flush=True)
        log = session.logs / (check.id + '.log')
        result = {'id': check.id, 'key': key, 'cached': False, 'cacheMiss': miss, 'log': str(log)}
        checkpoint.protect(observed)
        try:
            if check.fixture:
                code, metrics = self.fixtures.run(check, log)
            else:
                code, metrics = session.run(spec['command'], self.project.root, log, spec.get('env'), spec['timeoutSeconds'])
            final_inputs = self.check_inputs(check)
            if not code and inputs != final_inputs:
                code = 2
                result['error'] = 'Check inputs changed during execution'
            outputs = session.content.inventory(self.project.root, check.outputs)
            if not code and any(value == 'missing' for value in outputs.values()):
                code = 2
                result['error'] = 'Declared outputs are missing'
            result.update(exitCode=code, metrics=metrics)
            if not code:
                self.identities[check.id] = {'key': key, 'outputs': outputs}
                if cacheable:
                    self.pending_receipts.append((check, key, {'stage': check.id, 'inputs': inputs, 'command': [spec['command']],
                        'parameters': parameters, 'metrics': metrics}, outputs))
        except subprocess.TimeoutExpired:
            result.update(exitCode=124, error='Check exceeded its wall-time limit')
        except Exception as error:
            result.update(exitCode=2, error=str(error))
        if result['exitCode']:
            print(check.id + ': failed; ' + result.get('error', 'see ' + str(log)), file=sys.stderr, flush=True)
        return result

    @staticmethod
    def conflicts(first, second):
        for left, rights in [(item, second.writes + second.reads) for item in first.writes] + [(item, first.reads) for item in second.writes]:
            for right in rights:
                if left == right or left.startswith(right + '/') or right.startswith(left + '/'):
                    return True
        return False

    def run(self):
        start = time.monotonic()
        checkpoint = self.session.content.checkpoint(self.sources)
        checkpoint.protect(self.implementation_observed)
        report = {'format': 1, 'started': datetime.now(timezone.utc).isoformat(),
                  'intent': self.plan.intent, 'completeGate': self.plan.complete, 'checks': []}
        pending, running, done = list(self.plan.checks), {}, set()
        interrupted = None
        workers = PROFILES[self.session.profile]['workers']
        try:
            with ThreadPoolExecutor(max_workers=workers) as executor:
                failed = False
                while pending or running:
                    if not failed:
                        for check in list(pending):
                            if len(running) >= workers:
                                break
                            if set(check.requires) <= done and not any(self.conflicts(check, other) for other in running.values()):
                                pending.remove(check)
                                running[executor.submit(self.execute, check, checkpoint)] = check
                    if not running:
                        if pending and not failed:
                            raise ValueError('No runnable check satisfies its prerequisites')
                        break
                    finished, _ = wait(running, return_when=FIRST_COMPLETED)
                    for future in finished:
                        check = running.pop(future)
                        result = future.result()
                        report['checks'].append(result)
                        if result['exitCode']:
                            failed = True
                        else:
                            done.add(check.id)
            report['skipped'] = [check.id for check in pending]
        except BaseException as error:
            interrupted = error
            report['error'] = str(error)
        finally:
            try:
                self.fixtures.close()
                self.session.check_resources()
            except BaseException as error:
                interrupted = interrupted or error
                report['error'] = str(error)
            stability = checkpoint.finish()
            report.update(stable=stability['stable'], changedInputs=stability['changed_inputs'])
            report['passed'] = interrupted is None and len(report['checks']) == len(self.plan.checks) and stability['stable'] and all(not check['exitCode'] for check in report['checks'])
            if stability['stable'] and interrupted is None:
                for check, key, receipt, outputs in self.pending_receipts:
                    if self.session.content.inventory(self.project.root, check.outputs) == outputs:
                        self.session.receipts.save(key, receipt, self.project.root, check.outputs, artifacts=bool(check.definition.get('restore')))
                    else:
                        report['passed'] = False
            report.update(totalWallSeconds=round(time.monotonic() - start, 4), resources=self.session.monitor.summary())
            atomic_json(self.session.logs / 'report.json', report)
            if self.plan.complete:
                atomic_json(self.project.state / 'release.json', report)
        if interrupted:
            raise interrupted
        print('Report: ' + str(self.session.logs / 'report.json'), flush=True)
        return 0 if report['passed'] else next((check['exitCode'] for check in report['checks'] if check['exitCode']), 2)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('action', choices=('plan', 'run'))
    parser.add_argument('--config', default='pbgate.json')
    parser.add_argument('--intent', choices=sorted(INTENTS), default='release')
    parser.add_argument('--check', action='append')
    parser.add_argument('--domain', action='append')
    parser.add_argument('--profile', choices=tuple(PROFILES), default='quiet')
    parser.add_argument('--release', action='store_true')
    parser.add_argument('--force', action='store_true')
    args = parser.parse_args()
    project = Project.read(args.config)
    plan = project.plan(args.intent, args.check, args.domain)
    if args.action == 'plan':
        print(json.dumps(plan.describe(), indent=2))
        return 0
    if args.intent == 'release' and not args.release:
        parser.error('Release execution requires --release; use --intent dev for targeted checks')
    if args.force and not args.release:
        parser.error('--force requires explicit --release')
    with ProcessSession(project.state, args.profile, project.policy) as session:
        return Gate(project, plan, session, args.force).run()


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print('Gate cancelled; owned processes and temporary fixtures were disposed.', file=sys.stderr)
        raise SystemExit(130)
    except (ValueError, RuntimeError, OSError) as error:
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
