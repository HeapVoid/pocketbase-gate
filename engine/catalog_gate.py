"""Catalog execution: independent leaf proofs, batching and one owned session."""
import hashlib
import json
import resource
import subprocess
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from .runtime import ReceiptCache, atomic_json, FOOTPRINT_LIMIT
from .catalog import VerificationCatalog, ExecutionOrder, recipe, execution_command, access_conflict

class CatalogGate:
    def __init__(self, project):
        self.project = project

    @staticmethod
    def conflicts(first, second):
        def access(stage):
            reads = ['output:' + path for path in [*stage['inputs'],*stage.get('source_inputs',[]),*stage.get('tests',[])]]
            writes = ['output:' + path for path in stage['outputs']] + ['exclusive:' + name for name in stage.get('exclusive',[])]
            return reads, writes
        return first['name'] in second.get('requires',[]) or access_conflict(*access(first),*access(second))

    def verify(self, repo, gate, session, force=False, selected=None, legacy_command=None, stage_override=None, extra_parameters=None, candidate=None, restore_artifacts=False, plan=None):
        project = self.project
        project.assert_current()
        if plan is not None:
            plan.assert_current()
        parameters, snapshot = project.parameters, project.snapshot
        stages_for, stage_inputs = project.stages_for, project.stage_inputs
        stamp_path, write_stamp, git = project.stamp_path, project.write_stamp, project.git
        VerificationCatalog = project.catalog
        started = time.monotonic()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        cpu_start = usage.ru_utime + usage.ru_stime
        params = parameters(session.profile)
        params.update(extra_parameters or {})
        if plan is None and not stage_override and not legacy_command and (repo / 'scripts/verification-stages.json').is_file():
            catalog = VerificationCatalog.read(repo)
            catalog.validate_inventory(repo)
            plan = catalog.plan(selected=selected)
            selected = None
        stages = stage_override or (plan.stages if plan else stages_for(repo, gate))
        targeted = plan is not None and plan.intent != 'release'
        if getattr(session, 'typecheck_checkout', None):
            check = stages[0] if len(stages) == 1 else {}
            command = check.get('command', [])
            files = command[3:-1]
            if (repo.resolve() != session.typecheck_checkout or not targeted or plan.intent != 'dev'
                    or legacy_command or stage_override or check.get('name') != 'dev-types'
                    or check.get('group') != 'types' or check.get('outputs') or check.get('requires')
                    or command[:3] != ['bun', 'run', 'bimba'] or command[-1:] != ['--typecheck']
                    or not files or any(Path(name).is_absolute() or '..' in Path(name).parts
                        or Path(name).suffix != '.imba' or not (repo / name).is_file() for name in files)):
                raise ValueError('The file typecheck session accepts only read-only Imba file diagnostics')
        if legacy_command:
            stages = [{'name': 'legacy', 'group': 'legacy', 'command': legacy_command,
                       'inputs': ['src', 'scripts', 'test', 'tests', 'compile.imba', 'package.json', 'bun.lock'], 'outputs': []}]
        if selected:
            catalog = VerificationCatalog({'component': gate, 'stages': stages})
            plan = catalog.plan(selected=selected)
            stages = plan.stages
        content = session.content
        cache = session.receipts if not targeted or any(stage['group'] == 'compile' for stage in stages) else None
        report = {'format': 2, 'gate': gate, 'checkout': str(repo), 'commit': git(repo, 'rev-parse', 'HEAD').decode().strip(),
                  'profile': session.profile, 'release_candidate': candidate, 'fixture_logs': str(session.fixture_logs), 'stages': [], 'started': datetime.now(timezone.utc).isoformat()}
        checkpoint = content.checkpoint(lambda observed: snapshot(repo, gate, content, observed)) if not stage_override and not targeted else content.checkpoint()
        if plan:
            report['plan'] = plan.describe()
        stamp = stamp_path(repo, gate)
        previous_failed = []
        if not targeted:
            try:
                previous = json.loads(stamp.with_name(stamp.stem + '-report.json').read_text())
                failed = {stage['name'] for stage in previous['stages'] if stage.get('exit_code')}
                previous_failed = list(failed) + [member for check in previous.get('plan', {}).get('checks', [])
                                                if check['name'] in failed for member in check.get('covers', [])]
            except (OSError, ValueError, KeyError, TypeError):
                pass
        history = project.history / session.directory.name
        run_name = stamp.stem + '-' + str(time.time_ns())
        report['history_path'] = str(history / (run_name + '-report.json'))
        session.last_report_path = Path(report['history_path'])
        if hasattr(session, 'prepare_backend'):
            lifecycle_commands = [execution_command(stage) for stage in stages
                                  if session.lifecycle_command(execution_command(stage))]
            if lifecycle_commands:
                session.prepare_backend(repo, lifecycle_commands)
        lifecycle_commands = locals().get('lifecycle_commands', [])
        start = time.monotonic()
        code = 0
        pending = []
        command_intervals = []
        prepared = {}
        def execute(checks):
            results, missing = [], []
            for stage in checks:
                identity = recipe(stage)
                cacheable = not targeted or stage['group'] == 'compile'
                lightweight = targeted and not cacheable
                if not force and cacheable and not legacy_command:
                    cache.restore_manifest(stage, repo, identity, params)
                fingerprint = prepared.pop(stage['name'], None) if order and not stage.get('source_manifest') else None
                if fingerprint:
                    inputs, observed, key = fingerprint
                else:
                    observed = {}
                    inputs = stage_inputs(content, repo, gate, stage, observed, lightweight=lightweight)
                    key = ReceiptCache.key(inputs, identity, params)
                artifact = stage.get('restore_artifact', False) or (restore_artifacts and stage['group'] == 'compile')
                receipt = None if force or legacy_command or not cacheable else cache.read(key, repo, stage['outputs'])
                if not receipt and not force and cacheable and artifact and cache.restore(key, repo, stage['outputs']):
                    receipt = cache.read(key, repo, stage['outputs'])
                if receipt:
                    content.common_inventory(repo, stage['outputs'], observed)
                    checkpoint.protect(observed)
                    print(f"{gate}/{stage['name']}: cached", flush=True)
                    results.append((0, {'name':stage['name'], 'key':key, 'cache_hit':True,
                        'metrics':{'wall_seconds':0, 'cpu_seconds':0, 'peak_physical_bytes':0, 'swap_out_bytes':0},
                        'verified_metrics':receipt['metrics']}))
                    continue
                miss = cache.explain(stage['name'], inputs, identity, params, component=gate) if cacheable and not force and not legacy_command else {'reason':'forced' if force or legacy_command else 'targeted_test'}
                if cacheable:
                    cache.invalidate(key)
                missing.append((stage, identity, cacheable, lightweight, inputs, observed, key, artifact, miss))
            if not missing:
                return results
            stage = missing[0][0]
            command = execution_command(stage)
            if len(missing) > 1:
                batch = stage['file_batch']
                prefix = [*batch['command'], *batch['separator']]
                if any(item[0].get('file_batch') != batch or item[0]['outputs'] or
                       recipe(item[0]) != [[*prefix, *item[0]['tests']]] for item in missing):
                    raise ValueError('File batch must preserve each registered test recipe')
                command = [*prefix, *dict.fromkeys(test for item in missing for test in item[0]['tests'])]
            names = [item[0]['name'] for item in missing]
            print(f"{gate}: checking " + ', '.join(names), flush=True)
            log = history / (run_name + '-' + stage['name'] + '.log')
            command_started = time.monotonic()
            options = project.execution_options(gate, stage, session.profile, force)
            try:
                exit_code, metrics = session.run(command, repo, log, **options)
            except subprocess.TimeoutExpired as error:
                exit_code = 124
                metrics = dict(session.monitor.summary(command_started), wall_seconds=round(time.monotonic()-command_started,4), timeout_seconds=error.timeout)
                with log.open('a') as output:
                    output.write(f'\nVerification stage exceeded its {error.timeout}s wall-time limit; owned processes disposed.\n')
            command_intervals.append((command_started, time.monotonic()))
            if stage['group'] == 'types' and log.is_file():
                with log.open('rb') as output:
                    output.seek(max(0, log.stat().st_size-256*1024))
                    for line in output.read().decode(errors='replace').splitlines():
                        try:
                            diagnostics = json.loads(line)
                            if isinstance(diagnostics, dict) and diagnostics.get('mode') in ('diagnostics','snapshot-reused'):
                                metrics['bimba'] = {name:diagnostics.get(name) for name in ('mode','checked','covered','compiler','stable')}
                        except ValueError:
                            pass
            if exit_code:
                with log.open('rb') as output:
                    output.seek(max(0, log.stat().st_size-256*1024))
                    print('\n'.join(output.read().decode(errors='replace').splitlines()[-80:]), file=sys.stderr)
                print(f'{gate}: failed; full log: {log}', file=sys.stderr)
            for index, (member, identity, cacheable, lightweight, inputs, observed, key, artifact, miss) in enumerate(missing):
                # One process proves every requested file. Attribute its resource
                # metrics once; each leaf retains its own inputs and verdict.
                owned_metrics = metrics if index == 0 else {'wall_seconds':0, 'cpu_seconds':0}
                result = {'name':member['name'], 'key':key, 'cache_hit':False, 'cache_miss':miss,
                          'metrics':owned_metrics, 'log':str(log), 'exit_code':exit_code}
                if len(missing) > 1:
                    result['file_batch'] = {'metrics_owner':names[0], 'checks':names}
                request_metrics = log.with_suffix('.requests.jsonl')
                if request_metrics.is_file():
                    result['request_metrics'] = str(request_metrics)
                member_code = exit_code
                if not member_code:
                    after = {}
                    final_inputs = stage_inputs(content, repo, gate, member, after, lightweight=lightweight)
                    if not member.get('source_manifest') and (inputs != final_inputs or observed != after):
                        member_code = 2
                        result['exit_code'] = member_code
                        print(f"{gate}/{member['name']}: inputs changed during verification; success not saved", file=sys.stderr)
                    elif cacheable:
                        final_key = cache.key(final_inputs, identity, params)
                        result['key'] = final_key
                        pending.append((final_key, {'stage':member['name'], 'component':gate, 'inputs':final_inputs,
                            'command':identity, 'parameters':params, 'metrics':metrics}, member['outputs'],
                            artifact or member['group'] == 'compile', content.inventory(repo, member['outputs'])))
                results.append((member_code, result))
            return results

        try:
            order = ExecutionOrder(stages, previous_failed) if not targeted and plan else None
            reuse = {}
            at = 0
            while at < len(stages):
                ready = order.ready() if order else stages[at:]
                if not ready:
                    raise ValueError('No verification check has satisfied prerequisites')
                if order:
                    def priority(stage):
                        # This changes only execution order. execute() still verifies
                        # the identity and stability before accepting any receipt.
                        if stage['name'] not in reuse:
                            observed = {}
                            inputs = stage_inputs(content, repo, gate, stage, observed)
                            key = ReceiptCache.key(inputs, recipe(stage), params)
                            prepared[stage['name']] = inputs, observed, key
                            reuse[stage['name']] = not force and cache.read(key, repo, stage['outputs']) is not None
                        return order.priority(stage), reuse[stage['name']]
                    ready = sorted(ready, key=priority)
                batch = [ready[0]]
                if batch[0].get('file_batch'):
                    if order:
                        batch = [stage for stage in ready if stage.get('file_batch') == batch[0]['file_batch']]
                    else:
                        # Targeted plans retain prerequisite order. A later file
                        # with the same runner cannot jump over its preparation.
                        for stage in ready[1:]:
                            if stage.get('file_batch') != batch[0]['file_batch']:
                                break
                            batch.append(stage)
                elif session.profile == 'fast' and batch[0]['group'] == 'http' and len(ready)>1 and ready[1]['group'] == 'http' and not self.conflicts(batch[0],ready[1]):
                    batch.append(ready[1])
                if len(batch) == 1 or batch[0].get('file_batch'):
                    results = execute(batch)
                else:
                    with ThreadPoolExecutor(max_workers=2) as executor:
                        results = [result for group in executor.map(lambda stage: execute([stage]), batch) for result in group]
                for exit_code, result in results:
                    report['stages'].append(result)
                    if exit_code:
                        code = exit_code
                if any(not result['cache_hit'] for _, result in results):
                    # A command can rebuild compiler/dependency manifests. Cached
                    # stages can share their fingerprint inside the checkpoint;
                    # execution requires a new observation for remaining stages.
                    prepared.clear()
                if code:
                    break
                if order:
                    order.finish(batch)
                at += len(batch)
        except BaseException:
            code = 130
            if not targeted:
                stamp.unlink(missing_ok=True)
            raise
        finally:
            if lifecycle_commands:
                closing = time.monotonic()
                try:
                    session.close_lifecycle_workers()
                except Exception as error:
                    code = code or 2
                    report['fixture_shutdown_error'] = str(error)
                    print('Backend fixture shutdown failed: ' + str(error), file=sys.stderr)
                report['fixture_shutdown_seconds'] = round(time.monotonic()-closing,4)
            report['duration_seconds'] = round(time.monotonic() - start, 3)
            report['resource_metrics'] = session.monitor.summary()
            resources_valid = report['resource_metrics'].get('peak_physical_bytes', 0) <= FOOTPRINT_LIMIT and not getattr(session.monitor, 'error', None)
            if not resources_valid:
                code = code or 2
            delays = sorted(session.monitor.delays)
            report['supervisor_p95_delay_ms'] = 1000 * delays[min(len(delays)-1, int(len(delays)*.95))] if delays else None
            report['cache_hits'] = sum(s['cache_hit'] for s in report['stages'])
            report['completed'] = datetime.now(timezone.utc).isoformat()
            checked = checkpoint.finish()
            report['snapshot'], stable = checked['snapshot'], checked['stable']
            if not stable:
                code = code or 2
                report['changed_inputs'] = checked['changed_inputs']
                print(f'{gate}: inputs changed during verification; success not saved', file=sys.stderr)
                for path in report['changed_inputs'][:10]:
                    print('  ' + path, file=sys.stderr)
            report['total_wall_seconds'] = round(time.monotonic() - started, 3)
            merged = []
            for first, last in sorted(command_intervals):
                if merged and first <= merged[-1][1]:
                    merged[-1] = (merged[-1][0], max(last, merged[-1][1]))
                else:
                    merged.append((first, last))
            report['controller_wall_seconds'] = round(report['total_wall_seconds'] - sum(last - first for first, last in merged), 3)
            usage = resource.getrusage(resource.RUSAGE_SELF)
            report['supervisor_cpu_seconds'] = round(usage.ru_utime + usage.ru_stime - cpu_start, 4)
            report['cpu_seconds'] = round(report['supervisor_cpu_seconds'] + sum(stage['metrics'].get('cpu_seconds', 0) for stage in report['stages']), 4)
            report['passed'] = code == 0 and stable
            report['exit_code'] = code
            if not targeted:
                atomic_json(stamp.with_name(stamp.stem + '-report.json'), report)
            atomic_json(report['history_path'], report)
        # A later failed check does not invalidate completed stages. The enclosing
        # checkpoint still rejects changing sources, and outputs must remain those
        # produced by the successful command, including when a later stage fails.
        if stable and resources_valid:
            for key, receipt, outputs, artifact, verified_outputs in pending:
                if content.inventory(repo, outputs) != verified_outputs:
                    code = code or 2
                    print(f"{gate}/{receipt['stage']}: outputs changed after verification; receipt not saved", file=sys.stderr)
                    continue
                cache.save(key, receipt, repo, outputs, artifacts=artifact)
        report['total_wall_seconds'] = round(time.monotonic() - started, 3)
        report['controller_wall_seconds'] = round(report['total_wall_seconds'] - sum(last - first for first, last in merged), 3)
        report['resource_metrics'] = session.monitor.summary()
        usage = resource.getrusage(resource.RUSAGE_SELF)
        report['supervisor_cpu_seconds'] = round(usage.ru_utime + usage.ru_stime - cpu_start, 4)
        report['cpu_seconds'] = round(report['supervisor_cpu_seconds'] + sum(stage['metrics'].get('cpu_seconds', 0) for stage in report['stages']), 4)
        report['passed'] = code == 0 and stable
        report['exit_code'] = code
        if not targeted:
            atomic_json(stamp.with_name(stamp.stem + '-report.json'), report)
        atomic_json(report['history_path'], report)
        if code or not report['passed']:
            if not targeted:
                stamp.unlink(missing_ok=True)
            return code or 2
        if not selected and not legacy_command and not stage_override and (plan is None or plan.complete):
            write_stamp(stamp, report)
        print(f"{gate}: passed in {report['total_wall_seconds']}s; {report['cache_hits']}/{len(stages)} cached stages")
        print('Report: ' + report['history_path'])
        return 0

