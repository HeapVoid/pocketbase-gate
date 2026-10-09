import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from support import ROOT, session
from gate import Project, Gate
from runtime import Content, Monitor, ReceiptCache, healthy


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        (self.root / 'src').mkdir()
        (self.root / 'src/value').write_text('original')

    def check(self, name, code, **fields):
        return dict(id=name, command=[sys.executable, '-c', code], inputs=['src'], **fields)

    def run_gate(self, checks, intent='release', selected=None, force=False, profile='quiet'):
        project = Project(self.root, {'format':1, 'checks':checks})
        with session(self.root, profile) as owned:
            code = Gate(project, project.plan(intent, selected), owned, force).run()
            report = json.loads((owned.logs / 'report.json').read_text())
        return code, report

    def test_exact_cache_restores_outputs_and_invalidates_changed_inputs(self):
        checks = [self.check('compile', "from pathlib import Path; p=Path('out'); p.mkdir(exist_ok=True); (p/'value').write_text(Path('src/value').read_text())",
                             kind='prepare', outputs=['out'], restore=True),
                  self.check('behavior', "from pathlib import Path; assert Path('out/value').read_text() == Path('src/value').read_text()", requires=['compile'])]
        code, first = self.run_gate(checks)
        self.assertEqual(code, 0)
        self.assertFalse(any(check['cached'] for check in first['checks']))
        import shutil
        shutil.rmtree(self.root / 'out')
        code, second = self.run_gate(checks)
        self.assertEqual(code, 0)
        self.assertTrue(all(check['cached'] for check in second['checks']))
        (self.root / 'src/value').write_text('changed')
        code, third = self.run_gate(checks)
        self.assertEqual(code, 0)
        self.assertFalse(any(check['cached'] for check in third['checks']))

    def test_dev_runs_tests_fresh_but_reuses_preparation(self):
        checks = [self.check('prepare', 'pass', kind='prepare'), self.check('test', 'pass', requires=['prepare'])]
        self.assertEqual(self.run_gate(checks)[0], 0)
        code, report = self.run_gate(checks, 'dev', ['test'])
        self.assertEqual(code, 0)
        self.assertFalse(report['completeGate'])
        self.assertEqual([(check['id'], check['cached']) for check in report['checks']], [('prepare', True), ('test', False)])

    def test_input_mutation_and_missing_output_never_publish_success(self):
        checks = [self.check('mutate', "from pathlib import Path; Path('src/value').write_text('mutated')")]
        code, report = self.run_gate(checks)
        self.assertEqual(code, 2)
        self.assertFalse(report['stable'])
        self.assertFalse(list((self.root / '.pbgate/cache').glob('*/receipt.json')))
        code, report = self.run_gate([self.check('missing', 'pass', outputs=['missing'])])
        self.assertEqual(code, 2)
        self.assertFalse(report['passed'])

    def test_later_failure_keeps_only_proven_completed_receipts(self):
        checks = [self.check('good', 'pass'), self.check('bad', 'raise SystemExit(7)', requires=['good']), self.check('later', 'pass', requires=['bad'])]
        code, report = self.run_gate(checks)
        self.assertEqual(code, 7)
        self.assertEqual(report['skipped'], ['later'])
        self.assertEqual(len(list((self.root / '.pbgate/cache').glob('*/receipt.json'))), 1)

    def test_timeout_disposes_grandchildren_and_preserves_unowned_process(self):
        outsider = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'], start_new_session=True)
        self.addCleanup(lambda: (outsider.terminate(), outsider.wait()) if outsider.poll() is None else None)
        program = "import subprocess,sys,time; from pathlib import Path; child=subprocess.Popen([sys.executable,'-c','import time; time.sleep(30)']); Path('child.pid').write_text(str(child.pid)); time.sleep(30)"
        code, report = self.run_gate([self.check('hung', program, timeoutSeconds=.5)])
        self.assertEqual(code, 124)
        self.assertIsNone(outsider.poll())
        self.assertFalse(list((self.root / '.pbgate').glob('run-*')))
        self.assertFalse(list((self.root / '.pbgate/cache').glob('*/receipt.json')))
        pid = int((self.root / 'child.pid').read_text())
        row = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not row or row.startswith('Z'))

    def test_fast_scheduler_overlaps_independent_checks_and_respects_dependencies(self):
        program = "import time; from pathlib import Path; name='{name}'; Path(name+'.start').write_text(str(time.time_ns())); time.sleep(.3); Path(name+'.end').write_text(str(time.time_ns()))"
        checks = [self.check('a', program.format(name='a')), self.check('b', program.format(name='b')),
                  self.check('c', program.format(name='c'), requires=['a', 'b'])]
        self.assertEqual(self.run_gate(checks, profile='fast')[0], 0)
        stamp = lambda name: float((self.root / name).read_text())
        self.assertLess(max(stamp('a.start'), stamp('b.start')), min(stamp('a.end'), stamp('b.end')))
        self.assertGreater(stamp('c.start'), max(stamp('a.end'), stamp('b.end')))

    def test_metadata_cache_detects_restored_mtime_and_directory_additions(self):
        content = Content()
        path = self.root / 'src/value'
        original = content.file(path)
        info = path.stat()
        path.write_text('modified')
        os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
        self.assertNotEqual(original, content.file(path))
        checkpoint = content.checkpoint(lambda observed: content.common_digest(self.root, ['src'], observed))
        (self.root / 'src/added').write_text('new')
        self.assertFalse(checkpoint.finish()['stable'])

    def test_artifact_restore_rechecks_destination_before_removing_outputs(self):
        import shutil
        output = self.root / 'build/hooks'
        output.mkdir(parents=True)
        (output / 'value').write_text('generated')
        cache = ReceiptCache(self.root / '.pbgate/cache')
        cache.save('proof', {'metrics':{}}, self.root, ['build/hooks'], artifacts=True)
        shutil.rmtree(self.root / 'build')
        with tempfile.TemporaryDirectory() as outside:
            external = Path(outside) / 'hooks'
            external.mkdir()
            (external / 'value').write_text('maintained')
            (self.root / 'build').symlink_to(outside)
            self.assertFalse(cache.restore('proof', self.root, ['build/hooks']))
            self.assertEqual((external / 'value').read_text(), 'maintained')

    def test_resource_policy_requires_healthy_samples_and_bounds_wait(self):
        sample = {'cpu_idle_percent':31, 'memory_pressure':1, 'swap_out_bytes_per_second':0}
        self.assertTrue(healthy(sample))
        self.assertFalse(healthy(dict(sample, memory_pressure=4)))
        monitor = Monitor(policy={'admissionTimeoutSeconds':.01})
        with self.assertRaisesRegex(RuntimeError, 'timed out'):
            monitor.admit()

    def test_interrupted_executor_cannot_publish_an_empty_success(self):
        project = Project(self.root, {'format':1, 'checks':[self.check('cancel', 'pass')]})
        with session(self.root) as owned:
            runner = Gate(project, project.plan(), owned)
            with patch.object(runner, 'execute', side_effect=KeyboardInterrupt('cancelled')):
                with self.assertRaises(KeyboardInterrupt):
                    runner.run()
            report = json.loads((owned.logs / 'report.json').read_text())
            self.assertFalse(report['passed'])
            self.assertFalse(list((self.root / '.pbgate/cache').glob('*/receipt.json')))

    def test_forced_execution_repeats_cached_checks(self):
        checks = [self.check('test', 'pass')]
        self.assertEqual(self.run_gate(checks)[0], 0)
        code, report = self.run_gate(checks, force=True)
        self.assertEqual(code, 0)
        self.assertFalse(report['checks'][0]['cached'])

    def test_loss_of_supervisor_cleans_owned_children_and_releases_lease(self):
        driver = "import sys,time; from pathlib import Path; sys.path.insert(0,sys.argv[1]); from support import session; root=Path(sys.argv[2]);\nwith session(root) as owned:\n child=owned.spawn([sys.executable,'-c','import time; time.sleep(30)'],root,sys.stdout); (root/'owned.pid').write_text(str(child.pid)); time.sleep(30)\n"
        owner = subprocess.Popen([sys.executable, '-c', driver, str(ROOT / 'tests'), str(self.root)], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: (owner.kill(), owner.wait()) if owner.poll() is None else None)
        deadline = time.monotonic() + 5
        while not (self.root / 'owned.pid').exists() and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertTrue((self.root / 'owned.pid').exists())
        pid = int((self.root / 'owned.pid').read_text())
        owner.kill()
        owner.wait()
        deadline = time.monotonic() + 5
        while list((self.root / '.pbgate').glob('run-*')) and time.monotonic() < deadline:
            time.sleep(.05)
        self.assertFalse(list((self.root / '.pbgate').glob('run-*')))
        row = subprocess.run(['ps', '-p', str(pid), '-o', 'stat='], capture_output=True, text=True).stdout.strip()
        self.assertTrue(not row or row.startswith('Z'))
        import fcntl
        with (self.root / '.leases/machine.lock').open() as lease:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
