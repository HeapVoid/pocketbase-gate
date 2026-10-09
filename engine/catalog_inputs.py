"""Recipe and dependency inputs shared by all catalog consumers."""
import hashlib
import json
import re
from pathlib import Path
from .catalog import recipe, digest, package_inputs

def stage_inputs(content, repo, stage, dependencies, observations=None, lightweight=False):
    declared = list(stage['inputs'])
    # The executed recipe owns its test list. Bind those bytes even when a
    # source-read manifest is absent; domain checks need no global test glob.
    for step in recipe(stage):
        declared.extend(argument for argument in step if argument.startswith(('tests/', 'test/')))
    if stage.get('package_script'):
        scripts = json.loads((repo / 'package.json').read_text())['scripts']
        declared.extend(path.strip('"\'') for path in re.findall(r'\btests/[^\s;&]+', scripts[stage['package_script']]))
    source_inputs = stage.get('source_inputs', [])
    structure = sorted(content.common_inventory(repo, source_inputs, excluded=stage.get('source_exclude', []))) if source_inputs else []
    manifest = repo / stage.get('source_manifest', '__no_source_manifest__')
    try:
        metadata = json.loads(manifest.read_text())
        expected = hashlib.sha256(json.dumps({'steps': stage.get('steps'), 'build': stage.get('build')}, separators=(',', ':')).encode()).hexdigest()
        names = metadata['inputs']
        if metadata.get('format') != 1 or metadata.get('recipe_hash') != expected or not names or any(Path(name).is_absolute() or '..' in Path(name).parts for name in names):
            raise ValueError('Untrusted compiler manifest')
        declared.extend(names)
    except (OSError, ValueError, KeyError, TypeError):
        declared.extend(source_inputs)
        declared.extend(stage.get('fallback_inputs', []))
    inputs = content.inventory(repo, declared + ['bunfig.toml', '.bun-version', '.node-version',
                              'tsconfig*.json', 'jsconfig.json', '.env', '.env.local', '.env.test'], observations,
                               excluded=stage.get('test_exclude', []))
    inputs['recipe'] = digest({'steps': recipe(stage), 'build': stage.get('build'),
                             'members': stage.get('members'), 'tests': stage.get('tests'), 'test_exclude': stage.get('test_exclude'),
                             'inputs': stage['inputs'], 'source_inputs': source_inputs, 'fallback_inputs': stage.get('fallback_inputs', []), 'source_exclude': stage.get('source_exclude', []),
                             'external_inputs': stage.get('external_inputs'), 'outputs': stage.get('outputs', [])})
    inputs.update(content.inventory(repo, stage.get('tests', []), observations, excluded=stage.get('test_exclude', [])))
    if stage.get('backend_runtime'):
        inputs.update(dependencies.inputs(content, repo, stage['backend_runtime'], observations))
        inputs['backend_runtime_recipe'] = digest(stage['backend_runtime'])
    if stage.get('backend_models'):
        inputs.update(dependencies.model_inputs(content, repo, stage, observations))
    if source_inputs:
        # New/deleted files can affect resolution or wildcard entrypoints even
        # when a previous compiler manifest did not list them.
        inputs['source_structure'] = digest(structure)
    inputs.update({'package:' + key: value for key, value in package_inputs(repo, stage).items()})
    # Installed package bytes, rather than a lockfile alone, detect locally
    # modified dependencies. Memoized file hashes avoid rereading them per stage.
    if not lightweight:
        inputs['installed_dependencies'] = content.common_digest(repo, ['node_modules'], observations)
    return inputs
