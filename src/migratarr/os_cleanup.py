"""Durable exact-preview cleanup batches for an inactive OS filesystem."""
import hashlib
import os
from pathlib import Path
import stat

from .os_census import census


def open_parent(root, path, original):
    if path == '/' or not path.startswith('/') or any(p in ('', '.', '..') for p in path[1:].split('/')):
        raise ValueError('invalid cleanup path')
    fd = os.open(str(root), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    logical = '/'
    try:
        for part in path[1:].split('/')[:-1]:
            info = os.fstat(fd)
            expected = original[logical]
            if (info.st_dev, info.st_ino) != tuple(expected[1:3]):
                raise ValueError('cleanup parent was replaced: ' + logical)
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd); fd = child
            logical = logical.rstrip('/') + '/' + part
        info = os.fstat(fd)
        if (info.st_dev, info.st_ino) != tuple(original[logical][1:3]):
            raise ValueError('cleanup parent was replaced: ' + logical)
        return fd, path.rsplit('/', 1)[1]
    except BaseException:
        os.close(fd)
        raise


def leaf_signature(fd, name, row):
    info = os.stat(name, dir_fd=fd, follow_symlinks=False)
    if [info.st_dev, info.st_ino, info.st_mode, info.st_uid, info.st_gid] != row[1:6]:
        raise ValueError('cleanup identity or ownership changed')
    if stat.S_ISDIR(info.st_mode):
        return {'kind': 'directory'}
    if info.st_size != row[6] or info.st_mtime_ns != row[7]:
        raise ValueError('cleanup leaf changed')
    if stat.S_ISLNK(info.st_mode):
        return {'link': os.readlink(name, dir_fd=fd)}
    if stat.S_ISREG(info.st_mode):
        descriptor = os.open(name, os.O_RDONLY | os.O_NOFOLLOW, dir_fd=fd)
        with os.fdopen(descriptor, 'rb') as handle:
            opened = os.fstat(handle.fileno())
            if (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise ValueError('cleanup file was replaced during inspection')
            digest = hashlib.sha256()
            for chunk in iter(lambda: handle.read(1024 * 1024), b''):
                digest.update(chunk)
        return {'sha256': digest.hexdigest()}
    return {'kind': 'special'}


def prepare_batch(root, original, paths):
    batch = []
    for path in paths:
        fd, name = open_parent(root, path, original)
        try:
            batch.append({'path': path, 'signature': leaf_signature(fd, name, original[path])})
        finally:
            os.close(fd)
    return batch


def apply_batch(root, original, batch):
    for candidate in batch:
        path = candidate['path']
        try:
            fd, name = open_parent(root, path, original)
        except FileNotFoundError:
            # An already removed approved ancestor can make this leaf absent.
            continue
        try:
            try:
                signature = leaf_signature(fd, name, original[path])
            except FileNotFoundError:
                continue
            if signature != candidate['signature']:
                raise ValueError('pending cleanup contents changed: ' + path)
            if original[path][0] == 'd':
                os.rmdir(name, dir_fd=fd)
            else:
                os.unlink(name, dir_fd=fd)
        finally:
            os.close(fd)


def validate_progress(root, original, removal, removed, pending, opaque):
    if type(removed) is not int or not 0 <= removed <= len(removal):
        raise ValueError('invalid cleanup checkpoint')
    if [row['path'] for row in pending] != removal[removed:removed + len(pending)]:
        raise ValueError('pending cleanup differs from the exact preview order')
    current = census(root, opaque)
    committed = set(removal[:removed])
    missing = set(original) - set(current)
    allowed_missing = committed | {row['path'] for row in pending}
    if set(current) - set(original) or not committed <= missing or missing - allowed_missing:
        raise ValueError('destination paths changed outside approved cleanup')
    unlinked = {}
    for path in missing:
        row = original[path]
        if row[0] != 'd':
            key = tuple(row[1:3]); unlinked[key] = unlinked.get(key, 0) + 1
    for path, row in current.items():
        before = original[path]
        if row[:6] != before[:6]:
            raise ValueError('destination identity or ownership changed: ' + path)
        if row[0] != 'd':
            count = unlinked.get(tuple(before[1:3]), 0)
            if row[6:8] != before[6:8] or row[9] != before[9] - count or (not count and row[8] != before[8]):
                raise ValueError('destination file changed outside cleanup: ' + path)
    # Revalidate all surviving pending contents before the first resumed unlink.
    for candidate in pending:
        path = candidate['path']
        if path not in current: continue
        fd, name = open_parent(root, path, original)
        try:
            if leaf_signature(fd, name, original[path]) != candidate['signature']:
                raise ValueError('pending cleanup contents changed: ' + path)
        finally:
            os.close(fd)


def validate_copy_paths(root, saved, opaque, extra_paths=()):
    current = census(root, opaque)
    source = saved['source']['entries']
    retained = set(saved['destination']) - set(saved['remove'])
    allowed = set(source) | retained | set(extra_paths)
    if set(current) - allowed:
        raise ValueError('unreviewed destination paths appeared during copying')
    for path, row in current.items():
        if path in source and row[0] != source[path][0]:
            raise ValueError('destination type changed during copying: ' + path)
    counts = {}
    for row in current.values():
        if row[0] != 'd':
            key = tuple(row[1:3]); counts[key] = counts.get(key, 0) + 1
    if any(row[0] != 'd' and row[9] != counts[tuple(row[1:3])] for row in current.values()):
        raise ValueError('destination links cross a protected namespace')
