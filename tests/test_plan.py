import json
from pathlib import Path
import tempfile
import unittest
from support import ROOT
from gate import Project, Gate


def check(name, **fields):
    return dict(id=name, command=['python3', '-c', 'pass'], inputs=['src'], **fields)


class PlannerTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)

    def project(self, checks, **fields):
        return Project(self.root, dict(format=1, checks=checks, **fields))

    def test_dependencies_and_release_coverage_have_one_owner(self):
        project = self.project([check('compile', kind='prepare'), check('chat', requires=['compile'], domains=['chat'])])
        selected = project.plan('dev', domains=['chat'])
        self.assertEqual([check.id for check in selected.checks], ['compile', 'chat'])
        self.assertFalse(selected.complete)
        self.assertTrue(project.plan().complete)
        with self.assertRaisesRegex(ValueError, 'require --check'):
            project.plan('dev')

    def test_bad_registration_cannot_become_a_passing_empty_gate(self):
        for checks in ([], [check('a'), check('a')], [check('a', requires=['b'])],
                       [check('a', requires=['b']), check('b', requires=['a'])]):
            with self.assertRaises(ValueError):
                self.project(checks)
        with self.assertRaisesRegex(ValueError, 'Unknown check fields'):
            self.project([check('a', timoutSeconds=1)])
        with self.assertRaisesRegex(ValueError, 'Required checks'):
            self.project([check('a')], required=['missing'])

    def test_diagnostic_dependencies_cannot_enter_current_gate(self):
        project = self.project([check('history', intents=['diagnostic']), check('current', requires=['history'])])
        with self.assertRaisesRegex(ValueError, 'Diagnostic-only'):
            project.plan()

    def test_outputs_and_state_cannot_escape_or_overwrite_project_root(self):
        for path in ('.', '../other', '/tmp/out', 'public/*', '.pbgate/cache'):
            with self.assertRaises(ValueError):
                self.project([check('a', outputs=[path])])
        with tempfile.TemporaryDirectory() as outside:
            (self.root / 'escape').symlink_to(outside)
            with self.assertRaisesRegex(ValueError, 'escape'):
                self.project([check('a', outputs=['escape/output'])])

    def test_inventory_rejects_lost_or_duplicate_test_ownership(self):
        (self.root / 'test').mkdir()
        (self.root / 'test/a.js').write_text('// test')
        with self.assertRaisesRegex(ValueError, 'Unregistered'):
            self.project([check('a')], testInventory=['test/*.js'])
        project = self.project([check('a', tests=['test/*.js'])], testInventory=['test/*.js'])
        self.assertTrue(project.plan().complete)
        with self.assertRaisesRegex(ValueError, 'one release owner'):
            self.project([check('a', tests=['test/*.js']), check('b', tests=['test/*.js'])], testInventory=['test/*.js'])

    def test_write_read_and_fixture_conflicts_are_explicit(self):
        project = self.project([check('build', outputs=['public']),
            dict(check('consumer'), inputs=['public/module.js']), check('independent')])
        self.assertTrue(Gate.conflicts(project.checks['build'], project.checks['consumer']))
        self.assertFalse(Gate.conflicts(project.checks['build'], project.checks['independent']))

    def test_repeated_command_arguments_are_preserved(self):
        command = ['bun', 'test', '--file', 'one', '--file', 'two']
        project = self.project([dict(check('test'), command=command)])
        self.assertEqual(project.checks['test'].definition['command'], command)
