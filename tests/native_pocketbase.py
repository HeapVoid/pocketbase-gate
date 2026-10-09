import json
import os
from pathlib import Path
import tempfile
import unittest
from support import ROOT, session
from gate import Project, Gate

BINARY = os.environ.get('PBGATE_TEST_BINARY')
BIMBA = os.environ.get('PBGATE_TEST_BIMBA')
CONTEXT = (ROOT / 'src/context.js').as_uri()


@unittest.skipUnless(BINARY and Path(BINARY).is_file(), 'Set PBGATE_TEST_BINARY to a PocketBase 0.40+ binary')
class NativePocketBaseTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'migrations').mkdir()
        (self.root / 'migrations/1000000000_notes.js').write_text("migrate(app => { app.save(new Collection({name:'notes',type:'base',fields:[new TextField({name:'text'})]})); });")

    def check(self, name, source, **fields):
        (self.root / (name + '.mjs')).write_text("import assert from 'node:assert/strict'; import {TestContext} from '" + CONTEXT + "'; const context=TestContext.current();\n" + source)
        return dict(id=name, command=['node', name + '.mjs'], inputs=[name + '.mjs'], fixture='api', **fields)

    def run_gate(self, checks, fixture=None):
        definition = dict(binary=BINARY, migrations='migrations', **(fixture or {}))
        project = Project(self.root, {'format':1, 'fixtures':{'api':definition}, 'checks':checks})
        with session(self.root) as owned:
            code = Gate(project, project.plan(), owned).run()
            report = json.loads((owned.logs / 'report.json').read_text())
        return code, report

    def test_shared_fixture_restores_records_store_and_storage(self):
        (self.root / 'hooks').mkdir()
        (self.root / 'hooks/state.pb.js').write_text("onBootstrap(e=>{e.app.store().set('fixture.counter',1);e.next();}); routerAdd('GET','/api/state',e=>e.json(200,{counter:e.app.store().get('fixture.counter')})); routerAdd('POST','/api/state',e=>{e.app.store().set('fixture.counter',9);return e.json(200,{});});")
        (self.root / 'seed.mjs').write_text("import {TestContext} from '" + CONTEXT + "'; import {mkdir,writeFile} from 'node:fs/promises'; const path=TestContext.current().dataDirectory+'/storage'; await mkdir(path,{recursive:true}); await writeFile(path+'/baseline','seed');")
        source = "import {readFile,writeFile,readdir} from 'node:fs/promises'; assert.equal((await context.request('/api/collections/notes/records')).totalItems,0); assert.equal((await context.request('/api/state')).counter,1); assert.deepEqual(await readdir(context.dataDirectory+'/storage'),['baseline']); assert.equal(await readFile(context.dataDirectory+'/storage/baseline','utf8'),'seed'); await context.request('/api/state',{body:{}}); await writeFile(context.dataDirectory+'/storage/scenario','changed'); await writeFile(context.dataDirectory+'/storage/baseline','changed'); await context.request('/api/collections/notes/records',{body:{text:'scenario'}});"
        checks = [self.check('first', source, isolation='shared'), self.check('second', source, isolation='shared')]
        fixture = {'hooks':'hooks', 'seed':['node', 'seed.mjs']}
        code, report = self.run_gate(checks, fixture)
        self.assertEqual(code, 0, report)
        self.assertEqual([item['metrics']['fixture']['reused'] for item in report['checks']], [False, True])
        self.assertFalse(list((self.root / '.pbgate').glob('run-*')))
        code, repeat = self.run_gate(checks, fixture)
        self.assertEqual(code, 0)
        self.assertTrue(all(item['cached'] for item in repeat['checks']))

    def test_fresh_isolation_keeps_schema_mutation_local(self):
        checks = [self.check('delete', "const collection=await context.request('/api/collections/notes'); await context.request('/api/collections/'+collection.id,{method:'DELETE'});"),
                  self.check('fresh', "assert.equal((await context.request('/api/collections/notes/records')).totalItems,0);", requires=['delete'])]
        code, report = self.run_gate(checks)
        self.assertEqual(code, 0, report)
        self.assertTrue(all(not item['metrics']['fixture']['reused'] for item in report['checks']))
        self.assertEqual([item['metrics']['fixture']['baseline']['hit'] for item in report['checks']], [False,True])

    def test_stopped_baseline_is_reused_verified_and_invalidated(self):
        checks = [self.check('baseline', "assert.equal((await context.request('/api/collections/notes/records')).totalItems,0);", cache=False)]
        first_code, first = self.run_gate(checks)
        self.assertEqual(first_code,0,first)
        _, second = self.run_gate(checks)
        self.assertTrue(second['checks'][0]['metrics']['fixture']['baseline']['hit'])
        key = second['checks'][0]['metrics']['fixture']['baseline']['key']
        database = self.root / '.pbgate/cache' / key / 'data/data.db'
        with database.open('ab') as output:
            output.write(b'corrupted')
        code, restored = self.run_gate(checks)
        self.assertEqual(code,0,restored)
        self.assertFalse(restored['checks'][0]['metrics']['fixture']['baseline']['hit'])
        with (self.root / 'migrations/1000000000_notes.js').open('a') as output:
            output.write('\n// Changed migration input\n')
        code, changed = self.run_gate(checks)
        self.assertEqual(code,0,changed)
        self.assertNotEqual(key,changed['checks'][0]['metrics']['fixture']['baseline']['key'])

    def test_first_boot_scenarios_can_disable_prepared_baselines(self):
        checks = [self.check('firstboot','assert.ok(true);',cache=False)]
        for _ in range(2):
            code, report = self.run_gate(checks, {'baseline':False})
            self.assertEqual(code,0,report)
            self.assertIsNone(report['checks'][0]['metrics']['fixture']['baseline']['key'])

    def test_schema_mutation_rejects_shared_success(self):
        checks = [self.check('mutate', "const collection=await context.request('/api/collections/notes'); await context.request('/api/collections/'+collection.id,{method:'DELETE'});", isolation='shared')]
        code, report = self.run_gate(checks)
        self.assertEqual(code, 2)
        self.assertFalse(report['passed'])
        self.assertFalse(list((self.root / '.pbgate/cache').glob('*/receipt.json')))

    @unittest.skipUnless(BIMBA and Path(BIMBA).is_dir(), 'Set PBGATE_TEST_BIMBA to installed bimba-cli')
    def test_imba_compilation_runs_in_a_separate_consumer_project(self):
        (self.root / 'src').mkdir()
        (self.root / 'node_modules').mkdir()
        (self.root / 'node_modules/bimba-cli').symlink_to(BIMBA)
        (self.root / 'package.json').write_text('{"type":"module"}')
        (self.root / 'src/message.imba').write_text('const message = {text: "Hello from Imba"}\nexport default message\n')
        (self.root / 'src/api.pb.imba').write_text('routerAdd "GET", "/api/message", do(e)\n\tconst message = require("{__hooks}/message.js")\n\te.json(200, {text: message.text})\n')
        (self.root / 'compile.mjs').write_text("import {compileHooks} from '" + (ROOT / 'src/imba.js').as_uri() + "'; await compileHooks();\n")
        compile_check = dict(id='compile', kind='prepare', command=['bun', 'compile.mjs'], inputs=['src', 'compile.mjs'], outputs=['public'])
        behavior = self.check('behavior', "assert.deepEqual(await context.request('/api/message'),{text:'Hello from Imba'});", requires=['compile'])
        code, report = self.run_gate([compile_check, behavior], {'hooks':'public'})
        self.assertEqual(code, 0, report)
        self.assertTrue((self.root / 'public/api.pb.js').is_file())
        (self.root / 'public/maintained.txt').write_text('asset')
        (self.root / 'src/api.pb.imba').unlink()
        self.assertEqual(self.run_gate([compile_check])[0], 0)
        self.assertFalse((self.root / 'public/api.pb.js').exists())
        self.assertEqual((self.root / 'public/maintained.txt').read_text(), 'asset')

    def test_fixture_preparation_obeys_the_enclosing_timeout(self):
        (self.root / 'seed.mjs').write_text('await new Promise(resolve=>setTimeout(resolve,30000));')
        checks = [self.check('timed', 'assert.ok(true);', timeoutSeconds=1.5)]
        code, report = self.run_gate(checks, {'seed':['node','seed.mjs']})
        self.assertEqual(code, 124, report)
        self.assertFalse(list((self.root / '.pbgate').glob('run-*')))

    @unittest.skipUnless(BIMBA and Path(BIMBA).is_dir(), 'Set PBGATE_TEST_BIMBA to installed bimba-cli')
    def test_independent_callback_edits_preserve_exact_test_and_schema_proofs(self):
        import shutil
        (self.root / 'src').mkdir()
        package = self.root / 'node_modules/pocketbase-gate'
        package.mkdir(parents=True)
        shutil.copyfile(ROOT / 'src/context.js',package / 'context.js')
        (package / 'package.json').write_text('{"type":"module","exports":"./context.js"}')
        (self.root / 'node_modules/bimba-cli').symlink_to(BIMBA)
        (self.root / 'package.json').write_text('{"type":"module"}')
        source = 'routerAdd "GET", "/api/alpha", do(e)\n\te.json(200, {value: $app.store().get("override") || "alpha"})\nrouterAdd "GET", "/api/beta", do(e)\n\te.json(200, {value: "VALUE"})\n'
        untracked = self.root / 'src/untracked.imba'
        untracked.write_text('export default {value: "alpha"}\n')
        hooks = self.root / 'src/api.pb.imba'
        hooks.write_text(source.replace('VALUE','before'))
        (self.root / 'compile.mjs').write_text("import {compileHooks} from '" + (ROOT / 'src/imba.js').as_uri() + "'; await compileHooks();")
        compile_check = dict(id='compile',kind='prepare',command=['bun','compile.mjs'],inputs=['src','compile.mjs'],outputs=['public'])
        alpha = self.check('alpha',"assert.equal((await context.request('/api/alpha')).value,'alpha');",requires=['compile'],dependencies={'routes':['/api/alpha']})
        (self.root / 'alpha.mjs').write_text((self.root / 'alpha.mjs').read_text().replace(CONTEXT,'pocketbase-gate'))
        beta = self.check('beta',"assert.ok((await context.request('/api/beta')).value);",requires=['compile'],dependencies={'routes':['/api/beta']},cache=False)
        (self.root / 'beta.mjs').write_text((self.root / 'beta.mjs').read_text().replace(CONTEXT,'pocketbase-gate'))
        fixture = {'hooks':'public','baseline':'schema'}
        code, first = self.run_gate([compile_check,alpha,beta],fixture)
        self.assertEqual(code,0,first)
        key = next(item for item in first['checks'] if item['id']=='beta')['metrics']['fixture']['baseline']['key']
        hooks.write_text(source.replace('VALUE','after'))
        code, second = self.run_gate([compile_check,alpha,beta],fixture)
        self.assertEqual(code,0,second)
        by_id = {item['id']:item for item in second['checks']}
        self.assertTrue(by_id['alpha']['cached'],second)
        self.assertEqual(key,by_id['beta']['metrics']['fixture']['baseline']['key'])
        self.assertTrue(by_id['beta']['metrics']['fixture']['baseline']['hit'])
        # A maintained hook outside the compiler graph can load any module.
        external = self.root / 'public/external.pb.js'
        external.write_text('onBootstrap(function(e) {e.next(); $app.store().set("override", require(__hooks+"/untracked.js").value);});')
        code, extended = self.run_gate([compile_check,alpha],fixture)
        self.assertEqual(code,0,extended)
        untracked.write_text('export default {value: "changed"}\n')
        code, unregistered = self.run_gate([compile_check,alpha],fixture)
        self.assertFalse(next(item for item in unregistered['checks'] if item['id']=='alpha')['cached'],unregistered)
        self.assertNotEqual(code,0,unregistered)
        external.unlink()
        # Unknown/dynamic reads invalidate the narrow proof and execute fresh.
        with (self.root / 'alpha.mjs').open('a') as output:
            output.write("\nimport {readFileSync} from 'node:fs'; if(process.env.UNUSED_INPUT) readFileSync(process.env.UNUSED_INPUT);\n")
        code, third = self.run_gate([compile_check,alpha],fixture)
        self.assertEqual(code,0,third)
        self.assertFalse(third['checks'][-1]['cached'])
