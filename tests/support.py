from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'engine'))
from runtime import Monitor, ProcessSession


class IdleReader:
    def read(self, groups=()):
        return {'at':time.monotonic(), 'cpu_idle_percent':100, 'memory_pressure':1,
                'swap_out_bytes':0, 'swap_out_bytes_per_second':0, 'physical_bytes':0,
                'rss_bytes':0, 'owned_cpu_seconds':0, 'groups':{}}


class ReadyMonitor(Monitor):
    def __init__(self):
        super().__init__(IdleReader())

    def admit(self):
        return 0


def session(root, profile='quiet'):
    return ProcessSession(Path(root) / '.pbgate', profile, monitor=ReadyMonitor(), lock_directory=Path(root) / '.leases')
