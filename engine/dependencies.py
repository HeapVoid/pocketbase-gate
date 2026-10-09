"""Complete compiler and test descriptors; opaque inputs retain a broad scope."""
import hashlib
import json
from pathlib import Path
import shutil


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


class DependencyGraph:
    def __init__(self, project, session):
        self.project, self.session = project, session
        self.descriptors = {}

    def graph(self, outdir='public', observations=None):
        root, content = self.project.root, self.session.content
        path = root / outdir / '.pbgate-dependencies.json'
        content.file(path, observations)
        graph = json.loads(path.read_text())
        sources = content.common_inventory(root, [graph['sources']], observations)
        names = {name for name in sources if name.endswith('.imba')}
        generator = Path(__file__).resolve().parents[1] / 'src/dependencies.js'
        if (graph['format'] != 1 or graph['outdir'] != outdir or set(graph['modules']) != names
                or graph['generator'] != content.file(generator, observations)
                or set(graph['hooks']) != {name for name in names if name.endswith('.pb.imba')}):
            raise ValueError('Stale compiler descriptor')
        for name, module in graph['modules'].items():
            output = outdir + '/' + name[len(graph['sources'])+1:-5] + '.js'
            if module['hash'] != sources[name] or module['output'] != output or module['output_hash'] != content.file(root / output, observations):
                raise ValueError('Stale compiler output')
            for scope in [module, *module.get('routes', [])]:
                if not isinstance(scope['opaque'], bool) or set(scope['roots']) - names:
                    raise ValueError('Incomplete compiler scope')
        return graph, sources

    def select(self, graph, sources, policy):
        modules, hooks = graph['modules'], graph['hooks']
        mode = policy.get('mode', 'routes')
        selected = (set() if mode == 'models' else set(hooks)) | set(policy.get('roots', []))
        routes = policy.get('routes', [])
        if '*' in routes or selected - modules.keys():
            raise ValueError('Unknown dependency roots')
        matches = lambda route: any(route['path'] == prefix or route['path'].startswith(prefix.rstrip('/') + '/') for prefix in routes)
        for hook in [] if mode == 'models' else hooks:
            for route in modules[hook]['routes']:
                if matches(route):
                    if route['opaque']:
                        raise ValueError('Opaque request scope')
                    selected.update(route['roots'])
        pending = list(selected)
        while pending:
            module = modules[pending.pop()]
            scopes = [module, *module.get('routes', [])] if mode == 'models' else [module]
            if any(scope['opaque'] for scope in scopes):
                raise ValueError('Opaque module scope')
            for name in {name for scope in scopes for name in scope['roots']} - selected:
                selected.add(name)
                pending.append(name)
        result = {name:value for name,value in sources.items() if name in selected or name not in modules}
        for hook in [] if mode == 'models' else hooks:
            module = modules[hook]
            hashes = [module['startup_hash'], *(route['hash'] for route in module.get('routes', []) if matches(route))]
            if any(not isinstance(value, str) or len(value) != 64 for value in hashes):
                raise ValueError('Missing executable scope hash')
            result[hook] = digest(hashes)
        result['sourceStructure'] = digest(sorted(sources))
        return result

    def descriptor(self, check, graph):
        if check.id not in self.descriptors:
            request = self.session.directory / ('dependencies-' + check.id + '.json')
            output = request.with_suffix('.result.json')
            tests = sorted({str(path.relative_to(self.project.root)) for pattern in check.definition['tests']
                            for path in self.project.root.glob(pattern) if path.is_file()})
            commands = self.project.command_files(check.definition['command'][1:])
            files = list(dict.fromkeys([*tests, *commands]))
            interpreter = 'bun' if Path(check.definition['command'][0]).name == 'bun' else 'node'
            implementation = Path(__file__).resolve().parents[1]
            environment = {name:digest(value) for name,value in self.session.environment(check.definition.get('env')).items()
                           if name not in {'TMPDIR','PWD','OLDPWD','SHLVL','_','PBGATE_CONTEXT','PBGATE_PROCESS_OWNER'}}
            base = digest({'files':files, 'structure':sorted(graph['modules']), 'sources':graph['sources'], 'outdir':graph['outdir'],
                'implementation':self.session.content.inventory(implementation / 'src',['dependencies.js','dependency-scan.js']),
                'parser':self.session.content.common_digest(implementation,['node_modules/typescript']),
                'interpreter':self.session.content.file(shutil.which(interpreter) or interpreter), 'environment':environment,
                'configuration':self.session.content.inventory(self.project.root,['package.json','bunfig.toml','jsconfig.json','tsconfig.json']),
                'installed':self.session.content.common_digest(self.project.root,['node_modules'])})
            stage = 'dependencies:' + check.id
            for _, receipt in self.session.receipts.candidates(stage):
                previous = receipt.get('descriptor', {})
                names = previous.get('files', {})
                if receipt.get('base') != base or previous.get('opaque') is not False or not isinstance(names,dict):
                    continue
                if any(not isinstance(name,str) or Path(name).is_absolute() or '..' in Path(name).parts for name in names):
                    continue
                if self.session.content.inventory(self.project.root,list(names)) == names and not set(previous.get('roots', [])) - graph['modules'].keys():
                    self.descriptors[check.id] = previous
                    return previous
            request.write_text(json.dumps({'root':str(self.project.root), 'graph':graph,
                                          'files':files, 'output':str(output)}))
            script = Path(__file__).resolve().parents[1] / 'src/dependency-scan.js'
            code, _ = self.session.run([interpreter, str(script), str(request)], self.project.root,
                                      self.session.logs / ('dependencies-' + check.id + '.log'), env=check.definition.get('env'), timeout=30)
            if code:
                raise ValueError('Dependency inspection failed')
            self.descriptors[check.id] = json.loads(output.read_text())
            descriptor = self.descriptors[check.id]
            if not descriptor['opaque']:
                key = 'dependencies-' + digest({'base':base,'descriptor':descriptor})
                self.session.receipts.save(key, {'stage':stage, 'base':base, 'descriptor':descriptor, 'metrics':{}}, self.project.root, [])
        return self.descriptors[check.id]

    def inputs(self, check, observations=None):
        policy, content, root = check.definition.get('dependencies'), self.session.content, self.project.root
        if not policy:
            return None
        try:
            graph, sources = self.graph(policy.get('outdir', 'public'), observations)
            descriptor = self.descriptor(check, graph)
            if descriptor['opaque']:
                raise ValueError('Opaque test scope')
            files = content.inventory(root, list(descriptor['files']), observations)
            if files != descriptor['files']:
                raise ValueError('Stale test scope')
            effective = dict(policy, roots=sorted(set(policy.get('roots', [])) | set(descriptor['roots'])))
            selected = self.select(graph, sources, effective)
            outputs = {module['output']:module['output_hash'] for module in graph['modules'].values()}
            complete = content.inventory(root, [graph['outdir']], observations)
            if {name for name in complete if name.endswith('.js')} != set(outputs):
                raise ValueError('Unregistered executable outside the compiler graph')
            assets = {name:value for name,value in complete.items()
                      if name not in outputs and name != graph['outdir'] + '/.pbgate-dependencies.json'}
            return {'files':dict(selected, **files, **assets), 'sources':graph['sources'], 'outdir':graph['outdir'], 'scope':'precise'}
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return {'files':{}, 'sources':None, 'outdir':None, 'scope':'full'}

    def preparation(self, outdir, observations=None):
        content, root = self.session.content, self.project.root
        complete = content.inventory(root, [outdir], observations)
        try:
            graph, sources = self.graph(outdir, observations)
            selected = self.select(graph, sources, {'routes':[], 'mode':'routes'})
            outputs = {module['output'] for module in graph['modules'].values()}
            if {name for name in complete if name.endswith('.js')} != outputs:
                return complete
            assets = {name:value for name,value in complete.items() if name not in outputs and name != outdir + '/.pbgate-dependencies.json'}
            return dict(selected, **assets)
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            return complete
