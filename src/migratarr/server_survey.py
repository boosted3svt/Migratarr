"""Bounded, read-only whole-server identity and storage configuration metadata."""
import os
import re
import stat

MAX_READ_BYTES = 8 * 1024 * 1024
CONFIG_PATHS = ('/etc/fstab', '/etc/crypttab', '/etc/shadow', '/etc/gshadow',
                '/etc/machine-id', '/etc/ssh/sshd_config')


def _number(value):
    if not value.isdecimal():
        raise ValueError('invalid numeric metadata')
    return int(value)


def parse_passwd(text):
    rows = []
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        fields = line.split(':')
        if len(fields) != 7 or not fields[0]:
            raise ValueError('invalid account metadata')
        rows.append({'name': fields[0], 'uid': _number(fields[2]), 'gid': _number(fields[3]),
                     'home': fields[5], 'shell': fields[6]})
    return rows


def parse_group(text):
    rows = []
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        fields = line.split(':')
        if len(fields) != 4 or not fields[0]:
            raise ValueError('invalid group metadata')
        rows.append({'name': fields[0], 'gid': _number(fields[2]),
                     'members': fields[3].split(',') if fields[3] else []})
    return rows


def parse_subids(text):
    rows = []
    for line in text.splitlines():
        if not line or line.startswith('#'):
            continue
        fields = line.split(':')
        if len(fields) != 3 or not fields[0]:
            raise ValueError('invalid subordinate identity metadata')
        rows.append({'name': fields[0], 'start': _number(fields[1]), 'count': _number(fields[2])})
    return rows


def _unescape(value):
    return re.sub(r'\\([0-7]{3})', lambda match: chr(int(match[1], 8)), value)


def _source(value, filesystem=None):
    if filesystem in {'nfs', 'nfs4', 'cifs', 'smb3', 'sshfs', 'fuse.sshfs'}:
        return {'source_kind': 'remote'}
    for prefix in ('UUID=', 'LABEL=', 'PARTUUID=', 'PARTLABEL='):
        if value.startswith(prefix):
            return {'source_kind': prefix[:-1].lower(), 'source_reference': value}
    if value.startswith('/dev/'):
        return {'source_kind': 'device', 'source_reference': value}
    return {'source_kind': 'other_redacted'}


def parse_fstab(text):
    rows = []
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        fields = line.split('#', 1)[0].split()
        if not 4 <= len(fields) <= 6:
            raise ValueError('invalid fstab metadata')
        source, target, filesystem = map(_unescape, fields[:3])
        options = []
        for item in fields[3].split(','):
            name = item.partition('=')[0]
            if not re.fullmatch(r'[A-Za-z0-9_.:-]+', name):
                raise ValueError('invalid option metadata')
            options.append(name)
        rows.append({'target': target, 'filesystem': filesystem, 'option_names': options,
                     'dump': _number(fields[4]) if len(fields) > 4 else 0,
                     'pass': _number(fields[5]) if len(fields) > 5 else 0,
                     **_source(source, filesystem)})
    return rows


def parse_swaps(text):
    lines = text.splitlines()
    if not lines or lines[0].split() != ['Filename', 'Type', 'Size', 'Used', 'Priority']:
        raise ValueError('invalid swap header')
    rows = []
    for line in lines[1:]:
        if not line.strip():
            continue
        fields = line.split()
        if len(fields) != 5 or not re.fullmatch(r'-?\d+', fields[4]):
            raise ValueError('invalid swap metadata')
        rows.append({'path': _unescape(fields[0]), 'type': fields[1], 'size_kib': _number(fields[2]),
                     'used_kib': _number(fields[3]), 'priority': int(fields[4])})
    return rows


def parse_cmdline(text):
    rows = []
    for token in text.split():
        key, separator, value = token.partition('=')
        if key in {'root', 'resume'} and separator:
            rows.append({'key': key, **_source(value)})
        elif key == 'rootfstype' and separator and re.fullmatch(r'[A-Za-z0-9_,.-]+', value):
            rows.append({'key': key, 'value': value})
        elif key in {'ro', 'rw'} and not separator:
            rows.append({'key': key})
    return rows


def survey():
    errors = []

    def read(path, parser, optional=False):
        try:
            with open(path, 'rb') as handle:
                data = handle.read(MAX_READ_BYTES + 1)
            if len(data) > MAX_READ_BYTES:
                raise ValueError('metadata too large')
            return parser(data.decode('utf-8'))
        except FileNotFoundError:
            if not optional:
                errors.append({'probe': 'server_survey', 'path': path, 'error': 'FileNotFoundError'})
        except OSError as exc:
            errors.append({'probe': 'server_survey', 'path': path, 'error': type(exc).__name__})
        except (ValueError, UnicodeError):
            errors.append({'probe': 'server_survey', 'path': path, 'error': 'invalid or oversized metadata'})
        return None

    metadata = {}
    for path in CONFIG_PATHS:
        try:
            item = os.lstat(path)
            metadata[path] = {'present': True, 'uid': item.st_uid, 'gid': item.st_gid,
                              'mode': oct(stat.S_IMODE(item.st_mode)), 'size_bytes': item.st_size,
                              'is_symlink': stat.S_ISLNK(item.st_mode)}
        except FileNotFoundError:
            metadata[path] = {'present': False}
        except OSError as exc:
            metadata[path] = None
            errors.append({'probe': 'server_survey', 'path': path, 'error': type(exc).__name__})
    return {'local_users': read('/etc/passwd', parse_passwd),
            'local_groups': read('/etc/group', parse_group),
            'subordinate_ids': {'uid': read('/etc/subuid', parse_subids, True),
                                'gid': read('/etc/subgid', parse_subids, True)},
            'fstab': read('/etc/fstab', parse_fstab), 'swaps': read('/proc/swaps', parse_swaps),
            'boot_references': read('/proc/cmdline', parse_cmdline), 'config_metadata': metadata,
            'errors': errors,
            'limitations': ['Local identity files only; NSS/directory accounts and user service managers remain unresolved.',
                            'Numeric identity records do not verify per-file ownership, ACLs, xattrs or capabilities.',
                            'Passwords, GECOS, remote mount sources, option values and arbitrary kernel arguments are omitted.',
                            'No application-specific config, database, shadow, key, machine-id or sshd_config contents are read.',
                            'No recursive filesystem scan, bootloader validation or data-size measurement is performed.']}
