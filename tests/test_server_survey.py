import io
import json
import stat
import types
import unittest
from unittest.mock import patch

from migratarr.server_survey import (MAX_READ_BYTES, parse_cmdline, parse_fstab,
                                     parse_group, parse_passwd, parse_subids,
                                     parse_swaps, survey)


class ServerSurveyTests(unittest.TestCase):
    def test_accounts_exclude_passwords_and_gecos(self):
        users = parse_passwd('operator:secret-password:1000:1000:private-gecos:/home/operator:/bin/bash\n')
        groups = parse_group('media:secret-password:1000:operator,other\n')
        self.assertEqual(users, [{'name': 'operator', 'uid': 1000, 'gid': 1000, 'home': '/home/operator', 'shell': '/bin/bash'}])
        self.assertEqual(groups, [{'name': 'media', 'gid': 1000, 'members': ['operator', 'other']}])
        self.assertNotIn('secret', json.dumps([users, groups]))
        self.assertNotIn('gecos', json.dumps([users, groups]))

    def test_fstab_strips_remote_sources_and_option_values(self):
        rows = parse_fstab('UUID=root / ext3 defaults 0 1\n//user:secret@host/share /mnt/share cifs username=private,password=secret,credentials=/secret/file 0 0\n/dev/sda1 /mnt/local\\040disk ext4 defaults\n')
        self.assertEqual(rows[0]['source_reference'], 'UUID=root')
        self.assertEqual(rows[1]['source_kind'], 'remote')
        self.assertEqual(rows[1]['option_names'], ['username', 'password', 'credentials'])
        self.assertNotIn('source_reference', rows[1])
        self.assertNotIn('secret', json.dumps(rows))
        self.assertNotIn('private', json.dumps(rows))
        self.assertEqual(rows[2]['target'], '/mnt/local disk')

    def test_cmdline_allowlist_omits_credentials_and_unknown_options(self):
        rows = parse_cmdline('root=UUID=root ro rootfstype=ext3 resume=/dev/sda2 token=secret rd.luks.key=secret quiet')
        self.assertEqual(rows, [{'key': 'root', 'source_kind': 'uuid', 'source_reference': 'UUID=root'},
                                {'key': 'ro'}, {'key': 'rootfstype', 'value': 'ext3'},
                                {'key': 'resume', 'source_kind': 'device', 'source_reference': '/dev/sda2'}])
        self.assertNotIn('secret', json.dumps(rows))

    def test_swaps_and_subordinate_ids(self):
        self.assertEqual(parse_subids('operator:100000:65536\n'), [{'name': 'operator', 'start': 100000, 'count': 65536}])
        self.assertEqual(parse_swaps('Filename Type Size Used Priority\n/swapfile file 1024 16 -2\n'),
                         [{'path': '/swapfile', 'type': 'file', 'size_kib': 1024, 'used_kib': 16, 'priority': -2}])

    def test_malformed_records_fail_closed(self):
        for parser, text in ((parse_passwd, 'operator:x:bad:1000:secret:/home/operator:/bin/sh'),
                             (parse_group, 'media:x:bad:operator'), (parse_subids, 'operator:1:bad'),
                             (parse_fstab, 'broken'), (parse_swaps, 'wrong header')):
            with self.subTest(parser=parser.__name__), self.assertRaises(ValueError):
                parser(text)

    def test_bounded_reads_and_secret_metadata_only(self):
        inputs = {'/etc/passwd': b'operator:x:1000:1000:secret:/home/operator:/bin/sh\n',
                  '/etc/group': b'media:x:1000:operator\n', '/etc/fstab': b'UUID=root / ext3 defaults 0 1\n',
                  '/proc/swaps': b'Filename Type Size Used Priority\n', '/proc/cmdline': b'root=UUID=root ro password=secret'}
        opened = []

        def fake_open(path, mode):
            opened.append(path)
            self.assertEqual(mode, 'rb')
            if path not in inputs:
                raise FileNotFoundError
            return io.BytesIO(inputs[path])

        item = types.SimpleNamespace(st_uid=0, st_gid=0, st_size=42, st_mode=stat.S_IFREG | 0o600)
        with patch('builtins.open', side_effect=fake_open), patch('migratarr.server_survey.os.lstat', return_value=item):
            result = survey()
        self.assertEqual(result['errors'], [])
        self.assertIsNone(result['subordinate_ids']['uid'])
        self.assertTrue(result['config_metadata']['/etc/shadow']['present'])
        self.assertNotIn('/etc/shadow', opened)
        self.assertNotIn('/etc/machine-id', opened)
        self.assertNotIn('secret', json.dumps(result['local_users']))

    def test_unreadable_or_oversized_is_not_empty_success(self):
        with patch('builtins.open', side_effect=PermissionError), patch('migratarr.server_survey.os.lstat', side_effect=PermissionError):
            result = survey()
        self.assertIsNone(result['local_users'])
        self.assertTrue(result['errors'])
        with patch('builtins.open', side_effect=lambda *args, **kwargs: io.BytesIO(b'x' * (MAX_READ_BYTES + 1))), patch('migratarr.server_survey.os.lstat', side_effect=FileNotFoundError):
            result = survey()
        self.assertIsNone(result['local_users'])
        self.assertTrue(any(error['error'] == 'invalid or oversized metadata' for error in result['errors']))


if __name__ == '__main__':
    unittest.main()
