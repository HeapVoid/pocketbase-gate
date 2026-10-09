import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from support import ROOT, ReadyMonitor

name = '_catalog_test_sdk'
spec = importlib.util.spec_from_file_location(name, ROOT / 'engine/__init__.py', submodule_search_locations=[str(ROOT / 'engine')])
sdk = importlib.util.module_from_spec(spec)
sys.modules[name] = sdk
spec.loader.exec_module(sdk)

class CatalogTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        subprocess.run(['git','init','-q',str(self.root)],check=True)
        subprocess.run(['git','-C',str(self.root),'-c','user.name=Test','-c','user.email=test@example.test','commit','--allow-empty','-qm','fixture'],check=True)
        (self.root / 'test').mkdir()
        (self.root / 'test/a.py').write_text('valid a')
        (self.root / 'test/b.py').write_text('valid b')
        (self.root / 'runner.py').write_text("from pathlib import Path\nimport json,sys\nwith Path('.pbgate/calls').open('a') as output: output.write(json.dumps(sys.argv[1:])+'\\n')\nfor name in sys.argv[1:]: assert Path(name).read_text().startswith('valid')\n")
        self.config = self.root / 'pbgate.json'
        self.config.write_text(json.dumps({'format':2,'component':'example','stages':[
            {'name':'files','group':'models','command':[sys.executable,'runner.py'], 'inputs':['runner.py'],
             'outputs':[], 'tests':['test/*.py'], 'file_partition':'tests','intents':['dev','release']}],
            'test_inventory':{'patterns':['test/*.py']}}))

    def run_catalog(self):
        project = sdk.CatalogProject(self.config)
        with sdk.CatalogSession(project.state, monitor=ReadyMonitor(), lock_directory=self.root / '.leases') as session:
            code = sdk.CatalogGate(project).verify(self.root,project.component,session,plan=project.plan_catalog.plan())
            report = json.loads(session.last_report_path.read_text())
        return code, report

    def test_discovered_files_have_independent_proofs_and_one_shared_command(self):
        code, first = self.run_catalog()
        self.assertEqual(code,0)
        self.assertEqual(len(first['stages']),2)
        self.assertEqual(len((self.root / '.pbgate/calls').read_text().splitlines()),1)
        self.assertTrue(all(stage['file_batch']['checks'] == ['files.test.a.py','files.test.b.py'] for stage in first['stages']))
        code, reused = self.run_catalog()
        self.assertEqual(code,0)
        self.assertTrue(all(stage['cache_hit'] for stage in reused['stages']))
        (self.root / 'test/a.py').write_text('valid changed a')
        code, changed = self.run_catalog()
        self.assertEqual(code,0)
        self.assertEqual([stage['name'] for stage in changed['stages'] if not stage['cache_hit']],['files.test.a.py'])
        calls = [json.loads(line) for line in (self.root / '.pbgate/calls').read_text().splitlines()]
        self.assertEqual(calls,[['test/a.py','test/b.py'],['test/a.py']])
        (self.root / 'test/c.py').write_text('valid new c')
        code, added = self.run_catalog()
        self.assertEqual(code,0)
        self.assertEqual([stage['name'] for stage in added['stages'] if not stage['cache_hit']],['files.test.c.py'])

    def test_failed_batch_cannot_publish_success_receipts(self):
        (self.root / 'test/b.py').write_text('invalid')
        code, report = self.run_catalog()
        self.assertNotEqual(code,0)
        self.assertFalse(report['passed'])
        self.assertFalse(list((self.root / '.pbgate/cache').glob('*/receipt.json')))

    def test_cli_planning_does_not_allocate_runtime_state(self):
        result = subprocess.run([sys.executable,str(ROOT / 'engine/gate.py'),'plan','--config',str(self.config)],text=True,capture_output=True)
        self.assertEqual(result.returncode,0,result.stderr)
        self.assertEqual(len(json.loads(result.stdout)['checks']),2)
        self.assertFalse((self.root / '.pbgate').exists())

    def test_queued_plans_reject_a_changed_catalog_or_discovered_inventory(self):
        project = sdk.CatalogProject(self.config)
        plan = project.plan_catalog.plan()
        (self.root / 'test/c.py').write_text('new test')
        with self.assertRaisesRegex(RuntimeError,'inventory changed'):
            plan.assert_current()
        (self.root / 'test/c.py').unlink()
        self.config.write_text(self.config.read_text() + ' ')
        with self.assertRaisesRegex(RuntimeError,'catalog changed'):
            plan.assert_current()
