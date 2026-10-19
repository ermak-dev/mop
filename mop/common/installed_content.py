"""Read-only installed content: общий verifier node broker и body MCP client.

Не выдаёт admission, identity, token или физический attachment. Здесь нет
machine API, процессов или imports из node/client; только immutable bytes.
"""
import base64
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import re
import stat
from uuid import UUID

TOOLS = ('python', 'git', 'ssh', 'ansible', 'runner', 'ca', 'ansible_config')
DIRECTORIES = ('empty', 'git_exec', 'python_root', 'ansible_plugins')
NATIVE_TOOLS = (*TOOLS, 'native', 'tmux', 'hook', 'mcp')
MCP_SOURCES = (
    'bin/mop', 'mop/__init__.py', 'mop/common/__init__.py',
    'mop/common/installed_content.py', 'mop/client/body_entry.py', 'mop/session.py',
    'mop/client/__init__.py', 'mop/client/control.py', 'mop/client/body_management.py',
    'mop/client/action_catalogue.py', 'mop/common/management_context.py',
    'mop/common/telemetry.py', 'mop/cli/__init__.py', 'mop/cli/_mcp.py',
    'mop/cli/_control.py', 'mop/cli/service/host-body-mcp.py',
)
_FIELDS = ('st_dev', 'st_ino', 'st_mode', 'st_uid', 'st_gid', 'st_nlink',
           'st_size', 'st_mtime_ns', 'st_ctime_ns')


def exact(value, keys):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError('finite installed content schema required')


def path(value):
    if (type(value) is not str or not value.startswith('/') or '..' in Path(value).parts
            or len(value) > 240 or not value.isascii() or any(c.isspace() for c in value)
            or Path(value) == Path('/')):
        raise ValueError('fixed absolute installed runtime path required')
    return Path(value)


def sha(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('installed content SHA256 required')


def directory(filename, *, root_owned=True):
    """No-follow walk; production не принимает writable ancestors."""
    filename = path(str(filename)) if Path(filename) != Path('/') else Path('/')
    fd = os.open('/', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
    try:
        for part in filename.parts[1:]:
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = child
            value = os.fstat(fd)
            if root_owned and (value.st_uid != 0 or value.st_mode & 0o022):
                raise ValueError('root-owned immutable installed ancestors required')
        if os.fstat(fd).st_mode & 0o022:
            raise ValueError('installed directory permissions mismatch')
        result, fd = fd, None
        return result
    finally:
        if fd is not None:
            os.close(fd)


def verify_artifact(filename, expected, *, root_owned=True, executable=False, native_size=None):
    """Hash полных bounded bytes, стабильные metadata и single-link inode."""
    if native_size is not None and (type(native_size) is not int
            or not 0 < native_size <= 536870912 or executable is not True):
        raise ValueError('exact bounded executable native artifact size required')
    filename = path(str(filename))
    sha(expected)
    parent = directory(filename.parent, root_owned=root_owned)
    try:
        fd = os.open(filename.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_nlink != 1
                    or root_owned and (before.st_uid != 0 or before.st_mode & 0o022)
                    or before.st_mode & 0o6000 or executable and not before.st_mode & 0o111
                    or not 0 <= before.st_size <= (536870912 if native_size is not None else 67108864)
                    or native_size is not None and before.st_size != native_size):
                raise ValueError('immutable bounded single-link installed runtime artifact required')
            digest, remaining = hashlib.sha256(), before.st_size
            while remaining:
                raw = os.read(fd, min(65536, remaining))
                if not raw:
                    raise ValueError('installed runtime artifact truncated')
                digest.update(raw)
                remaining -= len(raw)
            after = os.fstat(fd)
            if (any(getattr(before, key) != getattr(after, key) for key in _FIELDS)
                    or os.read(fd, 1) or digest.hexdigest() != expected):
                raise ValueError('installed runtime artifact content changed')
            return expected
        finally:
            os.close(fd)
    except OSError as error:
        raise ValueError('installed runtime artifact unavailable: '+type(error).__name__) from None
    finally:
        os.close(parent)


def read_immutable(filename, *, limit):
    filename = path(str(filename))
    parent = directory(filename.parent)
    try:
        fd = os.open(filename.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK, dir_fd=parent)
        try:
            before = os.fstat(fd)
            if (not stat.S_ISREG(before.st_mode) or before.st_uid != 0 or before.st_mode & 0o022
                    or before.st_nlink != 1 or not 0 <= before.st_size <= limit):
                raise ValueError('bounded single-link root-owned immutable installed file required')
            raw = os.read(fd, limit+1)
            after = os.fstat(fd)
            if len(raw) != before.st_size or any(getattr(before, k) != getattr(after, k) for k in _FIELDS):
                raise ValueError('installed content changed during bounded read')
            return raw
        finally:
            os.close(fd)
    finally:
        os.close(parent)


def json_value(raw):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError('duplicate installed content field')
            result[key] = value
        return result
    try:
        return json.loads(raw, object_pairs_hook=unique)
    except (UnicodeError, RecursionError):
        raise ValueError('invalid installed content JSON') from None


@dataclass(frozen=True)
class RuntimeContent:
    """Только content projection. Capacity/image admission остаётся у node."""
    tools: dict
    directories: dict
    artifacts: tuple

    @classmethod
    def parse(cls, value):
        exact(value, ('schema_version', 'capacity', 'images', 'tools', 'directories', 'artifacts'))
        if type(value['schema_version']) is not int or value['schema_version'] not in (1, 2):
            raise ValueError('unsupported installed runtime manifest')
        native = value['schema_version'] == 2
        tool_names = NATIVE_TOOLS if native else TOOLS
        exact(value['tools'], tool_names)
        exact(value['directories'], DIRECTORIES)
        tools = {k: path(v) for k, v in value['tools'].items()}
        directories = {k: path(v) for k, v in value['directories'].items()}
        artifacts, names = value['artifacts'], set()
        if type(artifacts) is not list or not 1 <= len(artifacts) <= 16384:
            raise ValueError('explicit bounded runtime content closure required')
        for item in artifacts:
            if native and type(item) is dict and item.get('path') == str(tools['native']):
                exact(item, ('path', 'sha256', 'executable', 'kind', 'size'))
                if (item['kind'] != 'native' or item['executable'] is not True
                        or type(item['size']) is not int or not 0 < item['size'] <= 536870912):
                    raise ValueError('exact native-only installed artifact classification required')
            else:
                exact(item, ('path', 'sha256', 'executable'))
            filename = path(item['path'])
            sha(item['sha256'])
            if type(item['executable']) is not bool or filename in names:
                raise ValueError('unique installed runtime artifact required')
            names.add(filename)
        if not set(tools.values()) <= names:
            raise ValueError('every fixed runtime executable/input requires an actual content hash')
        executables = {item['path'] for item in artifacts if item['executable']}
        if any(str(tools[k]) not in executables for k in tool_names if k not in ('ca', 'ansible_config')):
            raise ValueError('every fixed runtime executable requires executable content proof')
        return cls(tools, directories, tuple(artifacts))

    def verify_tree(self, root, *, maximum):
        names = {item['path'] for item in self.artifacts}
        pending, count = [root], 0
        while pending:
            current = pending.pop()
            fd = directory(current)
            try:
                with os.scandir(fd) as entries:
                    for entry in entries:
                        count += 1
                        if count > maximum:
                            raise ValueError('installed runtime closure exceeds bound')
                        filename = current/entry.name
                        if entry.is_dir(follow_symlinks=False):
                            pending.append(filename)
                        elif not entry.is_file(follow_symlinks=False) or str(filename) not in names:
                            raise ValueError('unhashed installed runtime entry is unavailable')
            finally:
                os.close(fd)

    def verify(self):
        for item in self.artifacts:
            verify_artifact(item['path'], item['sha256'], executable=item['executable'],
                            native_size=item.get('size') if item.get('kind') == 'native' else None)
        for key, root in self.directories.items():
            fd = directory(root)
            try:
                if key == 'empty':
                    with os.scandir(fd) as entries:
                        if next(entries, None) is not None:
                            raise ValueError('installed Git templates/hooks directory must be empty')
                    continue
            finally:
                os.close(fd)
            self.verify_tree(root, maximum=20000)

    def require_mcp(self, release):
        required = {str(release/name) for name in MCP_SOURCES}
        if (set(self.tools) != set(NATIVE_TOOLS) or self.tools['mcp'] != release/'bin/mop'
                or self.tools['hook'] != release/'mop/session.py'
                or self.tools['runner'] != release/'mop/node/host_body.py'
                or not required <= {item['path'] for item in self.artifacts}):
            raise ValueError('native runtime requires the exact isolated MCP/hook entry and complete standby source closure')


def scope_launch(value):
    """Reversible full HOST address нужен только для fixed private siblings."""
    if type(value) is not str or not re.fullmatch(r'mop-host-[a-z2-7]{154}\.service', value):
        raise ValueError('UUID-derived HOST unit required')
    raw = base64.b32decode(value[9:-8].upper()+'======')
    if 'mop-host-'+base64.b32encode(raw).decode().rstrip('=').lower()+'.service' != value:
        raise ValueError('noncanonical HOST unit')
    return str(UUID(bytes=raw[-16:]))


def mcp_profile(release):
    """Чтение установленного профиля, не node operation/authority construction."""
    value = json_value(read_immutable(release/'host-phase.json', limit=8192))
    exact(value, ('schema_version', 'installed', 'runtime_manifest', 'ingress', 'snapshots', 'control_root'))
    if type(value['schema_version']) is not int or value['schema_version'] != 1:
        raise ValueError('unsupported installed HOST phase configuration')
    installed = value['installed']
    exact(installed, ('node_uid', 'body_uid', 'body_gid', 'runner', 'body_root', 'node_private'))
    if (any(type(installed[k]) is not int or not 0 < installed[k] < 2**31
            for k in ('node_uid', 'body_uid', 'body_gid'))
            or installed['node_uid'] == installed['body_uid']
            or (os.geteuid(), os.getegid()) != (installed['body_uid'], installed['body_gid'])
            or path(installed['runner']) != release/'mop/node/host_body.py'):
        raise ValueError('actual installed body UID/GID and fixed lifecycle broker required')
    body_root = path(installed['body_root'])
    content = RuntimeContent.parse(json_value(read_immutable(path(value['runtime_manifest']), limit=4194304)))
    content.require_mcp(release)
    content.verify()
    content.verify_tree(release/'mop/cli', maximum=4096)
    return body_root, content
