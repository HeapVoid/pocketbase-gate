"""Declarative verification catalog, dependency plans and process recipes.

Catalogs own stable test leaves, dependency closure, coverage and batching.
Projects supply recipes and input contracts; the engine owns their expansion.
"""
import ast
import argparse
import glob
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys

SOURCE_HASH = hashlib.sha256(Path(__file__).read_bytes()).hexdigest()
INTENTS = {'dev', 'release', 'diagnostic'}


def semantic_hash(path):
    """Bind all executable Python code, excluding formatting and docstrings."""
    tree = ast.parse(Path(path).read_text())
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            body = node.body
            if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) and isinstance(body[0].value.value, str):
                node.body = body[1:]
    return hashlib.sha256(ast.dump(tree, include_attributes=False).encode()).hexdigest()


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class VerificationCatalog:
    def __init__(self, document):
        document = dict(document)
        owners = [stage['name'] for stage in document['stages'] if stage.get('model_partition')]
        if 'model_owner' not in document and len(owners) == 1:
            document['model_owner'] = owners[0]
        self.document = document
        self.component = document.get('component', 'legacy')
        for domain in document.get('python_domains', {}).values():
            if not domain.get('tests') or not isinstance(domain.get('inputs'), list) or not isinstance(domain.get('requires'), list):
                raise ValueError('Python domains require tests, inputs and prerequisites')
            for path in domain['inputs'] + domain['tests']:
                if Path(path).is_absolute() or '..' in Path(path).parts:
                    raise ValueError('Python domain paths must be checkout relative: ' + path)
            if set(domain.get('input_sets', [])) - set(document.get('input_sets', {})):
                raise ValueError('Unknown Python input set')
        self.stages = []
        self.named = {}
        self._batch_inputs = {}
        self._file_commands = {}
        sets = document.get('input_sets', {})
        originals = []
        for original in document['stages']:
            if original.get('model_domain'):
                if original['model_domain'] not in document.get('unit_domains', {}):
                    raise ValueError('Unknown model domain: ' + original['model_domain'])
                owner = next(stage for stage in document['stages'] if stage['name'] == document['model_owner'])
                original = dict(owner, **original)
            originals.append(original)
            reason = original.get('historical_rehearsal')
            if reason is not None:
                if (not isinstance(reason, str) or not reason.strip()
                        or original.get('kind') != 'http' or not original.get('command')
                        or original.get('steps') or original.get('covers') or original.get('outputs')
                        or original.get('historical_transition')):
                    raise ValueError('Historical rehearsals require a Backend HTTP leaf: ' + original['name'])
                history = dict(original, name=original['name'] + '.historical',
                               id=original.get('id', self.component + '.' + original['name']) + '.historical', intents=['diagnostic'],
                               command=[*original['command'], '--historical'],
                               historical_transition=reason, manual_reason=reason)
                history.pop('historical_rehearsal')
                originals.append(history)
        for original in originals:
            stage = dict(original)
            name = stage['name']
            if name in self.named:
                raise ValueError('Duplicate check: ' + name)
            stage.setdefault('id', self.component + '.' + name)
            stage.setdefault('domains', ['all'])
            stage.setdefault('kind', stage['group'])
            stage.setdefault('requires', [])
            stage.setdefault('outputs', [])
            stage.setdefault('intents', ['release'])
            if not set(stage['intents']) <= INTENTS:
                raise ValueError('Invalid check intents: ' + name)
            if stage.get('historical_transition') is not None:
                reason = stage['historical_transition']
                if (not isinstance(reason, str) or not reason.strip()
                        or stage['intents'] != ['diagnostic'] or stage.get('covers') or stage.get('outputs')):
                    raise ValueError('Historical transitions must be diagnostic Backend leaves: ' + name)
            stage['inputs'] = list(stage.get('inputs', []))
            for key in stage.get('input_sets', []):
                if key not in sets:
                    raise ValueError('Unknown input set: ' + key)
                stage['inputs'].extend(sets[key])
            stage['inputs'] = list(dict.fromkeys(stage['inputs']))
            if stage.get('backend_runtime'):
                policy = stage['backend_runtime']
                if (not isinstance(policy.get('routes'), list)
                        or any(not isinstance(route, str) or (route != '*' and not route.startswith('/')) for route in policy['routes'])
                        or any(not root.startswith('src/') or '..' in Path(root).parts for root in policy.get('roots', []))):
                    raise ValueError('Invalid Backend runtime dependencies: ' + name)
            if stage.get('member_runner'):
                if stage.get('partition_by') != 'lifecycle' or not stage.get('covers') or stage.get('outputs'):
                    raise ValueError('Lifecycle batching requires leaf coverage without artifacts: ' + name)
                self._batch_inputs[name] = stage['inputs']
                stage['command'] = stage['member_runner']
            if stage.get('receipt_by') is not None and (stage['receipt_by'] != 'check' or not stage.get('member_runner')):
                raise ValueError('Per-check receipts require a lifecycle owner: ' + name)
            if stage.get('model_partition') is not None and (stage['model_partition'] != 'unit_domains' or not document.get('unit_domains')):
                raise ValueError('Model partitions require Backend unit_domains: ' + name)
            if stage.get('python_partition') is not None and (stage['python_partition'] != 'python_domains' or not document.get('python_domains')):
                raise ValueError('Python partitions require Backend python_domains: ' + name)
            if stage.get('file_checks'):
                if not stage.get('command') or stage.get('steps') or stage.get('outputs') or stage.get('covers'):
                    raise ValueError('File checks require a command without artifacts or coverage: ' + name)
                self._file_commands[name] = stage['command']
                files = []
                for suffix, definition in stage['file_checks'].items():
                    if not suffix or not definition.get('tests') or not isinstance(definition.get('inputs'), list):
                        raise ValueError('File checks require tests and inputs: ' + name)
                    for path in definition['tests'] + definition['inputs']:
                        if Path(path).is_absolute() or '..' in Path(path).parts:
                            raise ValueError('File check paths must be checkout relative: ' + path)
                    if set(definition.get('input_sets', [])) - set(sets):
                        raise ValueError('Unknown file check input set: ' + name)
                    files.extend(definition['tests'])
                if len(files) != len(set(files)):
                    raise ValueError('File checks need one physical file owner: ' + name)
                stage['command'] = [*stage['command'], *files]
            if not stage.get('command') and not stage.get('steps'):
                raise ValueError('Missing recipe: ' + name)
            if stage.get('test_arguments') and (stage['intents'] != ['diagnostic'] or not stage.get('command') or stage.get('covers')):
                raise ValueError('Parameterized checks require a diagnostic leaf command: ' + name)
            if stage.get('lifecycle'):
                scripts = [arg for step in recipe(stage) for arg in step if arg.startswith('scripts/test-') and arg.endswith('.js')]
                if stage['lifecycle'].get('scripts') != scripts or not scripts:
                    raise ValueError('Lifecycle scripts differ from their check recipe: ' + name)
                if stage.get('fixture', {}).get('mode') not in ('shared', 'isolated'):
                    raise ValueError('Lifecycle requires an explicit fixture mode: ' + name)
            for path in stage['inputs'] + stage.get('tests', []) + stage.get('test_exclude', []) + stage.get('outputs', []) + stage.get('source_inputs', []) + stage.get('fallback_inputs', []) + stage.get('source_exclude', []) + ([stage['source_manifest']] if stage.get('source_manifest') else []):
                if Path(path).is_absolute() or '..' in Path(path).parts:
                    raise ValueError('Check paths must be checkout relative: ' + path)
            self.stages.append(stage)
            self.named[name] = stage
        ids = [stage['id'] for stage in self.stages]
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate check ID')
        for stage in self.stages:
            if stage.get('member_runner'):
                members = [self.named[name] for name in stage['covers'] if name in self.named]
                if len(members) != len(set(stage['covers'])) or any(not member.get('lifecycle', {}).get('id') for member in members):
                    raise ValueError('Invalid lifecycle batch membership: ' + stage['name'])
                stage.update(self._lifecycle_batch(stage, members))
        self.batches = {batch['name']: batch for stage in self.stages if stage.get('member_runner')
                        for batch in [*self.execution_stages(stage, per_check=False), *self.execution_stages(stage)]}
        self.batches.update({check['name']:check for stage in self.stages
                             if stage.get('python_partition') or stage.get('model_partition') or stage.get('file_checks')
                             for check in self.execution_stages(stage)})
        if self.batches.keys() & self.named.keys():
            raise ValueError('Lifecycle batch names must be unique')
        self.named.update(self.batches)
        ids += [batch['id'] for batch in self.batches.values()]
        if len(set(ids)) != len(ids):
            raise ValueError('Duplicate check ID')
        for alias, names in document.get('aliases', {}).items():
            if not isinstance(names, list) or not names or set(names) - set(self.named) - set(ids):
                raise ValueError('Invalid check alias: ' + alias)
        self.covered_by = {}
        for stage in self.stages:
            for name in stage.get('covers', []):
                if name not in self.named or name == stage['name'] or name in self.covered_by:
                    raise ValueError('Invalid scenario coverage: ' + name)
                member = self.named[name]
                if member.get('covers') or member.get('outputs'):
                    raise ValueError('Scenario members must be leaf checks without artifacts: ' + name)
                if not set(member['inputs']) <= set(stage['inputs']) or not set(member.get('external_inputs', [])) <= set(stage.get('external_inputs', [])):
                    raise ValueError('Scenario is missing member inputs: ' + name)
                if not set(member['requires']) <= set(stage['requires']):
                    raise ValueError('Scenario is missing member prerequisites: ' + name)
                scripts = {arg for step in recipe(member) for arg in step if arg.startswith('scripts/test-') and arg.endswith('.js')}
                if not scripts or not scripts <= {arg for step in recipe(stage) for arg in step}:
                    raise ValueError('Scenario is missing member scripts: ' + name)
                self.covered_by[name] = stage['name']
        for name in document.get('release_requirements', []):
            owner = self.covered_by.get(name, name)
            if name not in self.named or 'release' not in self.named[owner]['intents']:
                raise ValueError('Mandatory release check was removed: ' + name)
        self._closure(self.named)

    def _lifecycle_batch(self, owner, members, suffix=''):
        scripts = [script for member in members for script in member['lifecycle']['scripts']]
        result = dict(owner, name=owner['name'] + suffix, id=owner['id'] + suffix,
                    covers=[member['name'] for member in members],
                    domains=list(dict.fromkeys(domain for member in members for domain in member['domains'])),
                    inputs=list(dict.fromkeys([*self._batch_inputs[owner['name']],
                        *(path for member in members for path in member['inputs'])])),
                    external_inputs=list(dict.fromkeys(path for member in members for path in member.get('external_inputs', []))),
                    members=[{key: member.get(key) for key in ('name', 'lifecycle', 'fixture', 'command', 'steps', 'requires')} for member in members],
                    command=[*owner['member_runner'], *('--check=' + member['name'] for member in members), *scripts])
        if any(member.get('backend_runtime') for member in members):
            result['backend_runtime'] = {key: sorted({item for member in members
                for item in member.get('backend_runtime', {'routes':['*']}).get(key, [])}) for key in ('routes','roots')}
        return result

    def execution_stages(self, stage, per_check=None):
        if stage.get('python_partition') or stage.get('file_checks'):
            result = []
            python = bool(stage.get('python_partition'))
            definitions = self.document['python_domains'] if python else stage['file_checks']
            command = stage['command'] if python else self._file_commands[stage['name']]
            separator = ['--files'] if python else []
            for domain, definition in definitions.items():
                inputs = list(dict.fromkeys([*definition['inputs'], *(path for name in definition.get('input_sets', [])
                    for path in self.document['input_sets'][name])]))
                check = dict(stage, name=stage['name'] + '.' + domain, id=stage['id'] + '.' + domain,
                             command=[*command, *separator, *definition['tests']],
                             tests=definition['tests'], inputs=inputs, requires=definition.get('requires', stage['requires']),
                             external_inputs=definition.get('external_inputs', []), execution_owner=stage['name'])
                check.pop('python_partition', None)
                check.pop('file_checks', None)
                if definition.get('backend_runtime'):
                    check['backend_runtime'] = definition['backend_runtime']
                if not python or not definition.get('backend_runtime'):
                    check['file_batch'] = {'command': command, 'separator': separator}
                result.append(check)
            return result
        if stage.get('model_partition'):
            excluded = set(stage.get('test_exclude', []))
            result = []
            for domain, declared in self.document['unit_domains'].items():
                if stage.get('model_domain') and domain != stage['model_domain']:
                    continue
                tests = [name for name in declared if name not in excluded]
                if not tests:
                    continue
                inputs = [name for name in stage['inputs'] if name not in
                          self.document.get('model_scope_inputs', [])]
                for test in tests:
                    suffix = '.' + domain + '.' + test.replace('/', '.').removeprefix('test.').removesuffix('.test.js')
                    check = dict(stage, name=stage['name'] + suffix, id=stage['id'] + suffix,
                                 command=[*stage['command'], '--files', test], inputs=inputs,
                                 tests=[test], test_exclude=[], backend_models={'domain':domain},
                                 file_batch={'command':stage['command'], 'separator':['--files']}, execution_owner=stage['name'])
                    check.pop('model_partition')
                    result.append(check)
            return result
        if not stage.get('member_runner'):
            return [stage]
        if per_check is None:
            per_check = stage.get('receipt_by') == 'check'
        if per_check:
            return [dict(self._lifecycle_batch(stage, [self.named[name]], '.check.' + name),
                         member_runner=None, partition_by=None, execution_owner=stage['name'])
                    for name in stage['covers']]
        # Contiguous runs preserve the historical order, even when a domain
        # occurs again later. Runtime reuse belongs to the verification session;
        # these boundaries continue to own receipts, dependencies and reports.
        batches, counts = [], {}
        for name in stage['covers']:
            member = self.named[name]
            domain = member['lifecycle']['id']
            if not batches or batches[-1][0] != domain:
                batches.append((domain, []))
            batches[-1][1].append(member)
        result = []
        for domain, members in batches:
            counts[domain] = counts.get(domain, 0) + 1
            suffix = '.' + domain + ('.' + str(counts[domain]) if counts[domain] > 1 else '')
            result.append(self._lifecycle_batch(stage, members, suffix))
            result[-1].pop('member_runner')
            result[-1].pop('partition_by')
        return result

    @classmethod
    def read(cls, repo, path=None):
        repo = Path(repo)
        path = Path(path or repo / 'scripts/verification-stages.json')
        data = path.read_bytes()
        document = json.loads(data)
        patterns = set(document.get('test_inventory', {}).get('patterns', []))
        for stage in document['stages']:
            if stage.get('file_partition') != 'tests':
                continue
            patterns.update(stage.get('tests', []))
            if not stage.get('tests'):
                raise ValueError('File partitions require registered test patterns')
            names = sorted({str(path.relative_to(repo)) for pattern in stage['tests']
                            for path in repo.glob(pattern) if path.is_file()} - set(stage.get('test_exclude', [])))
            if not names:
                raise ValueError('File partition discovered no tests: ' + stage['name'])
            stage['file_checks'] = {name.replace('/', '.'): {'tests':[name], 'inputs':stage.get('inputs', []),
                'input_sets':stage.get('input_sets', []), 'external_inputs':stage.get('external_inputs', [])} for name in names}
            if len(stage['file_checks']) != len(names):
                raise ValueError('Discovered test paths have conflicting leaf names')
            stage.pop('file_partition')
        catalog = cls(document)
        catalog.source = str(path), hashlib.sha256(data).hexdigest()
        catalog.inventory = str(repo), {pattern:sorted(str(file.relative_to(repo)) for file in repo.glob(pattern) if file.is_file()) for pattern in patterns}
        return catalog

    def _closure(self, names):
        ordered, visiting, visited = [], set(), set()
        def visit(name):
            if name not in self.named:
                raise ValueError('Unknown check dependency: ' + name)
            if name in visiting:
                raise ValueError('Check dependency cycle: ' + name)
            if name in visited:
                return
            visiting.add(name)
            for dependency in self.named[name]['requires']:
                visit(dependency)
            visiting.remove(name)
            visited.add(name)
            ordered.append(name)
        for name in names:
            visit(name)
        return ordered

    def plan(self, intent='release', selected=None, domains=None, include_tooling=True):
        if intent not in INTENTS:
            raise ValueError('Unknown verification intent: ' + intent)
        selected, domains = set(selected or []), set(domains or [])
        aliases = self.document.get('aliases', {})
        selected = {check for name in selected for check in aliases.get(name, [name])}
        unknown = selected - set(self.named) - {stage['id'] for stage in self.named.values()}
        if unknown:
            raise ValueError('Unknown checks: ' + ', '.join(sorted(unknown)))
        known_domains = {domain for stage in self.stages for domain in stage['domains']}
        if domains - known_domains:
            raise ValueError('Unknown domains: ' + ', '.join(sorted(domains - known_domains)))
        if intent != 'release' and not (selected or domains):
            raise ValueError('Targeted checks require --stage or --domain')
        roots, reasons = [], {}
        for stage in [*self.stages, *self.batches.values()]:
            name = stage['name']
            explicit = name in selected or stage['id'] in selected
            matched = domains.intersection(stage['domains'])
            if selected:
                chosen = explicit
            elif domains:
                chosen = bool(matched) and intent in stage['intents']
            else:
                chosen = intent in stage['intents']
            if name in self.batches and not explicit:
                chosen = False
            if not include_tooling and stage['group'] == 'tooling' and not explicit:
                chosen = False
            if chosen:
                roots.append(name)
                reasons[name] = 'explicit check' if explicit else ('domain: ' + ', '.join(sorted(matched)) if matched else intent + ' requirement')
        if not roots:
            raise ValueError('No checks match this plan')
        ordered = self._closure(roots)
        historical = [name for name in ordered if self.named[name].get('historical_transition')]
        historical += [member for name in ordered for member in self.named[name].get('covers', [])
                       if self.named[member].get('historical_transition')]
        if historical and intent != 'diagnostic':
            raise ValueError('Historical transitions require --intent diagnostic: ' + ', '.join(historical))
        complete = intent == 'release' and not (selected or domains) and include_tooling
        if complete:
            delivered = {member for name in ordered for member in [name, *self.named[name].get('covers', [])]}
            missing = set(self.document.get('release_requirements', [])) - delivered
            if missing:
                raise ValueError('Release plan is missing mandatory checks: ' + ', '.join(sorted(missing)))
        for name in ordered:
            if name not in reasons:
                consumers = [other for other in ordered if name in self.named[other]['requires']]
                reasons[name] = 'prerequisite of ' + ', '.join(consumers)
        stages = []
        for name in ordered:
            for stage in self.execution_stages(self.named[name]):
                reasons[stage['name']] = reasons[name]
                stages.append(stage)
        plan = VerificationPlan(self.component, intent, stages, reasons, complete)
        plan.source = getattr(self, 'source', None)
        plan.inventory = getattr(self, 'inventory', None)
        return plan

    def plan_test_files(self, files):
        """Keep model files addressable and route tooling files to their owner."""
        names = list(dict.fromkeys(files))
        registered = {name for members in self.document.get('unit_domains', {}).values() for name in members}
        registered -= set(self.named.get(self.document.get('model_owner'), {}).get('test_exclude', []))
        if not names:
            raise ValueError('Choose registered Backend test files')
        selected = []
        partitioned = {file:stage['name'] for stage in self.batches.values()
                       if stage.get('file_batch') or stage.get('execution_owner') in {item['name'] for item in self.stages if item.get('python_partition')}
                       for file in stage.get('tests', [])}
        for name in names:
            if name in registered:
                continue
            if name in partitioned:
                selected.append(partitioned[name])
                continue
            owners = [stage['name'] for stage in self.stages if 'dev' in stage['intents']
                      and ('release' in stage['intents'] or stage['name'] in self.covered_by)
                      and not stage.get('covers') and name in stage.get('tests', []) +
                      [arg.removeprefix('./') for step in recipe(stage) for arg in step]]
            if len(owners) != 1:
                raise ValueError('Choose registered Backend test files: ' + name)
            selected.extend(owners)
        stages = self.document['stages']
        models = [name for name in names if name in registered]
        if models:
            stage = dict(self.named[self.document['model_owner']], name='dev-model-files', id='backend.dev.model-files',
                         intents=['dev'], requires=self.named[self.document['model_owner']]['requires'], command=[*self.named[self.document['model_owner']]['command'], '--files', *models])
            stage.pop('model_partition', None)
            stages = [*stages, stage]
            selected.append(stage['name'])
        catalog = VerificationCatalog(dict(self.document, stages=stages))
        catalog.source, catalog.inventory = getattr(self, 'source', None), getattr(self, 'inventory', None)
        return catalog.plan(intent='dev', selected=list(dict.fromkeys(selected)))

    def validate_inventory(self, repo):
        repo = Path(repo)
        policy = self.document.get('test_inventory')
        if policy:
            actual = {str(path.relative_to(repo)) for pattern in policy['patterns'] for path in repo.glob(pattern) if path.is_file()}
            owners = {name: [] for name in actual}
            for stage in self.stages:
                if stage.get('covers'):
                    continue  # Covered leaf recipes own their tests.
                patterns = stage.get('tests', []) + [arg for step in recipe(stage) for arg in step]
                for pattern in patterns:
                    if not pattern or pattern.startswith('-') or not Path(pattern).parts or Path(pattern).is_absolute() or '..' in Path(pattern).parts:
                        continue
                    pattern = Path(pattern).as_posix()
                    matches = list(repo.glob(pattern))
                    if not matches and pattern.startswith(('test/','tests/','scripts/test-')):
                        raise ValueError('Declared test is missing: ' + pattern)
                    for path in matches:
                        name = str(path.relative_to(repo))
                        if name in stage.get('test_exclude', []):
                            continue
                        if name in owners and stage['name'] not in owners[name]:
                            owners[name].append(stage['name'])
            missing = [name for name, checks in owners.items() if not any(self.named[self.covered_by.get(check, check)]['intents'] for check in checks)]
            if missing:
                raise ValueError('Unclassified tests; add their recipe and intent: ' + ', '.join(sorted(missing)))
            for stage in self.stages:
                for pattern in stage.get('tests', []):
                    if not list(repo.glob(pattern)):
                        raise ValueError('Declared test is missing: ' + pattern)
            for name, checks in owners.items():
                release = [check for check in checks if 'release' in self.named[self.covered_by.get(check, check)]['intents']]
                if len(release) > 1:
                    raise ValueError('Test has multiple release owners: ' + name + ': ' + ', '.join(release))
        declared = {argument for stage in self.stages for step in recipe(stage) for argument in step}
        missing = [str(path.relative_to(repo)) for path in (repo / 'scripts').glob('test-*-isolated.js')
                   if str(path.relative_to(repo)) not in declared]
        if missing:
            raise ValueError('Unclassified native scenarios: ' + ', '.join(sorted(missing)))
        if self.document.get('unit_domains') is not None:
            entries = [path for paths in self.document['unit_domains'].values() for path in paths]
            classified = set(entries)
            if len(entries) != len(classified):
                raise ValueError('Model tests must belong to one unit domain')
            actual = {str(path.relative_to(repo)) for path in (repo / 'test').rglob('*.test.js')}
            if classified != actual:
                raise ValueError('Update unit_domains for added/deleted model tests: ' + ', '.join(sorted(classified ^ actual)))
        if self.document.get('python_domains') is not None:
            entries = [path for domain in self.document['python_domains'].values() for path in domain['tests']]
            actual = {str(path.relative_to(repo)) for path in (repo / 'test').glob('test_release_*.py')}
            if len(entries) != len(set(entries)) or set(entries) != actual:
                raise ValueError('Update python_domains; each release test needs one domain')
        if self.document.get('session_domains') is not None:
            classified = {path for paths in self.document['session_domains'].values() for path in paths}
            actual = {str(path.relative_to(repo)) for path in (repo / 'tests').glob('session-*.test.js')}
            if classified != actual:
                raise ValueError('Update session_domains for added/deleted session tests: ' + ', '.join(sorted(classified ^ actual)))


class VerificationPlan:
    def __init__(self, component, intent, stages, reasons, complete=False):
        self.component, self.intent = component, intent
        self.stages, self.reasons, self.complete = stages, reasons, complete

    def assert_current(self):
        source = getattr(self, 'source', None)
        if source and hashlib.sha256(Path(source[0]).read_bytes()).hexdigest() != source[1]:
            raise RuntimeError('Verification catalog changed while queued; repeat planning')
        inventory = getattr(self, 'inventory', None)
        if inventory:
            repo = Path(inventory[0])
            actual = {pattern:sorted(str(file.relative_to(repo)) for file in repo.glob(pattern) if file.is_file()) for pattern in inventory[1]}
            if actual != inventory[1]:
                raise RuntimeError('Test inventory changed while queued; repeat planning')

    def bind_test_arguments(self, arguments=None, allow_missing=False):
        checks = [stage for stage in self.stages if stage.get('test_arguments')]
        if arguments:
            if self.intent != 'diagnostic' or len(checks) != 1:
                raise ValueError('--test-args requires one parameterized diagnostic check')
            target = checks[0]
            self.stages = [dict(stage, command=[*stage['command'], *arguments]) if stage is target else stage for stage in self.stages]
        elif checks and not allow_missing:
            raise ValueError('Provide --test-args for ' + ', '.join(stage['name'] + ': ' + stage['test_arguments'] for stage in checks))

    def describe(self):
        return {'format': 1, 'component': self.component, 'intent': self.intent,
                'complete_gate': self.complete, 'checks': [
                    {'id': stage['id'], 'name': stage['name'], 'domains': stage['domains'],
                     'kind': stage['kind'], 'requires': stage['requires'],
                     'covers': stage.get('covers', []),
                     **({'execution_owner': stage['execution_owner']} if stage.get('execution_owner') else {}),
                     'test_arguments': stage.get('test_arguments'),
                     **({'historical_transition': stage['historical_transition']} if stage.get('historical_transition') else {}),
                     **({'backend_runtime': stage['backend_runtime']} if stage.get('backend_runtime') else {}),
                     'reason': self.reasons[stage['name']], 'inputs': stage['inputs'],
                     'outputs': stage.get('outputs', []), 'recipe': recipe(stage)}
                    for stage in self.stages]}


class ExecutionOrder:
    """Keep complete coverage while bringing failed checks and prerequisites forward."""
    def __init__(self, stages, preferred=()):
        self.pending = list(stages)
        self.done = set()
        self.groups = {}
        for stage in stages:
            for name in [stage['name'], stage.get('execution_owner'), *stage.get('covers', [])]:
                if name:
                    self.groups.setdefault(name, set()).add(stage['name'])
        preferred = set(preferred)
        self.preferred = {stage['name'] for stage in stages if preferred.intersection(
                          [stage['name'], stage.get('execution_owner'), *stage.get('covers', [])])}
        self.ancestors = set()
        def ancestors(stage):
            for dependency in stage.get('requires', []):
                for name in self.groups.get(dependency, set()):
                    if name not in self.ancestors:
                        self.ancestors.add(name)
                        ancestors(next(item for item in stages if item['name'] == name))
        for stage in stages:
            if stage['name'] in self.preferred:
                ancestors(stage)

    def ready(self):
        return [stage for stage in self.pending if all(
            self.groups.get(name, {name}) <= self.done for name in stage.get('requires', []))]

    def priority(self, stage):
        return 0 if stage['name'] in self.preferred else 1 if stage['name'] in self.ancestors else 2

    def finish(self, stages):
        for stage in stages:
            self.pending.remove(stage)
            self.done.add(stage['name'])


def access_conflict(first_reads, first_writes, second_reads, second_writes):
    """Serialize shared resources and overlapping output/read paths."""
    import fnmatch
    pairs = [(item, second_writes + second_reads) for item in first_writes]
    pairs += [(item, first_reads) for item in second_writes]
    return any(left == right or left.startswith(right + '/') or right.startswith(left + '/')
               or fnmatch.fnmatch(left, right) or fnmatch.fnmatch(right, left)
               for left, rights in pairs for right in rights)


def recipe(stage):
    return stage.get('steps') or [stage['command']]


def execution_command(stage):
    if stage.get('steps'):
        return [sys.executable, str(Path(__file__).resolve()), '--execute', json.dumps(stage['steps'])]
    return stage['command']


def package_inputs(repo, stage):
    """Hash package metadata and only scripts actually used by this recipe."""
    path = Path(repo) / 'package.json'
    package = json.loads(path.read_text()) if path.exists() else {}
    scripts = package.pop('scripts', {})
    used, pending = {}, []
    import re
    for command in recipe(stage):
        if command[:2] == ['bun', 'run'] and len(command) > 2:
            pending.append(command[2])
    while pending:
        name = pending.pop()
        if name in used or name not in scripts:
            continue
        used[name] = scripts[name]
        pending.extend(re.findall(r'\bbun\s+run\s+(?:--[\w-]+\s+)*([\w:.-]+)', scripts[name]))
    return {'manifest': digest(package), 'scripts': digest(used)}


def run_recipe(steps):
    if not (os.environ.get('CI') or os.environ.get('PBGATE_SESSION')):
        raise RuntimeError('Recipes must run through the verification executor')
    for step in steps:
        command = []
        for argument in step:
            bindings = dict(CHECKOUT=str(Path.cwd()), **json.loads(os.environ.get('PBGATE_RECIPE_BINDINGS', '{}')))
            for name, value in bindings.items():
                argument = argument.replace('${' + name + '}', value)
            if '${' in argument:
                raise ValueError('Missing recipe binding: ' + argument)
            matches = sorted(glob.glob(argument)) if any(char in argument for char in '*?[') else []
            command.extend(matches or [argument])
        result = subprocess.run(command)
        if result.returncode:
            return result.returncode
    return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--execute', required=True)
    arguments = parser.parse_args()
    raise SystemExit(run_recipe(json.loads(arguments.execute)))
