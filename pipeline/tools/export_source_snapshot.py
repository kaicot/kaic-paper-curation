"""Publish committed source only, with conflict detection and resumable writes."""
from __future__ import annotations

import argparse
import hashlib
import importlib.util
import json
import os
from pathlib import Path, PurePosixPath
import subprocess
import tempfile
from datetime import datetime, timezone
from contextlib import contextmanager

ROOT = Path(__file__).resolve().parents[2]
MANIFEST = '.source-snapshot.json'
PENDING = '.source-snapshot.pending.json'
FORBIDDEN = {'.git', '.tools', '.cache', '.worktrees', '.omo', 'config.json'}


class SnapshotError(RuntimeError):
    pass


def git(root: Path, *args: str, data: bytes | None = None) -> bytes:
    result = subprocess.run(['git', '-C', str(root), *args], input=data, capture_output=True)
    if result.returncode:
        raise SnapshotError('Git could not read the requested local commit')
    return result.stdout


def digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def relative(name: str) -> Path:
    path = PurePosixPath(name)
    if not name or path.is_absolute() or '..' in path.parts or '\\' in name or ':' in name or path.parts[0] in FORBIDDEN or name in {MANIFEST, PENDING}:
        raise SnapshotError(f'unsafe source path: {name}')
    return Path(*path.parts)


def committed_files(root: Path, revision: str = 'HEAD') -> tuple[str, dict[str, bytes]]:
    commit = git(root, 'rev-parse', '--verify', revision + '^{commit}').decode().strip()
    entries = []
    for line in git(root, 'ls-tree', '-r', '-z', '--full-tree', commit).split(b'\0'):
        if not line:
            continue
        info, raw_name = line.split(b'\t', 1)
        mode, kind, oid = info.decode().split()
        name = raw_name.decode('utf-8')
        relative(name)
        if mode not in {'100644', '100755'} or kind != 'blob':
            raise SnapshotError(f'links/submodules are not source files: {name}')
        entries.append((name, oid))
    objects = git(root, 'cat-file', '--batch', data=('\n'.join(oid for _, oid in entries) + '\n').encode())
    files = {}
    offset = 0
    for name, oid in entries:
        end = objects.index(b'\n', offset)
        actual, kind, size = objects[offset:end].decode().split()
        if actual != oid or kind != 'blob':
            raise SnapshotError('Git object mismatch')
        offset = end + 1
        files[name] = objects[offset:offset + int(size)]
        offset += int(size) + 1
    return commit, files


def reject_credentials(root: Path, files: dict[str, bytes]) -> None:
    scanner = root / 'scripts/scan-secrets.py'
    if not scanner.is_file():
        raise SnapshotError('credential scanner is required before cloud export')
    spec = importlib.util.spec_from_file_location('snapshot_secret_scanner', scanner)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    for name, data in files.items():
        if module.findings(data):
            raise SnapshotError(f'credential-like content refused: {name}')


def load_record(path: Path, root: Path) -> dict:
    if not path.exists():
        return {}
    if path.is_symlink() or path.stat().st_size > 4_000_000:
        raise SnapshotError('unsafe snapshot metadata')
    record = json.loads(path.read_text(encoding='utf-8'))
    if record.get('schema_version') != 1 or record.get('source_root') != str(root):
        raise SnapshotError('snapshot belongs to a different working repository')
    return record


def atomic_file(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, raw = tempfile.mkstemp(prefix='.snapshot-', dir=path.parent)
    temporary = Path(raw)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def encoded(value: dict) -> bytes:
    return (json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n').encode('utf-8')


@contextmanager
def publication_lock(root: Path):
    directory = root / '.omo'
    directory.mkdir(exist_ok=True)
    with (directory / 'source-snapshot.lock').open('a+b') as stream:
        stream.seek(0)
        stream.write(b'0')
        stream.flush()
        stream.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(stream, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise SnapshotError('another source snapshot publication is active') from error
        try:
            yield
        finally:
            stream.seek(0)
            if os.name == 'nt':
                msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(stream, fcntl.LOCK_UN)


def export(root: Path, destination: Path, *, publish: bool = False, revision: str = 'HEAD') -> dict:
    root = root.resolve()
    if not (root / '.git').is_dir():
        raise SnapshotError('publish only from the authoritative main checkout')
    if publish:
        with publication_lock(root):
            return _export(root, destination, publish=True, revision=revision)
    return _export(root, destination, publish=False, revision=revision)


def _export(root: Path, destination: Path, *, publish: bool, revision: str) -> dict:
    for path in (destination, *destination.parents):
        if path.is_symlink() or path.is_junction():
            raise SnapshotError('snapshot destination must not pass through links')
    root, destination = root.resolve(), destination.resolve()
    if destination == root or root in destination.parents or destination in root.parents:
        raise SnapshotError('source and snapshot must be disjoint directories')
    if (destination / '.git').exists():
        raise SnapshotError('refusing to replace a Git working repository')
    commit, files = committed_files(root, revision)
    reject_credentials(root, files)
    previous = load_record(destination / MANIFEST, root)
    pending = load_record(destination / PENDING, root)
    known = {name: [value] for name, value in previous.get('files', {}).items()}
    for name, values in pending.get('known_hashes', {}).items():
        known.setdefault(name, []).extend(values)
    existing = {}
    if destination.exists():
        for parent, dirs, names in os.walk(destination):
            for name in dirs + names:
                path = Path(parent) / name
                if path.is_symlink() or path.is_junction():
                    raise SnapshotError('snapshot contains a link')
            for name in names:
                path = Path(parent) / name
                rel = path.relative_to(destination).as_posix()
                if rel not in {MANIFEST, PENDING}:
                    existing[rel] = digest(path.read_bytes())
    if not previous and not pending and existing:
        raise SnapshotError('initial snapshot destination must be empty')
    conflicts = []
    for name, hashes in known.items():
        relative(name)
        current = existing.get(name)
        if current not in hashes and not (pending and current is None):
            conflicts.append(name)
    for name in files:
        if name in existing and name not in known:
            conflicts.append(name)
    if conflicts:
        raise SnapshotError('snapshot was edited; preserved without changes: ' + ', '.join(sorted(set(conflicts))))
    desired = {name: digest(data) for name, data in files.items()}
    result = {'status': 'planned', 'commit': commit, 'files': len(files),
              'bytes': sum(map(len, files.values())), 'destination': str(destination),
              'unmanaged_files': sorted(set(existing) - set(known) - set(files))}
    if not publish:
        return result
    # Journal is published before any file mutation. A restart accepts only
    # known old/new hashes; concurrent human changes still fail closed.
    for name, value in desired.items():
        known.setdefault(name, []).append(value)
    journal = {'schema_version': 1, 'source_root': str(root), 'commit': commit,
               'known_hashes': {k: sorted(set(v)) for k, v in known.items()}}
    destination.mkdir(parents=True, exist_ok=True)
    atomic_file(destination / PENDING, encoded(journal))
    for name, data in files.items():
        if existing.get(name) != desired[name]:
            atomic_file(destination / relative(name), data)
    for name in set(known) - set(files):
        path = destination / relative(name)
        path.unlink(missing_ok=True)
        parent = path.parent
        while parent != destination:
            try:
                parent.rmdir()
            except OSError:
                break
            parent = parent.parent
    manifest = {'schema_version': 1, 'source_root': str(root), 'commit': commit,
                'created_at_utc': datetime.now(timezone.utc).isoformat(), 'files': desired}
    atomic_file(destination / MANIFEST, encoded(manifest))
    (destination / PENDING).unlink(missing_ok=True)
    for name, expected in desired.items():
        if digest((destination / relative(name)).read_bytes()) != expected:
            raise SnapshotError('snapshot verification failed: ' + name)
    return {**result, 'status': 'published'}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination', type=Path)
    parser.add_argument('--publish', action='store_true')
    args = parser.parse_args()
    try:
        destination = args.destination
        if destination is None:
            config = json.loads((ROOT / '.omo/source-snapshot-config.json').read_text(encoding='utf-8'))
            destination = Path(config['destination'])
        result = export(ROOT, destination, publish=args.publish)
        print(json.dumps(result, ensure_ascii=False))
        return 0
    except (OSError, ValueError, SnapshotError) as error:
        print(json.dumps({'status': 'blocked', 'reason': str(error)}, ensure_ascii=False))
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
