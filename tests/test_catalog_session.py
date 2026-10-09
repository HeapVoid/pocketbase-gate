import io
import json
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from test_catalog import sdk
from support import ReadyMonitor

class WorkerTests(unittest.TestCase):
    def test_worker_admission_does_not_consume_scenario_timeout(self):
        clock = {'at':0}
        def admit():
            clock['at'] += 20
            return 20
        session = sdk.CatalogSession('.', monitor=SimpleNamespace(admit=admit,error=None,summary=lambda start:{'peak_physical_bytes':0}))
        worker = SimpleNamespace(args=['worker'],stdin=io.StringIO(),stdout=io.StringIO(
            '{"operation":"admit"}\n{"operation":"admit"}\n{"code":0}\n'))
        with patch.object(sdk.catalog_session.time,'monotonic',side_effect=lambda:clock['at']), \
             patch.object(sdk.catalog_session.select,'select',return_value=([worker.stdout],[],[])) as ready:
            self.assertEqual(session.worker_reply(worker,{'operation':'run'},timeout=5),{'code':0,'admission_seconds':40})
        self.assertEqual([call.args[3] for call in ready.call_args_list],[.25,.25,.25])
        self.assertEqual([json.loads(line)['operation'] for line in worker.stdin.getvalue().splitlines()],['run','admitted','admitted'])

    def test_worker_is_lazy_reused_and_disposes_its_descendants(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            (root / 'worker.py').write_text('''import json,subprocess,sys
child=subprocess.Popen([sys.executable,'-c','import time;time.sleep(30)'])
for line in sys.stdin:
 request=json.loads(line)
 print(json.dumps({'code':0}),flush=True)
 if request['operation']=='close':
  child.terminate();child.wait();sys.stdin.read();break
''')
            protocol = {'runner':['-u','runner.py'],'worker':['-u','worker.py']}
            command = [sys.executable,'-u','runner.py','--check=one']
            with sdk.CatalogSession(root / '.pbgate',monitor=ReadyMonitor(),protocol=protocol,lock_directory=root / '.leases') as session:
                session.prepare_backend(root,[command])
                self.assertEqual(session._lifecycle_workers,{})
                self.assertEqual(session.run(command,root,root / 'one.log')[0],0)
                group = next(iter(session.monitor.groups))
                self.assertEqual(session.run(command,root,root / 'two.log')[0],0)
                self.assertEqual(set(session.monitor.groups),{group})
                guard = session._processes.guard
            self.assertIsNotNone(guard.returncode)
            self.assertEqual(set(session.monitor.groups),set())

    def test_default_timeout_bounds_an_undeclared_hang(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            with sdk.CatalogSession(root / '.pbgate',monitor=ReadyMonitor(),policy={'checkTimeoutSeconds':.1},lock_directory=root / '.leases') as session:
                with self.assertRaises(subprocess.TimeoutExpired):
                    session.run([sys.executable,'-c','while True: pass'],root,root / 'hang.log')
                self.assertEqual(set(session.monitor.groups),set())
