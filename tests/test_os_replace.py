import ast
from pathlib import Path
import tempfile
import unittest
import subprocess
from unittest.mock import patch

from migratarr.os_census import census, removal_paths
from migratarr.os_replace import preview_os, source_code, grub_config, scope_filters, verify_scope, PayloadMismatch


class OsReplacementTests(unittest.TestCase):
    def test_payload_verification_allows_directory_mtime_only(self):
        source = {'host': '192.0.2.10', 'user': 'root', 'port': 22,
                  'identity_file': '/run/key', 'known_hosts': '/run/hosts'}
        with patch('migratarr.os_replace.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '.d..t...... etc/\n', '')):
            verify_scope(source, 'a' * 32, 'root', Path('/private/scratch'))
        for output in ('>f+++++++++ missing-kernel\n', '.d...p..... etc/\n', '>fc........ changed-data\n'):
            with patch('migratarr.os_replace.subprocess.run', return_value=subprocess.CompletedProcess([], 0, output, '')):
                with self.assertRaises(PayloadMismatch):
                    verify_scope(source, 'a' * 32, 'root', Path('/private/scratch'))

    def test_census_never_follows_links_or_enters_opaque_namespaces(self):
        with tempfile.TemporaryDirectory() as name:
            root = Path(name)
            (root / 'protected').mkdir()
            (root / 'protected/secret').write_text('excluded')
            (root / 'link').symlink_to('/outside')
            result = census(root, ['/protected'])
            self.assertEqual(set(result), {'/', '/link'})
            self.assertEqual(result['/link'][0], 'l')
            with self.assertRaises(ValueError): census(root, [], max_entries=1)

    def test_exact_removal_includes_type_conflicts_and_retains_guard_ancestors(self):
        source = {'/': ['d'], '/keep': ['f'], '/change': ['l']}
        dest = {'/': ['d'], '/keep': ['f'], '/change': ['d'], '/change/old': ['f'],
                '/old': ['f'], '/var': ['d'], '/var/lib': ['d']}
        self.assertEqual(removal_paths(source, dest, ['/var/lib/migratarr']), ['/change/old', '/change', '/old'])
        with self.assertRaises(ValueError):
            removal_paths({'/var': ['l']}, dest, ['/var/lib/migratarr'])

    def test_source_guard_and_boot_menu_are_fixed_and_uuid_bound(self):
        for scope in (None, 'root', 'boot'):
            code = source_code('a' * 32, scope)
            ast.parse(code, feature_version=8)
            self.assertIn("'/source'", code)
            self.assertIn("'ro'", code)
        with self.assertRaises(ValueError): source_code('bad')
        config = grub_config('5.4.0-216-generic', '1' * 8 + '-1111-1111-1111-' + '1' * 12,
                             '2' * 8 + '-2222-2222-2222-' + '2' * 12, '3' * 8 + '-3333-3333-3333-' + '3' * 12)
        self.assertIn('root=UUID=11111111-', config)
        self.assertIn('Migratarr RAM recovery', config)
        self.assertIn('/vmlinuz-5.4.0-216-generic', config)
        self.assertIn('/boot/*', scope_filters('root'))
        self.assertIn('/grub', scope_filters('boot'))

    def test_physical_host_refused_before_source_connection_or_mutation(self):
        with patch('migratarr.os_replace.fixture_context', side_effect=ValueError('physical host')), \
                patch('migratarr.os_replace.remote_evidence') as remote:
            with self.assertRaisesRegex(ValueError, 'physical host'): preview_os('a' * 32, 'b' * 32)
            remote.assert_not_called()
