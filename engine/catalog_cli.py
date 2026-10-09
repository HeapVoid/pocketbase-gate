"""Command-line planning and presentation for the verification engine."""
import argparse
import json
import os
from pathlib import Path
import sys
import time
from .runtime import Content, ReceiptCache, atomic_json
from .catalog import recipe

def main(engine):
    VerificationSession, VerificationCatalog = engine.VerificationSession, engine.VerificationCatalog
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('gate', choices=engine.GATES, nargs='?')
    parser.add_argument('--scope', choices=engine.GATES, action='append', help='Plan several components in one verification session')
    parser.add_argument('--cwd', type=Path)
    parser.add_argument('--release', action='store_true')
    parser.add_argument('--intent', choices=('dev', 'release', 'diagnostic'))
    parser.add_argument('--domain', action='append')
    parser.add_argument('--file', action='append', help='Targeted Imba types or registered Backend test files; checkout-relative paths')
    parser.add_argument('--plan', '--explain', action='store_true', dest='plan', help='Describe checks and prerequisites without executing')
    parser.add_argument('--json', action='store_true')
    parser.add_argument('--force', action='store_true', help='Repeat every selected stage')
    parser.add_argument('--status', action='store_true')
    parser.add_argument('--verbose', action='store_true', help='Compatibility option; detailed output is retained in stage logs')
    parser.add_argument('--profile', choices=('quiet', 'fast'), default='quiet')
    parser.add_argument('--stage', action='append', help='Run a named stage for targeted release verification')
    parser.add_argument('--legacy-command', help='JSON argv for a serial baseline measurement; disables reuse')
    parser.add_argument('--test-args', nargs=argparse.REMAINDER, help='Arguments for one parameterized diagnostic check; place last')
    args = parser.parse_args()
    intent = args.intent or 'release'
    if not args.status and not args.plan and intent == 'release' and not args.release:
        parser.error('Full gates require explicit deployment preparation; add --release')
    if args.force and not args.release:
        parser.error('--force requires --release')
    if args.scope:
        if args.gate or args.cwd or args.file or args.status or args.legacy_command or args.test_args:
            parser.error('--scope cannot be combined with a positional target, --cwd, --file, --status, --legacy-command or --test-args')
        plans = []
        for component in dict.fromkeys(args.scope):
            checkout = engine.WORKSPACE / engine.GATES[component][0]
            catalog = VerificationCatalog.read(checkout) if (checkout / 'scripts/verification-stages.json').exists() else VerificationCatalog({'component': component, 'stages': engine.stages_for(checkout, component)})
            plan = catalog.plan(intent=intent, selected=args.stage, domains=args.domain)
            plan.bind_test_arguments(allow_missing=args.plan)
            catalog.validate_inventory(checkout)
            plans.append((component, checkout, plan))
        if args.plan:
            if args.json:
                print(json.dumps({'scope': args.scope, 'intent': intent, 'plans': [plan.describe() for _, _, plan in plans]}, indent=2))
            else:
                for component, _, plan in plans:
                    print(component + ': ' + str(len(plan.stages)) + ' checks')
                    for check in plan.describe()['checks']:
                        print('  ' + check['id'] + ': ' + check['reason'])
                        if check.get('test_arguments'):
                            print('    --test-args ' + check['test_arguments'])
            return 0
        with VerificationSession(engine.WORKSPACE, args.profile) as session:
            started = time.monotonic()
            checkpoint = session.content.checkpoint(lambda observed: {
                component: engine.snapshot(checkout, component, session.content, observed)
                for component, checkout, _ in plans}) if intent == 'release' else session.content.checkpoint()
            reports = []
            for component, checkout, plan in plans:
                code = engine.verify(checkout, component, session, args.force, plan=plan)
                if code:
                    return code
                reports.append(engine.read_stamp(session.last_report_path))
            passed = checkpoint.finish()['stable']
            report = engine.WORKSPACE / '.local-run/gate-results/history' / session.directory.name / 'scope-report.json'
            atomic_json(report, {'scope': args.scope, 'intent': intent, 'passed': passed,
                                 'total_wall_seconds': round(time.monotonic() - started, 3), 'components': reports,
                                 'resource_metrics': session.monitor.summary()})
            if not passed:
                print('Assembled sources changed during verification; repeat this plan', file=sys.stderr)
                return 2
            print('Scope report: ' + str(report))
        return 0
    if not args.gate:
        parser.error('Choose a target or --scope')
    repo = (args.cwd or engine.WORKSPACE / engine.GATES[args.gate][0]).resolve()
    if not (repo / 'package.json').is_file():
        parser.error('Not a project checkout: ' + str(repo))
    if args.file:
        if intent != 'dev' or args.gate not in ('app', 'admin', 'backend'):
            parser.error('--file requires --intent dev and an Imba component')
        if any(Path(name).is_absolute() or '..' in Path(name).parts for name in args.file):
            parser.error('--file paths must be checkout relative')
        if args.gate == 'backend' and all(Path(name).suffix in ('.js', '.py') for name in args.file):
            catalog = VerificationCatalog.read(repo)
            catalog.validate_inventory(repo)
            plan = catalog.plan_test_files(args.file)
        else:
            stage = {'name': 'dev-types', 'group': 'types', 'command': ['bun', 'run', 'bimba', *args.file, '--typecheck'],
                     'inputs': args.file + ['types.d.ts', 'src/types.d.ts'], 'outputs': [], 'intents': ['dev'], 'external_inputs': []}
            catalog = VerificationCatalog({'component': args.gate, 'stages': [stage]})
            plan = catalog.plan(intent='dev', selected=['dev-types'])
    else:
        catalog_path = repo / 'scripts/verification-stages.json'
        catalog = VerificationCatalog.read(repo) if catalog_path.exists() else VerificationCatalog({'component': args.gate, 'stages': engine.stages_for(repo, args.gate)})
        plan = catalog.plan(intent=intent, selected=args.stage, domains=args.domain)
        catalog.validate_inventory(repo)
    try:
        plan.bind_test_arguments(args.test_args, allow_missing=args.plan)
    except ValueError as error:
        parser.error(str(error))
    if args.plan:
        description = plan.describe()
        if args.json:
            print(json.dumps(description, indent=2))
        else:
            print(f'{args.gate}: {intent}; {len(plan.stages)} checks; full gate: {plan.complete}')
            for check in description['checks']:
                print(check['id'] + ': ' + check['reason'])
                if check.get('test_arguments'):
                    print('  --test-args ' + check['test_arguments'])
        return 0

    if args.status:
        previous = engine.read_stamp(engine.stamp_path(repo, args.gate))
        current = previous.get('snapshot') == engine.snapshot(repo, args.gate) and previous.get('passed', True)
        if not current:
            print(f'{args.gate}: not checked')
            return 1
        content = Content()
        cache = ReceiptCache(engine.WORKSPACE / '.local-run/verification-cache', content)
        params = engine.parameters(args.profile)
        for stage in plan.stages:
            key = cache.key(engine.stage_inputs(content, repo, args.gate, stage), recipe(stage), params)
            if cache.read(key, repo, stage['outputs']) is None:
                print(f'{args.gate}: not checked')
                return 1
        print(f'{args.gate}: {"passed" if current else "not checked"}')
        return 0 if current else 1
    file_types = bool(args.file) and all((repo / name).is_file() and Path(name).suffix == '.imba' for name in args.file)
    with VerificationSession(engine.WORKSPACE, args.profile, typecheck_checkout=repo if file_types else None) as session:
        return engine.verify(repo, args.gate, session, args.force, None,
                      json.loads(args.legacy_command) if args.legacy_command else None, plan=plan)
