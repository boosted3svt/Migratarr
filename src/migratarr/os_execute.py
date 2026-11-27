"""Checkpointed whole-OS execution for the offline disposable-VM contract."""
import contextlib
import errno
import json
import os
from pathlib import Path
import signal
import stat
import subprocess
import zlib

from .os_boot import generated_paths
from .os_cleanup import prepare_batch, apply_batch, validate_progress, validate_copy_paths
from .recovery import _directory, _fingerprint, _json, _locked, _now, _read, _write, _write_json
from .os_replace import (OPAQUE, HEADROOM, context, snapshot, remote_evidence,
                         copy_scope, verify_scope, PayloadMismatch, clean_scratch, reconcile, release)

BATCH_SIZE = 64
STAGES = ('cleanup', 'copy_root', 'copy_boot', 'reconcile', 'ready_for_vm_boot')


class OsInterrupted(Exception):
    pass


@contextlib.contextmanager
def interrupt_handlers():
    previous = {}
    def interrupted(signum, frame): raise OsInterrupted()
    try:
        for sig in (signal.SIGTERM, signal.SIGINT):
            previous[sig] = signal.signal(sig, interrupted)
        yield
    finally:
        for sig, handler in previous.items(): signal.signal(sig, handler)


def load_preview(path, job, digest):
    data = _read(path / 'os-preview.z', 64 * 1024 * 1024)
    if _fingerprint(data) != digest:
        raise ValueError('OS preview digest differs')
    decoder = zlib.decompressobj()
    raw = decoder.decompress(data, 128 * 1024 * 1024 + 1)
    if len(raw) > 128 * 1024 * 1024 or not decoder.eof or decoder.unused_data:
        raise ValueError('invalid or oversized OS preview')
    saved = json.loads(raw)
    if (saved.get('job_id') != job['job_id'] or saved.get('target_uuid') != job['target_uuid']
            or saved.get('scope') != 'offline_qemu_whole_os_only' or saved.get('migration_ready') is not False):
        raise ValueError('OS preview belongs to a different job or target')
    return saved


def load_state(path, job, saved, digest):
    state = _json(path / 'os-transfer.json')
    if (state.get('schema_version') != 2 or state.get('job_id') != job['job_id']
            or state.get('target_uuid') != job['target_uuid'] or state.get('preview_sha256') != digest
            or state.get('scope') != saved['scope'] or state.get('migration_ready') is not False
            or state.get('stage') not in STAGES or type(state.get('removed')) is not int
            or not 0 <= state['removed'] <= len(saved['remove'])
            or not isinstance(state.get('pending_cleanup'), list)
            or len(state['pending_cleanup']) > BATCH_SIZE
            or state.get('completed_scopes') not in ([], ['root'], ['root', 'boot'])):
        raise ValueError('invalid whole-OS resume checkpoint; legacy jobs support reconciliation-only retry')
    if state.get('phase') not in STAGES + ('failed', 'interrupted') or type(state.get('attempt')) is not int or state['attempt'] < 1:
        raise ValueError('invalid whole-OS execution status')
    if state['stage'] in ('cleanup', 'copy_root') and state['completed_scopes']:
        raise ValueError('checkpoint claims completion before its copy stage')
    if state['stage'] != 'cleanup' and (state['removed'] != len(saved['remove']) or state['pending_cleanup']):
        raise ValueError('copy checkpoint has unfinished cleanup')
    if state['stage'] == 'copy_boot' and state['completed_scopes'] != ['root']:
        raise ValueError('boot copy checkpoint has no durable root completion')
    if state['stage'] in ('reconcile', 'ready_for_vm_boot') and state['completed_scopes'] != ['root', 'boot']:
        raise ValueError('reconciliation checkpoint has unfinished filesystem copies')
    return state


def temporary_headroom(saved, scope, scratch):
    maximum = max((row[6] for name, row in saved['source']['entries'].items()
                   if row[0] == 'f' and name.startswith('/boot/') == (scope == 'boot')), default=0)
    fs = os.statvfs(scratch)
    if fs.f_bavail * fs.f_frsize < maximum + HEADROOM:
        raise ValueError('insufficient temporary headroom for source file replacement')


def execute(path, job, source, saved, state, bandwidth, resuming):
    if type(bandwidth) is not int or not 0 <= bandwidth <= 1048576:
        raise ValueError('invalid VM transfer bandwidth limit')
    checkpoint = path / 'os-transfer.json'
    reserve = path / '.os-checkpoint-reserve'
    if reserve.exists():
        _read(reserve, 32 * 1024 * 1024)
        if reserve.stat().st_nlink != 1: raise ValueError('checkpoint reserve has external links')
    else:
        _write(reserve, b'\0' * (16 * 1024 * 1024))
    log = Path('/run/migratarr/os-transfer.log')
    if not log.exists(): _write(log, b'')
    state.pop('error', None)
    state['attempt'] = state.get('attempt', 0) + 1
    state['phase'] = state['stage']

    def save():
        state['updated_at'] = _now()
        try:
            _write_json(checkpoint, state, replace=checkpoint.exists())
        except OSError as exc:
            if exc.errno not in (errno.ENOSPC, errno.EDQUOT): raise
            if reserve.exists(): reserve.unlink()
            state['checkpoint_reserve_released'] = True
            _write_json(checkpoint, state, replace=checkpoint.exists())

    def stage(value):
        state.update(stage=value, phase=value)
        save()

    def scratch_for(scope):
        root = path if scope == 'root' else Path('/target/boot/migratarr') / job['job_id']
        scratch = root / 'os-scratch'
        scratch.mkdir(mode=0o700, exist_ok=True); _directory(scratch)
        state['discarded_temporary_files'] = state.get('discarded_temporary_files', 0) + clean_scratch(scratch)
        return scratch

    def copy(scope):
        scratch = scratch_for(scope)
        temporary_headroom(saved, scope, scratch)
        copy_scope(source, saved['source_nonce'], scope, scratch, log, bandwidth=bandwidth)
        clean_scratch(scratch)

    save()
    with interrupt_handlers():
        try:
            if state['stage'] == 'cleanup':
                if resuming:
                    validate_progress('/target', saved['destination'], saved['remove'],
                                      state['removed'], state['pending_cleanup'], OPAQUE)
                while state['removed'] < len(saved['remove']):
                    if not state['pending_cleanup']:
                        names = saved['remove'][state['removed']:state['removed'] + BATCH_SIZE]
                        state['pending_cleanup'] = prepare_batch('/target', saved['destination'], names)
                        save()  # Intent is durable before any selected unlink.
                    apply_batch('/target', saved['destination'], state['pending_cleanup'])
                    subprocess.run(['sync'], check=True, timeout=60)
                    state['removed'] += len(state['pending_cleanup'])
                    state['pending_cleanup'] = []
                    save()  # Only a synced batch becomes a completed prefix.
                stage('copy_root')
            extra = generated_paths(saved['source']['kernels']) if state['stage'] in ('reconcile', 'ready_for_vm_boot') else ()
            validate_copy_paths('/target', saved, OPAQUE, extra)
            if state['stage'] == 'ready_for_vm_boot':
                return state  # Never replace an already reconciled initramfs.
            if resuming:
                # Only unfinished/damaged payloads are written again. A scope's
                # completion record alone is not proof after a machine restart.
                for scope in list(state['completed_scopes']):
                    try: verify_scope(source, saved['source_nonce'], scope, scratch_for(scope))
                    except PayloadMismatch:
                        copy(scope)
                        verify_scope(source, saved['source_nonce'], scope, scratch_for(scope))
            if state['stage'] == 'copy_root':
                copy('root'); state['completed_scopes'] = ['root']; stage('copy_boot')
            if state['stage'] == 'copy_boot':
                copy('boot'); state['completed_scopes'] = ['root', 'boot']; stage('reconcile')
            if state['stage'] == 'reconcile':
                state['boot'] = reconcile(job, saved['source']['kernels'], log)
                if release('/target/etc/os-release') != ['ubuntu', '20.04']:
                    raise ValueError('copied OS release differs from frozen source')
                state['payload_verified'] = True
                stage('ready_for_vm_boot')
            return state
        except OsInterrupted:
            subprocess.run(['sync'], check=True, timeout=60)
            state['phase'] = 'interrupted'; save(); return state
        except BaseException as exc:
            state.update(phase='failed', error=str(exc)); save(); raise


def start_os(token, digest, bandwidth=0):
    path, job, source = context(token)
    with _locked(path):
        if (path / 'os-transfer.json').exists():
            raise ValueError('OS transfer already started; use os-resume-vm')
        saved = load_preview(path, job, digest)
        if snapshot(job, source, saved['source_nonce']) != saved:
            raise ValueError('OS preview is stale; no deletion started')
        state = {'schema_version': 2, 'job_id': job['job_id'], 'target_uuid': job['target_uuid'],
                 'scope': saved['scope'], 'migration_ready': False, 'preview_sha256': digest,
                 'stage': 'cleanup', 'phase': 'cleanup', 'removed': 0,
                 'pending_cleanup': [], 'completed_scopes': [], 'attempt': 0}
        return execute(path, job, source, saved, state, bandwidth, False)


def resume_os(token, digest, bandwidth=0):
    path, job, source = context(token)
    with _locked(path):
        saved = load_preview(path, job, digest)
        state = load_state(path, job, saved, digest)
        if remote_evidence(source, saved['source_nonce']) != saved['source']:
            raise ValueError('frozen source evidence changed; resume refused')
        return execute(path, job, source, saved, state, bandwidth, True)
