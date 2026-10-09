"""Resource supervision and exact content receipts for owned gate processes.

Extracted from HeapVoid's existing verification runtime. Project-specific
compilation, routes, environment rules and fixtures live outside this module.
"""
import ctypes
from datetime import datetime, timezone
import fcntl
import hashlib
import json
import os
from pathlib import Path
import platform
import select
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time


PROFILES = {'quiet': {'nice': 10, 'qos': 'background', 'workers': 1},
            'fast': {'nice': 5, 'qos': 'utility', 'workers': 2}}
SAMPLE_SECONDS = 2
CACHE_LIMIT = 2 * 1024**3
CONTENT_FILE_LIMIT = 200_000


def atomic_json(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + '.' + str(time.time_ns()) + '.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def schedule(command, profile):
    policy = PROFILES[profile]
    arguments = ['nice', '-n', str(policy['nice']), *map(str, command)]
    if platform.system() == 'Darwin':
        # Background CPU/I/O is reserved for heavy checks and SDK copying;
        # controllers perform fingerprints independently of that policy.
        arguments = (['taskpolicy', '-b', '-c', 'background'] if profile == 'quiet'
                     else ['taskpolicy', '-c', 'utility']) + arguments
    return arguments


class Content:
    """Hash relative names and bytes, including additions, removals and links."""
    def __init__(self, cache_file=None):
        self.memo = {}
        self._inventories = {}
        self._digests = {}
        self.cache_file = Path(cache_file) if cache_file else None
        self.used = set()
        if self.cache_file:
            try:
                record = json.loads(self.cache_file.read_text())
                files = record['files']
                if isinstance(files, dict) and record['format'] == 1 and record['digest'] == hashlib.sha256(json.dumps(files, separators=(',', ':')).encode()).hexdigest():
                    self.memo = {name: (tuple(value[0]), value[1]) for name, value in files.items()
                                 if isinstance(value, list) and len(value) == 2 and isinstance(value[0], list)
                                 and len(value[0]) == 5 and all(isinstance(part, int) for part in value[0])
                                 and isinstance(value[1], str) and len(value[1]) == 64}
            except (OSError, ValueError, KeyError, TypeError):
                pass

    def persist(self):
        if not self.cache_file:
            return
        active = sorted(self.used.intersection(self.memo))[:CONTENT_FILE_LIMIT]
        files = {name: self.memo[name] for name in active}
        # A targeted session must not evict the full gate's unchanged inputs.
        # Retain prior signatures within a bounded metadata budget, prioritizing
        # the current session; every later reuse still stats its actual path.
        for name, value in self.memo.items():
            if len(files) >= CONTENT_FILE_LIMIT:
                break
            files.setdefault(name, value)
        encoded = json.dumps(files, separators=(',', ':'))
        self.cache_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.cache_file.with_name(self.cache_file.name + '.' + str(time.time_ns()) + '.tmp')
        temporary.write_text('{"format":1,"digest":' + json.dumps(hashlib.sha256(encoded.encode()).hexdigest()) + ',"files":' + encoded + '}\n')
        temporary.replace(self.cache_file)

    def common_inventory(self, root, patterns, observations=None, excluded=()):
        key = (str(root), tuple(patterns), tuple(excluded))
        if key not in self._inventories:
            observed = {}
            self._inventories[key] = self.inventory(root, patterns, observed, excluded), observed
        values, observed = self._inventories[key]
        if observations is not None:
            observations.update(observed)
        return values

    def common_digest(self, root, patterns, observations=None):
        values = self.common_inventory(root, patterns, observations)
        key = (str(root), tuple(patterns))
        if key not in self._digests:
            self._digests[key] = hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()
        return self._digests[key]

    def checkpoint(self, reader=lambda observed: None):
        return ContentCheckpoint(self, reader)

    def file(self, path, observations=None, entry=None):
        path = os.fspath(path)
        try:
            info = entry.stat() if entry is not None else os.stat(path)
        except FileNotFoundError:
            if observations is not None:
                observations[path] = None
            return 'missing'
        signature = (info.st_dev, info.st_ino, info.st_size, info.st_mtime_ns, info.st_ctime_ns)
        key = path
        self.used.add(key)
        if observations is not None:
            observations[key] = signature
        if stat.S_ISDIR(info.st_mode):
            # Git can list a directory symlink or a submodule as one entry.
            # Never memoize only its mtime: nested file writes do not change it.
            return hashlib.sha256(json.dumps(self.inventory(path, ['.'], observations), sort_keys=True).encode()).hexdigest()
        if self.memo.get(key, (None,))[0] == signature:
            return self.memo[key][1]
        digest = hashlib.sha256()
        with open(path, 'rb') as source:
            for chunk in iter(lambda: source.read(1024 * 1024), b''):
                digest.update(chunk)
        value = digest.hexdigest()
        self.memo[key] = signature, value
        return value

    def inventory(self, root, patterns, observations=None, excluded=()):
        root = Path(root)
        root_name = str(root)
        result = {}
        prefixes = tuple(prefix.rstrip('/') + '/' for prefix in excluded)
        def visit(path, name, ancestors=(), entry=None, resolved=None):
            if name in excluded or name.startswith(prefixes):
                return
            is_directory = entry.is_dir() if entry is not None else os.path.isdir(path)
            if is_directory:
                resolved = os.path.realpath(path) if resolved is None or (entry is not None and entry.is_symlink()) else resolved
                if resolved in ancestors:
                    result[name] = 'directory-cycle'
                    return
                with os.scandir(path) as directory:
                    entries = sorted(directory, key=lambda item: item.name)
                for item in entries:
                    if item.name not in ('__pycache__', '.git'):
                        visit(item.path, item.name if name == '.' else name + '/' + item.name,
                              (*ancestors, resolved), item, os.path.join(resolved, item.name))
            else:
                result[name] = self.file(path, observations, entry)
        for pattern in patterns:
            paths = sorted(root.glob(pattern)) if any(c in pattern for c in '*?[') else [root / pattern]
            if not paths:
                result[pattern] = 'missing'
            for path in paths:
                visit(str(path), os.path.relpath(path, root_name))
        return result


class ContentCheckpoint:
    """Own the fresh membership pass and byte/signature comparison."""
    def __init__(self, content, reader):
        self.content, self.reader = content, reader
        content._inventories, content._digests = {}, {}
        self.observed = {}
        self.protected = {}
        self.before = reader(self.observed)

    def protect(self, observations):
        for path, signature in observations.items():
            self.protected.setdefault(path, signature)

    def finish(self):
        content = self.content
        inventories = content._inventories
        content._inventories, content._digests = {}, {}
        observed = {}
        current = self.reader(observed)
        for path in self.protected:
            if path not in observed:
                content.file(path, observed)
        expected = dict(self.protected, **self.observed)
        changed = {path for path in expected.keys() | observed.keys()
                   if expected.get(path) != observed.get(path)}
        stable = current == self.before and not changed
        for (root, patterns, excluded), (values, before) in inventories.items():
            after = {}
            refreshed = content.common_inventory(Path(root), patterns, after, excluded)
            if values != refreshed or before != after:
                stable = False
                changed.update(path for path in before.keys() | after.keys() if before.get(path) != after.get(path))
        content.persist()
        return {'snapshot': current, 'stable': stable, 'changed_inputs': sorted(changed)}


class SystemSample:
    def __init__(self):
        self.previous = None
        if platform.system() == 'Darwin':
            self.lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib')
            self.proc = ctypes.CDLL('/usr/lib/libproc.dylib')
            timebase = (ctypes.c_uint32 * 2)()
            if self.lib.mach_timebase_info(ctypes.byref(timebase)) or not timebase[1]:
                raise OSError('Mach timebase sampling failed')
            self.cpu_tick_seconds = timebase[0] / timebase[1] / 1e9

    def resources(self, groups=()):
        footprint = rss = cpu = 0
        owned = {group: {'physical_bytes': 0, 'rss_bytes': 0, 'cpu_seconds': 0} for group in groups}
        if platform.system() == 'Darwin':
            # rusage_info_v2 begins with uuid[16], then 18 uint64 fields.
            rows = [tuple(map(int, row.split())) for row in subprocess.check_output(['ps', '-axo', 'pid=,pgid=,ppid='], text=True).splitlines()]
            parents = {pid: parent for pid, _, parent in rows}
            def descendant(pid):
                seen = set()
                while pid and pid not in seen:
                    if pid == os.getpid():
                        return True
                    seen.add(pid)
                    pid = parents.get(pid, 0)
                return False
            for pid, group, _ in rows:
                if group not in groups and not descendant(pid):
                    continue
                info = ctypes.create_string_buffer(256)
                if self.proc.proc_pid_rusage(pid, 2, ctypes.byref(info)) == 0:
                    numbers = (ctypes.c_uint64 * 18).from_buffer(info, 16)
                    # Native rusage CPU fields use Mach absolute ticks, whose
                    # scale differs between Intel and Apple Silicon machines.
                    process_cpu = (numbers[0] + numbers[1] + numbers[10] + numbers[11]) * self.cpu_tick_seconds
                    cpu += process_cpu
                    rss += numbers[6]
                    footprint += numbers[7]
                    if group in owned:
                        owned[group]['cpu_seconds'] += process_cpu
                        owned[group]['rss_bytes'] += numbers[6]
                        owned[group]['physical_bytes'] += numbers[7]
        else:
            for path in Path('/proc').glob('[0-9]*'):
                try:
                    fields = (path / 'stat').read_text().rsplit(')', 1)[1].split()
                    if int(fields[2]) not in groups and int(path.name) != os.getpid():
                        continue
                    process_cpu = sum(int(fields[index]) for index in (11,12,13,14)) / os.sysconf('SC_CLK_TCK')
                    cpu += process_cpu
                    rollup = (path / 'smaps_rollup').read_text().splitlines()
                    physical = next(int(row.split()[1]) for row in rollup if row.startswith('Pss:')) * 1024
                    resident = next(int(row.split()[1]) for row in rollup if row.startswith('Rss:')) * 1024
                    footprint += physical
                    rss += resident
                    if int(fields[2]) in owned:
                        owned[int(fields[2])]['cpu_seconds'] += process_cpu
                        owned[int(fields[2])]['physical_bytes'] += physical
                        owned[int(fields[2])]['rss_bytes'] += resident
                except (OSError, StopIteration, ValueError):
                    pass
        return {'physical_bytes':footprint, 'rss_bytes':rss, 'cpu_seconds':cpu, 'groups':owned}

    def read(self, groups=()):
        now = time.monotonic()
        resources = self.resources(groups)
        footprint, rss, cpu, owned = (resources[name] for name in ('physical_bytes','rss_bytes','cpu_seconds','groups'))
        if platform.system() == 'Darwin':
            ticks = (ctypes.c_uint32 * 4)()
            count = ctypes.c_uint32(4)
            if self.lib.host_statistics(self.lib.mach_host_self(), 3, ticks, ctypes.byref(count)):
                raise OSError('host_statistics CPU sampling failed')
            pressure = ctypes.c_int()
            length = ctypes.c_size_t(ctypes.sizeof(pressure))
            if self.lib.sysctlbyname(b'kern.memorystatus_vm_pressure_level', ctypes.byref(pressure), ctypes.byref(length), None, 0):
                raise OSError('memory pressure sampling failed')
            vm = subprocess.check_output(['vm_stat'], text=True)
            page_size = int(vm.split('page size of ')[1].split(' bytes')[0])
            swap = next(int(line.split(':')[1].strip().rstrip('.')) for line in vm.splitlines() if line.startswith('Swapouts:')) * page_size
            cpu_ticks = list(ticks)
            pressure_value = pressure.value
        else:
            cpu_ticks = list(map(int, Path('/proc/stat').read_text().splitlines()[0].split()[1:]))
            swap = next(int(row.split()[1]) for row in Path('/proc/vmstat').read_text().splitlines() if row.startswith('pswpout ')) * os.sysconf('SC_PAGE_SIZE')
            pressure_value = 1
        idle = None
        rate = 0
        if self.previous:
            at, previous_ticks, previous_swap = self.previous
            delta = [a - b for a, b in zip(cpu_ticks, previous_ticks)]
            # Darwin: user, system, idle, nice. Linux: user, nice, system, idle.
            idle = 100 * delta[2 if platform.system() == 'Darwin' else 3] / max(1, sum(delta))
            rate = max(0, swap - previous_swap) / max(.001, now - at)
        self.previous = now, cpu_ticks, swap
        return {'at': now, 'cpu_idle_percent': idle, 'memory_pressure': pressure_value,
                'swap_out_bytes': swap, 'swap_out_bytes_per_second': rate,
                'physical_bytes': footprint, 'rss_bytes': rss, 'owned_cpu_seconds': cpu, 'groups': owned}


def healthy(sample, policy=None):
    policy = policy or {}
    return (sample['memory_pressure'] != 4 and
            sample['cpu_idle_percent'] is not None and sample['cpu_idle_percent'] >= policy.get('minIdlePercent', 30) and
            sample['swap_out_bytes_per_second'] <= policy.get('maxSwapBytesPerSecond', 16 * 1024**2))


class Monitor:
    def __init__(self, reader=None, policy=None):
        self.reader = reader or SystemSample()
        self.policy = policy or {}
        self.groups = set()
        self.samples = []
        self.delays = []
        self.condition = threading.Condition()
        self.done = threading.Event()
        self.error = None
        self.cancelled = threading.Event()
        self.thread = threading.Thread(target=self.watch, daemon=True)

    def watch(self):
        next_sample = 0
        deadline = time.monotonic() + .1
        while not self.done.wait(max(0, deadline - time.monotonic())):
            now = time.monotonic()
            self.delays.append(max(0, now - deadline))
            deadline = now + .1
            if now >= next_sample:
                try:
                    sample = self.reader.read(set(self.groups))
                    with self.condition:
                        self.samples.append(sample)
                        self.condition.notify_all()
                except Exception as error:
                    self.error = error
                    with self.condition:
                        self.condition.notify_all()
                    return
                next_sample = time.monotonic() + SAMPLE_SECONDS
                # Sampling time must not be counted as scheduling latency.
                deadline = time.monotonic() + .1

    def admit(self):
        start = time.monotonic()
        announced = False
        with self.condition:
            while True:
                if self.cancelled.is_set():
                    raise KeyboardInterrupt('Verification cancelled')
                if self.error:
                    raise RuntimeError('Resource monitor failed') from self.error
                recent = self.samples[-3:]
                if len(recent) == 3 and all(healthy(sample, self.policy) for sample in recent):
                    return time.monotonic() - start
                if time.monotonic() - start >= self.policy.get('admissionTimeoutSeconds', 300):
                    raise RuntimeError('Resource admission timed out; no check was started')
                if self.samples and self.samples[-1]['cpu_idle_percent'] is not None and not healthy(self.samples[-1], self.policy) and not announced:
                    print('Waiting for CPU idle, memory pressure and swap to recover...', flush=True)
                    announced = True
                remaining = self.policy.get('admissionTimeoutSeconds', 300) - (time.monotonic() - start)
                self.condition.wait(max(0, min(SAMPLE_SECONDS, remaining)))

    def summary(self, start=0, group=None):
        samples = [sample for sample in self.samples if sample['at'] >= start]
        memory = [sample['groups'].get(group, {'physical_bytes': 0, 'rss_bytes': 0}) if group is not None else sample for sample in samples]
        return {'peak_physical_bytes': max((s['physical_bytes'] for s in memory), default=0),
                'peak_rss_bytes': max((s['rss_bytes'] for s in memory), default=0),
                'swap_out_bytes': max(0, samples[-1]['swap_out_bytes'] - samples[0]['swap_out_bytes']) if samples else 0,
                'peak_swap_out_bytes_per_second': max((s['swap_out_bytes_per_second'] for s in samples), default=0),
                'peak_memory_pressure': max((s['memory_pressure'] for s in samples), default=0),
                'sample_count': len(samples)}


class OwnedProcesses:
    """A pipe lease protects registered groups even if the supervisor is killed.

    Launchers wait for registration before exec: loss of the supervisor during
    allocation cannot leave an unregistered heavy command running.
    """
    def __init__(self, session):
        self.session = session
        self.groups = set()
        self.guard = None
        self.cancelled = session.cancelled
        self.lock = threading.RLock()

    def start(self, log):
        if self.guard is not None:
            return
        receive, self.send = os.pipe()
        self.ack, reply = os.pipe()
        try:
            Path(log).parent.mkdir(parents=True, exist_ok=True)
            with Path(log).open('ab') as errors:
                self.guard = subprocess.Popen([sys.executable, str(Path(__file__).resolve()),
                    '--process-owner', str(receive), str(reply), str(os.getpid()), str(os.getppid()), str(self.session.directory)],
                    pass_fds=(receive,reply,self.session.lock.fileno()), stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                    stderr=errors, start_new_session=True)
        except BaseException:
            os.close(self.send)
            os.close(self.ack)
            raise
        finally:
            os.close(receive)
            os.close(reply)
        if os.read(self.ack,1) != b'1':
            raise RuntimeError('Verification process owner did not start; see '+str(log))

    def request(self, operation, group):
        os.write(self.send, (json.dumps({'operation':operation,'group':group})+'\n').encode())
        if os.read(self.ack,1) != b'1':
            raise RuntimeError('Verification process owner disconnected')

    def spawn(self, command, *, log, **options):
        with self.lock:
            if self.cancelled.is_set():
                raise KeyboardInterrupt('Verification cancelled before allocation')
            self.start(log)
            receive, allow = os.pipe()
            child = None
            try:
                child = subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--owned-launch',str(receive),*command],
                    pass_fds=(receive,), start_new_session=True, **options)
                self.groups.add(child.pid)
                self.request('add',child.pid)
                if self.cancelled.is_set():
                    raise KeyboardInterrupt('Verification cancelled before launch')
                os.write(allow,b'1')
                return child
            except BaseException:
                if child is not None:
                    ProcessSession.stop_group(child.pid)
                    child.wait()
                    self.discard(child.pid)
                raise
            finally:
                os.close(receive)
                os.close(allow)

    def discard(self, group):
        with self.lock:
            if group in self.groups:
                self.request('remove',group)
                self.groups.discard(group)

    def close(self):
        if self.guard is None:
            return
        os.close(self.send)
        try:
            code = self.guard.wait()
            if code:
                raise RuntimeError('Verification process cleanup failed with '+str(code))
        finally:
            os.close(self.ack);self.guard=None


def supervise_processes(receive, reply, owner, parent, directory):
    groups, pending = set(), b''
    reason = 'supervisor_disconnected'
    queue = None
    parent_fd = None
    try:
        # macOS exposes process exit as a kernel event; no active polling loop.
        if hasattr(select,'kqueue') and parent > 1:
            queue = select.kqueue()
            queue.control([select.kevent(receive,filter=select.KQ_FILTER_READ,flags=select.KQ_EV_ADD),
                select.kevent(parent,filter=select.KQ_FILTER_PROC,flags=select.KQ_EV_ADD|select.KQ_EV_ONESHOT,
                              fflags=select.KQ_NOTE_EXIT)],0,0)
        elif hasattr(os,'pidfd_open') and parent > 1:
            parent_fd = os.pidfd_open(parent)
        os.write(reply,b'1')
        while True:
            if queue:
                events = queue.control(None,2,None)
                readable = any(event.filter == select.KQ_FILTER_READ for event in events)
                parent_exited = any(event.filter == select.KQ_FILTER_PROC for event in events)
            else:
                ready,_,_ = select.select([receive]+([parent_fd] if parent_fd is not None else []),[],[],None if parent_fd is not None or parent<=1 else 2)
                readable = receive in ready
                parent_exited = parent_fd in ready if parent_fd is not None else False
                if not ready and parent>1:
                    try:os.kill(parent,0)
                    except ProcessLookupError:parent_exited=True
            if readable:
                chunk = os.read(receive,65536)
                if not chunk:
                    break
                pending += chunk
                while b'\n' in pending:
                    line,pending = pending.split(b'\n',1)
                    message = json.loads(line)
                    if message['operation']=='add':groups.add(message['group'])
                    elif message['operation']=='remove':groups.discard(message['group'])
                    else:raise ValueError('Unknown process ownership operation')
                    os.write(reply,b'1')
            if parent_exited:
                reason = 'host_process_exited'
                try:os.kill(owner,signal.SIGTERM)
                except ProcessLookupError:pass
                break
    finally:
        if queue:queue.close()
        if parent_fd is not None:os.close(parent_fd)
        errors = []
        for group in groups:
            try:ProcessSession.stop_group(group)
            except Exception as error:errors.append(str(error))
        # The inherited flock remains held until process and data cleanup ends.
        # Logs and persistent artifacts live outside this owned temp directory.
        try:
            shutil.rmtree(directory)
        except FileNotFoundError:
            pass
        except Exception as error:
            errors.append(str(error))
        print(json.dumps({'reason':reason,'disposed_groups':sorted(groups),'errors':errors}),file=sys.stderr,flush=True)
        os.close(receive);os.close(reply)
        if errors:raise RuntimeError('Owned process cleanup failed: '+'; '.join(errors))


class ProcessSession:
    """Own admission, process groups, temporary files and the machine lease."""
    def __init__(self, state, profile='quiet', policy=None, monitor=None, lock_directory=None):
        self.state = Path(state)
        self.profile = profile
        self.policy = policy or {}
        self.monitor = monitor or Monitor(policy=self.policy)
        self.cancelled = threading.Event()
        self._processes = OwnedProcesses(self)
        self.monitor.groups = self._processes.groups
        self.previous_signals = {}
        self.lock_directory = Path(lock_directory or Path.home() / '.cache/pocketbase-gate')
        self.directory = None
        self.lock = None

    def __enter__(self):
        if platform.system() not in ('Darwin', 'Linux'):
            raise RuntimeError('Process supervision currently supports macOS and Linux')
        self.state.mkdir(parents=True, exist_ok=True)
        self.lock_directory.mkdir(parents=True, exist_ok=True)
        self.lock = (self.lock_directory / 'machine.lock').open('a+')
        try:
            fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print('Another gate owns the machine lease; waiting...', flush=True)
            fcntl.flock(self.lock, fcntl.LOCK_EX)
        self.content = Content(self.state / 'content.json')
        self.receipts = ReceiptCache(self.state / 'cache', self.content)
        self.receipts.prune(self.policy.get('cacheBytes', CACHE_LIMIT))
        self.directory = Path(tempfile.mkdtemp(prefix='run-', dir=self.state))
        self.logs = self.state / 'logs' / self.directory.name
        self.logs.mkdir(parents=True)
        self.monitor.thread.start()
        for signum in (signal.SIGINT, signal.SIGTERM):
            self.previous_signals[signum] = signal.getsignal(signum)
            signal.signal(signum, self.cancel)
        return self

    def cancel(self, signum, frame):
        self.cancelled.set()
        self.monitor.cancelled.set()
        with self.monitor.condition:
            self.monitor.condition.notify_all()
        raise KeyboardInterrupt('Gate cancelled')

    def environment(self, extra=None):
        temporary = self.directory / 'tmp'
        temporary.mkdir(exist_ok=True)
        return dict(os.environ, TMPDIR=str(temporary), PBGATE_PROFILE=self.profile, **(extra or {}))

    def spawn(self, command, cwd, log, env=None):
        return self._processes.spawn(schedule(command, self.profile),
            log=self.logs / 'process-owner.log', cwd=cwd, env=self.environment(env),
            stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT)

    def stop(self, child):
        try:
            self.stop_group(child.pid)
            child.wait()
        finally:
            self._processes.discard(child.pid)

    def check_resources(self, start=0):
        if self.cancelled.is_set():
            raise KeyboardInterrupt('Gate cancelled')
        if self.monitor.error:
            raise RuntimeError('Resource monitor failed') from self.monitor.error
        if self.monitor.summary(start)['peak_physical_bytes'] > self.policy.get('maxMemoryBytes', 6 * 1024**3):
            raise RuntimeError('Owned processes exceeded the configured memory budget')

    def run(self, command, cwd, log, env=None, timeout=120, admission=True):
        waited = self.monitor.admit() if admission else 0
        start = time.monotonic()
        with Path(log).open('ab') as output:
            child = self.spawn(command, cwd, output, env)
            try:
                while True:
                    self.check_resources(start)
                    pid, status, usage = os.wait4(child.pid, os.WNOHANG)
                    if pid:
                        child.returncode = os.waitstatus_to_exitcode(status)
                        break
                    if time.monotonic() - start >= timeout:
                        raise subprocess.TimeoutExpired(command, timeout)
                    self.cancelled.wait(.05)
            finally:
                self.stop(child)
        return child.returncode, dict(self.monitor.summary(start),
            wall_seconds=round(time.monotonic() - start, 4),
            cpu_seconds=round(usage.ru_utime + usage.ru_stime, 4), admission_seconds=round(waited, 4))

    @staticmethod
    def stop_group(group):
        def finished():
            rows = subprocess.check_output(['ps', '-axo', 'pgid=,stat='], text=True).splitlines()
            return not any(int(fields[0]) == group and not fields[1].startswith('Z')
                for fields in (row.split() for row in rows) if len(fields) == 2)
        try:
            os.killpg(group, signal.SIGTERM)
        except ProcessLookupError:
            return
        except PermissionError:
            if finished():
                return
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if finished():
                return
            time.sleep(.05)
        try:
            os.killpg(group, signal.SIGKILL)
        except ProcessLookupError:
            return
        except PermissionError:
            if not finished():
                raise
        deadline = time.monotonic() + 5
        while not finished():
            if time.monotonic() >= deadline:
                raise RuntimeError('Owned process group survived SIGKILL: ' + str(group))
            time.sleep(.05)

    def __exit__(self, *error):
        failures = []
        for group in set(self._processes.groups):
            try:
                self.stop_group(group)
                self._processes.discard(group)
            except Exception as failure:
                failures.append(failure)
        try:
            self._processes.close()
        except Exception as failure:
            failures.append(failure)
        finally:
            self.monitor.done.set()
            self.monitor.thread.join(timeout=5)
            try:
                self.receipts.prune(self.policy.get('cacheBytes', CACHE_LIMIT))
                shutil.rmtree(self.directory, ignore_errors=True)
            finally:
                for signum, handler in self.previous_signals.items():
                    signal.signal(signum, handler)
                self.lock.close()
        if failures:
            raise RuntimeError('Process cleanup failed: ' + '; '.join(map(str, failures))) from failures[0]


class ReceiptCache:
    def __init__(self, directory, content=None):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.content = content or Content()
        self._candidates = None
        self._candidate_lock = threading.Lock()

    def candidates(self, stage):
        with self._candidate_lock:
            if self._candidates is None:
                self._candidates = {}
                for directory in sorted(self.directory.iterdir(), key=lambda path: path.stat().st_mtime_ns, reverse=True):
                    try:
                        receipt = json.loads((directory / 'receipt.json').read_text())
                        if receipt.get('format') == 1 and receipt.get('key') == directory.name:
                            self._candidates.setdefault(receipt.get('stage'), []).append((directory, receipt))
                    except (OSError, ValueError, TypeError, AttributeError):
                        pass
            return list(self._candidates.get(stage, []))

    def explain(self, stage, inputs, command, parameters, component=None):
        """Diagnostic metadata only; never an alternative cache verdict."""
        previous = next((receipt for _, receipt in self.candidates(stage) if isinstance(receipt.get('inputs'), dict)
                         and (component is None or receipt.get('component') == component)), None)
        if previous is None:
            return {'reason':'no_previous_receipt'}
        changed = sorted(name for name in inputs.keys() | previous['inputs'].keys() if inputs.get(name) != previous['inputs'].get(name))
        return {'reason':'inputs_or_execution_changed' if changed or previous.get('command') != command or previous.get('parameters') != parameters else 'receipt_or_outputs_invalid',
                'changed_inputs':changed, 'command_changed':previous.get('command') != command,
                'parameters_changed':previous.get('parameters') != parameters}

    @staticmethod
    def key(inputs, command, parameters):
        return hashlib.sha256(json.dumps({'format': 1, 'inputs': inputs, 'command': command,
                                         'parameters': parameters}, sort_keys=True).encode()).hexdigest()

    def read(self, key, repo, outputs=None):
        path = self.directory / key / 'receipt.json'
        try:
            receipt = json.loads(path.read_text())
            if receipt['key'] != key or receipt['format'] != 1 or not isinstance(receipt['outputs'], dict) or not isinstance(receipt['output_roots'], list):
                return None
            if outputs is not None and receipt['output_roots'] != outputs:
                return None
            if not isinstance(receipt.get('metrics'), dict):
                return None
            if self.content.inventory(repo, receipt['output_roots']) != receipt['outputs']:
                return None
            os.utime(path.parent, None)
            return receipt
        except (OSError, ValueError, KeyError, TypeError):
            return None

    def save(self, key, receipt, repo, outputs, artifacts=False):
        hashes = self.content.inventory(repo, outputs)
        if (outputs and not hashes) or any(value == 'missing' for value in hashes.values()):
            raise RuntimeError('Missing verification output; receipt was not saved')
        target = self.directory / key
        target.mkdir(exist_ok=True)
        if artifacts:
            for name in hashes:
                destination = target / 'artifacts' / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(Path(repo) / name, destination)
        saved = dict(receipt, format=1, key=key, outputs=hashes, output_roots=outputs,
                     completed=datetime.now(timezone.utc).isoformat())
        atomic_json(target / 'receipt.json', saved)
        with self._candidate_lock:
            if self._candidates is not None:
                entries = self._candidates.setdefault(saved.get('stage'), [])
                entries[:] = [(path, value) for path, value in entries if path.name != key]
                entries.insert(0, (target, saved))

    def restore(self, key, repo, outputs=None):
        try:
            repo = Path(repo).resolve()
            entry = self.directory / key
            receipt = json.loads((entry / 'receipt.json').read_text())
            if receipt['key'] != key or receipt.get('format') != 1 or not isinstance(receipt['outputs'], dict) or not receipt['outputs'] or not isinstance(receipt['output_roots'], list):
                return False
            if outputs is not None and receipt['output_roots'] != outputs:
                return False
            for name in [*receipt['output_roots'], *receipt['outputs']]:
                if not isinstance(name, str) or name in ('', '.') or Path(name).is_absolute() or '..' in Path(name).parts:
                    return False
                destination = (repo / name).resolve()
                if destination == repo or not destination.is_relative_to(repo):
                    return False
            for name, expected in receipt['outputs'].items():
                if not any(name == root or name.startswith(root.rstrip('/') + '/') for root in receipt['output_roots']):
                    return False
                if self.content.file(entry / 'artifacts' / name) != expected:
                    return False
            for name in receipt['output_roots']:
                path = Path(repo) / name
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink(missing_ok=True)
            for name in receipt['outputs']:
                destination = Path(repo) / name
                destination.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(entry / 'artifacts' / name, destination)
            return True
        except (OSError, ValueError, KeyError, TypeError):
            return False


    def invalidate(self, key):
        (self.directory / key / 'receipt.json').unlink(missing_ok=True)
        with self._candidate_lock:
            if self._candidates is not None:
                for entries in self._candidates.values():
                    entries[:] = [(path, value) for path, value in entries if path.name != key]

    def prune(self, limit=CACHE_LIMIT):
        entries = []
        for directory in self.directory.iterdir():
            if directory.is_dir():
                size = sum(path.stat().st_size for path in directory.rglob('*') if path.is_file())
                entries.append((directory.stat().st_mtime_ns, directory, size))
        size = sum(entry[2] for entry in entries)
        for _, directory, used in sorted(entries):
            if size <= limit:
                break
            shutil.rmtree(directory)
            size -= used


if __name__ == '__main__':
    if sys.argv[1:2] == ['--owned-launch']:
        receive = int(sys.argv[2])
        permitted = os.read(receive, 1) == b'1'
        os.close(receive)
        if not permitted:
            raise SystemExit(125)
        os.execvpe(sys.argv[3], sys.argv[3:], os.environ)
    elif sys.argv[1:2] == ['--process-owner']:
        supervise_processes(*(int(value) for value in sys.argv[2:6]), Path(sys.argv[6]))
    else:
        raise SystemExit('Use the pbgate CLI')
