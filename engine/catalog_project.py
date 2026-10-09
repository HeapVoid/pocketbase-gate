"""Default project binding for advanced declarative catalogs."""
import hashlib
import json
import os
import platform
import subprocess
import sys
from pathlib import Path
from . import catalog as catalog_module
from .catalog import VerificationCatalog, recipe
from .catalog_inputs import stage_inputs
from .dependencies import CatalogDependencies
from .runtime import Content, atomic_json

class CatalogProject:
    def __init__(self, config):
        self.config = Path(config).resolve()
        self.root = self.config.parent
        document = json.loads(self.config.read_text())
        self.component = document.get('component', 'project')
        self.document = document
        self.state = self.root / document.get('state', '.pbgate')
        if self.state.resolve() == self.root or not self.state.resolve().is_relative_to(self.root):
            raise ValueError('Gate state must be below the project root')
        self.history = self.state / 'history'
        self.catalog = VerificationCatalog
        self.plan_catalog = VerificationCatalog.read(self.root, path=self.config)
        self.plan_catalog.validate_inventory(self.root)

    def assert_current(self):
        from . import assert_current
        assert_current()

    def git(self, repo, *arguments):
        return subprocess.check_output(['git', *arguments], cwd=repo)

    def parameters(self, profile):
        ignored = {'PWD','OLDPWD','SHLVL','_','TMPDIR','PBGATE_PROCESS_OWNER','PBGATE_PROFILE','PBGATE_SESSION','PBGATE_RECIPE_BINDINGS'}
        environment = {name:hashlib.sha256(value.encode()).hexdigest() for name,value in os.environ.items() if name not in ignored}
        environment['BIMBA_NO_TYPECHECK_DAEMON'] = hashlib.sha256(b'1').hexdigest()
        return {'profile':profile, 'platform':[platform.system(),platform.machine()], 'environment':environment,
                'tools':{name:subprocess.check_output([name,'--version'],text=True).strip() for name in dict.fromkeys(['python3','node',*self.document.get('tools',[])])}}

    def stages_for(self, repo, component):
        return self.plan_catalog.stages

    def stage_inputs(self, content, repo, component, stage, observations=None, lightweight=False):
        inputs = stage_inputs(content, repo, stage, CatalogDependencies, observations, lightweight)
        inputs['catalog'] = content.file(self.config, observations)
        inputs['gate_sdk'] = content.common_digest(Path(__file__).resolve().parent.parent, ['engine','src'], observations)
        return inputs

    def snapshot(self, repo, component, content=None, observations=None):
        content = content or Content()
        inputs = {stage['name']:self.stage_inputs(content, repo, component, stage, observations) for stage in self.plan_catalog.stages}
        return hashlib.sha256(json.dumps(inputs,sort_keys=True).encode()).hexdigest()

    def stamp_path(self, repo, component):
        return self.state / 'results' / (component + '.json')

    def write_stamp(self, path, result):
        atomic_json(path, result)

    def execution_options(self, component, stage, profile, force):
        options = {'env':stage.get('env', {})}
        if stage.get('timeout_seconds') is not None:
            options['timeout'] = stage['timeout_seconds']
        return options
