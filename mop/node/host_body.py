#!/usr/bin/python3 -ISB
"""Один immutable body-UID broker; только finite installed lifecycle modes."""
import os
from pathlib import Path
import stat
import sys

BODY_SOURCES = (
    'mop/__init__.py', 'mop/node/__init__.py', 'mop/common/__init__.py',
    'mop/node/host_body.py', 'mop/node/host_root.py', 'mop/node/host_backend.py',
    'mop/node/host_scope.py', 'mop/node/host_scope_supervisor.py', 'mop/node/host_scope_systemd.py',
    'mop/node/host_phase_supervisor.py', 'mop/node/host_phase_manifest.py',
    'mop/node/host_phase_source.py', 'mop/node/host_phases.py', 'mop/node/host_inputs.py',
    'mop/node/host_input_files.py', 'mop/node/http_execution.py', 'mop/common/initialcreate.py',
    'mop/common/bootstrap_envelope.py', 'mop/common/credentials.py', 'mop/common/soul.py',
    'mop/common/busnames.py', 'mop/common/paths.py', 'mop/common/installed_content.py',
)


def installed_entry():
    if not sys.flags.isolated or not sys.flags.no_site or os.geteuid() == 0:
        raise RuntimeError('isolated installed body UID entry required')
    release = Path(__file__).absolute().parents[2]
    if (release/'.git').exists(): raise RuntimeError('body checkout entry is forbidden')
    for name in BODY_SOURCES:
        filename = release/name
        for parent in filename.parents:
            info = parent.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or info.st_mode & 0o022:
                raise RuntimeError('immutable root-owned body release required')
        info = filename.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or info.st_uid != 0 or info.st_mode & 0o022:
            raise RuntimeError('immutable root-owned body source required')
    sys.dont_write_bytecode = True
    sys.path.insert(0, str(release))
    return release


def load_installation(release, *, cleanup=False):
    from mop.node.host_phase_supervisor import PhaseConfig
    from mop.node.host_phase_manifest import RuntimeManifest
    config = PhaseConfig.load(release/'host-phase.json')
    if (os.geteuid(), os.getegid()) != (config.installed.body_uid, config.installed.body_gid):
        raise RuntimeError('actual installed body UID/GID required')
    if config.installed.runner != Path(__file__).absolute():
        raise RuntimeError('fixed installed lifecycle broker required')
    # Cleanup не запускает Git/Ansible: Root уже проверил runner и его Python.
    # Полное хеширование toolchain отнимало bounded cleanup reserve у body UID.
    manifest = None if cleanup else RuntimeManifest.load(config.runtime_manifest)
    if manifest is not None:
        manifest.require_release(release)
    return config, manifest


def secret_read(filename):
    from mop.node.http_execution import Refused
    fd = os.open(filename, os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
    try:
        value = os.fstat(fd)
        if (not stat.S_ISREG(value.st_mode) or value.st_nlink != 1 or value.st_uid != os.geteuid()
                or stat.S_IMODE(value.st_mode) != 0o600 or not 0 < value.st_size < 65536):
            raise Refused('fixed private body source input required')
        raw = os.read(fd, 65536)
        after = os.fstat(fd)
        fields = ('st_dev', 'st_ino', 'st_uid', 'st_gid', 'st_mode', 'st_nlink', 'st_size', 'st_mtime_ns', 'st_ctime_ns')
        if len(raw) != value.st_size or any(getattr(value, key) != getattr(after, key) for key in fields):
            raise Refused('body source input changed')
        return raw
    finally: os.close(fd)


def helper(config, manifest, mode, unit):
    """Git helper только с fixed derived secret files; consumer name не используется."""
    from mop.node.host_scope import Scope
    from mop.node.host_inputs import _json
    from mop.node.host_phase_source import SourceCredential, source_origin
    scope = Scope.from_unit(unit)
    body = config.installed.body(scope)
    value = _json(secret_read(body/'.phase/source.json'))
    source = SourceCredential.parse(value, value['origin'])
    if mode == 'askpass':
        if source.format not in ('git-https-basic-v1', 'git-https-token-v1') or len(sys.argv) != 2:
            raise RuntimeError('fixed Git HTTPS askpass required')
        prompt = sys.argv[1]
        if prompt.startswith('Username for '): result = source.username
        elif prompt.startswith('Password for '): result = source.password
        else: raise RuntimeError('unknown Git credential prompt')
        sys.stdout.write(result+'\n')
        sys.stdout.flush()
        return
    if mode != 'ssh' or source.format != 'git-ssh-key-v1':
        raise RuntimeError('fixed source helper mode required')
    from mop.node.host_phase_source import ssh_command
    # Git -G discovery выключен GIT_SSH_VARIANT; ровно upload-pack exact origin.
    argv = ssh_command(source, tuple(sys.argv[1:]), manifest.tools['ssh'], body)
    os.execve(argv[0], argv, dict(PATH='/usr/bin:/bin', HOME=str(body), LANG='C.UTF-8'))


class Broker:
    def __init__(self, config, manifest, scope, reader, writer, peer, process):
        self.config, self.manifest, self.scope = config, manifest, scope
        self.reader, self.writer, self.peer, self.process = reader, writer, peer, process
        self.body = config.installed.body(scope)
        self.repo, self.temp = self.body/'repo', self.body/'.phase'
        self.active = None
        self.effect = None

    async def checkpoint(self, effect, sequence):
        from mop.node.host_scope import encode, decode
        from mop.node.host_scope_supervisor import peer_credentials, read_frame
        from mop.node.host_backend import Process
        from mop.node.http_execution import Refused
        if (peer_credentials(self.writer.get_extra_info('socket')) != self.peer
                or self.peer[1] != 0 or Process.read(self.peer[0]) != self.process):
            raise Refused('body checkpoint lost its pinned root process')
        a = self.active.admission
        self.writer.write(encode(dict(schema_version=1, action='body_checkpoint', effect=effect,
            sequence=sequence, attempt_id=a.attempt_id))+b'\n')
        await self.writer.drain()
        value = decode(await read_frame(self.reader))
        from mop.node.host_phases import require_frame
        require_frame(value, dict(schema_version=1, approved=effect, sequence=sequence, attempt_id=a.attempt_id))
        if (peer_credentials(self.writer.get_extra_info('socket')) != self.peer
                or Process.read(self.peer[0]) != self.process):
            raise Refused('body checkpoint root process changed')
        from mop.node.host_inputs import validate_admission
        validate_admission(a)
        self.effect = effect

    def write(self, filename, data):
        fd = os.open(filename, os.O_WRONLY|os.O_CREAT|os.O_EXCL|os.O_NOFOLLOW|os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(fd, 'wb', closefd=False) as stream:
                stream.write(data)
                stream.flush()
                os.fsync(fd)
        finally: os.close(fd)

    def environment(self):
        from mop.node.host_phase_source import git_environment
        env = git_environment(self.body, self.manifest.directories['empty'], self.manifest.tools['ca'])
        env['GIT_EXEC_PATH'] = str(self.manifest.directories['git_exec'])
        return env

    async def command(self, argv, *, env=None, cwd=None):
        import asyncio
        from mop.node.http_execution import Refused
        process = await asyncio.create_subprocess_exec(*map(str, argv), cwd=cwd or self.repo,
            env=self.environment() if env is None else env, stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        # stdout ограничен; stderr source/Ansible никогда не попадает в telemetry.
        try:
            raw = await process.stdout.read(65537)
            if len(raw) > 65536:
                raise Refused('planned tool output exceeds fixed bound')
            await process.wait()
            if process.returncode != 0:
                raise Refused('planned '+str(self.effect)+' installed tool failed (exit '+str(process.returncode)+'); output withheld')
            return raw
        finally:
            if process.returncode is None:
                process.kill()
                await process.wait()

    def git(self):
        from mop.node.host_phase_source import git_prefix
        return git_prefix(self.manifest.tools['git'], self.manifest.directories['empty'])

    async def prepare(self, value):
        import base64
        import json
        from mop.node.host_inputs import _json, canonical_json
        from mop.node.host_phase_source import SourceCredential
        from mop.node.http_execution import Refused
        source = SourceCredential.parse(_json(base64.b64decode(value['consumers'][0]['value'], validate=True)), value['selection']['source']['origin'])
        await self.checkpoint('source-input', 2)
        self.temp.mkdir(mode=0o700)
        self.write(self.temp/'source.json', canonical_json(_json(base64.b64decode(value['consumers'][0]['value'], validate=True))))
        names = ['source.json']
        if source.format == 'git-ssh-key-v1':
            self.write(self.temp/'id_ed25519', source.private_key.encode())
            self.write(self.temp/'known_hosts', source.known_hosts.encode())
            names += ['id_ed25519', 'known_hosts']
        env = self.environment()
        env.update(MOP_PHASE_SCOPE=self.scope.unit, MOP_PHASE_HELPER='ssh' if source.format == 'git-ssh-key-v1' else 'askpass')
        if source.format == 'git-ssh-key-v1':
            env.update(GIT_SSH=str(self.config.installed.runner), GIT_SSH_VARIANT='ssh')
        else: env['GIT_ASKPASS'] = str(self.config.installed.runner)
        await self.checkpoint('git-init', 3)
        self.repo.mkdir(mode=0o700)
        await self.command((*self.git(), 'init', '--template='+str(self.manifest.directories['empty']), '--', str(self.repo)), cwd=self.body)
        await self.checkpoint('git-origin', 4)
        await self.command((*self.git(), 'remote', 'add', '--', 'origin', source.origin))
        await self.checkpoint('git-fetch', 5)
        commit = value['selection']['source']['commit']
        await self.command((*self.git(), 'fetch', '--no-tags', '--no-recurse-submodules', '--depth=1', '--', 'origin', commit), env=env)
        await self.checkpoint('git-checkout', 6)
        await self.command((*self.git(), 'checkout', '--detach', '--force', commit, '--'))
        head = (await self.command((*self.git(), 'rev-parse', '--verify', 'HEAD'))).decode().strip()
        origin = (await self.command((*self.git(), 'remote', 'get-url', '--', 'origin'))).decode().strip()
        if head != commit or origin != source.origin:
            raise Refused('actual Git HEAD/origin differs from selected exact commit')
        await self.checkpoint('source-remove', 7)
        for name in names: (self.temp/name).unlink()
        self.temp.rmdir()

    async def bootstrap(self, value):
        import base64
        import json
        from mop.common.bootstrap_envelope import decode_base64
        from mop.node.http_execution import Refused
        retained = decode_base64(value['bootstrap']['envelope'], value['bootstrap_digest'])
        # YAML только body UID, только approved retained bytes, не checkout files.
        sys.path.insert(0, str(self.manifest.directories['python_root']))
        import yaml
        tasks, variables = yaml.safe_load(retained.tasks), yaml.safe_load(retained.variables)
        if type(tasks) is not list or not tasks or type(variables) is not dict:
            raise Refused('supported bootstrap requires nonempty task list and variables mapping')
        allowed = {'ansible.builtin.copy', 'ansible.builtin.file', 'ansible.builtin.command', 'ansible.builtin.fail'}
        for task in tasks:
            if (type(task) is not dict or len(set(task) & allowed) != 1
                    or set(task) - allowed - {'name', 'register', 'when', 'loop', 'no_log'}):
                raise Refused('bootstrap task uses unsupported plugin/control override')
        if any(type(k) is not str or k.startswith('ansible_') for k in variables):
            raise Refused('bootstrap variables cannot override installed execution authority')
        consumers = {item['version_id']: base64.b64decode(item['value'], validate=True).decode('utf-8') for item in value['consumers']}
        variables['mop_bootstrap_inputs'] = consumers
        variables['ansible_python_interpreter'] = str(self.manifest.tools['python'])
        await self.checkpoint('bootstrap-input', 0)
        self.temp.mkdir(mode=0o700)
        self.write(self.temp/'playbook.json', json.dumps([dict(hosts='localhost', gather_facts=False,
            become=False, tasks=tasks, vars=variables)]).encode())
        await self.checkpoint('ansible', 1)
        env = dict(HOME=str(self.body), PATH='/usr/bin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8',
            ANSIBLE_CONFIG=str(self.manifest.tools['ansible_config']), ANSIBLE_NOCOLOR='1',
            ANSIBLE_LOCAL_TEMP=str(self.temp/'local'), ANSIBLE_REMOTE_TEMP=str(self.temp/'remote'),
            ANSIBLE_COLLECTIONS_PATH=str(self.manifest.directories['ansible_plugins']),
            ANSIBLE_COLLECTIONS_SCAN_SYS_PATH='False', ANSIBLE_ROLES_PATH=str(self.manifest.directories['empty']),
            ANSIBLE_LIBRARY=str(self.manifest.directories['ansible_plugins']),
            ANSIBLE_ACTION_PLUGINS=str(self.manifest.directories['ansible_plugins']),
            ANSIBLE_CONNECTION_PLUGINS=str(self.manifest.directories['ansible_plugins']),
            ANSIBLE_BECOME='False', PYTHONDONTWRITEBYTECODE='1')
        program = 'import sys,runpy;sys.path.insert(0,sys.argv.pop(1));runpy.run_path(sys.argv.pop(1),run_name="__main__")'
        await self.command((self.manifest.tools['python'], '-I', '-S', '-B', '-c', program,
            self.manifest.directories['python_root'], self.manifest.tools['ansible'], '-i', 'localhost,',
            '-c', 'local', '--', self.temp/'playbook.json'), env=env, cwd=self.temp)
        await self.checkpoint('bootstrap-remove', 2)
        import shutil
        shutil.rmtree(self.temp)

    async def identity(self, value):
        import base64
        import json
        from mop.common.soul import from_manifest
        from mop.node.host_phase_source import identity_commands
        from mop.node.http_execution import Refused
        role = from_manifest(value['role']['manifest'], base64.b64decode(value['role']['prompt'], validate=True))
        if role.manifest.runtime.driver != 'any':
            raise Refused('isolating role is unavailable on shared-reservation HOST')
        for sequence, (effect, argv) in enumerate(zip(('git-name', 'git-email', 'git-use-config-only'),
                identity_commands(self.manifest.tools['git'], value['git_name'], value['git_email']))):
            await self.checkpoint(effect, sequence)
            await self.command((*self.git(), *argv[1:]))
        expected = value['git_name']+' <'+value['git_email']+'> '
        for kind in ('GIT_AUTHOR_IDENT', 'GIT_COMMITTER_IDENT'):
            ident = (await self.command((*self.git(), 'var', kind))).decode().strip()
            if not ident.startswith(expected):
                raise Refused('actual Git author/committer differs from approved owner identity')
        await self.checkpoint('role', 3)
        directory = self.body/'role'
        directory.mkdir(mode=0o700)
        self.write(directory/'manifest.json', json.dumps(role.manifest.to_dict(), sort_keys=True).encode())
        self.write(directory/'prompt.md', role.prompt)
        if ((directory/'prompt.md').read_bytes() != role.prompt
                or json.loads((directory/'manifest.json').read_bytes()) != role.manifest.to_dict()):
            raise Refused('actual role materialization differs from approved bundle')

    async def run(self):
        from mop.node.host_scope import decode, encode
        from mop.node.host_scope_supervisor import read_frame
        from mop.node.host_phases import Submission, Projection
        from mop.node.host_input_files import BodyReader
        from mop.common.initialcreate import CONFIRMATIONS
        from mop.node.http_execution import Refused
        while True:
            raw = await read_frame(self.reader)
            submission = Submission.decode(raw)
            if submission.descriptor.scope != self.scope:
                raise Refused('body phase scope differs from installed derived control channel')
            self.active = submission
            self.manifest.project(submission.projection)
            with BodyReader(self.config.snapshots, body_uid=self.config.installed.body_uid,
                    body_gid=self.config.installed.body_gid, backend_epoch=self.scope.epoch,
                    node_id=self.scope.node_id, executor_epoch=self.scope.executor_epoch) as reader:
                value = reader.read(submission.descriptor, submission.admission).value
            if Projection.from_delivery(value) != submission.projection:
                raise Refused('body snapshot projection changed')
            try:
                await {'prepare_body': self.prepare, 'bootstrap_body': self.bootstrap, 'initial_identity': self.identity}[submission.admission.work.action](value)
            except Exception as error:
                # Только library Refused уже безопасен; YAML/tool raw exception — нет.
                reason = str(error) if isinstance(error, Refused) else type(error).__name__
                self.writer.write(encode(dict(schema_version=1, action='body_failed',
                    attempt_id=submission.admission.attempt_id, error=reason))+b'\n')
                await self.writer.drain()
                raise
            self.writer.write(encode(dict(schema_version=1, action='body_complete',
                attempt_id=submission.admission.attempt_id, evidence=CONFIRMATIONS[submission.admission.work.action]))+b'\n')
            await self.writer.drain()
            self.active = None


async def root_connection(config, scope):
    import asyncio
    from mop.node.host_scope_supervisor import peer_credentials
    from mop.node.host_scope import encode
    from mop.node.host_backend import Process
    filename = config.control(scope)
    fd = os.open(filename.parent, os.O_RDONLY|os.O_DIRECTORY|os.O_NOFOLLOW|os.O_CLOEXEC)
    try:
        info = os.stat(filename.name, dir_fd=fd, follow_symlinks=False)
        if not stat.S_ISSOCK(info.st_mode) or info.st_uid != 0 or info.st_gid != config.installed.body_gid or stat.S_IMODE(info.st_mode) != 0o660:
            raise RuntimeError('root-controlled derived body socket required')
        reader, writer = await asyncio.open_unix_connection(str(filename), limit=4097)
        peer = peer_credentials(writer.get_extra_info('socket'))
        after = os.stat(filename.name, dir_fd=fd, follow_symlinks=False)
        if peer[1] != 0 or (info.st_dev, info.st_ino) != (after.st_dev, after.st_ino):
            raise RuntimeError('body control root peer/socket changed')
        process = Process.read(peer[0])
    finally: os.close(fd)
    return reader, writer, peer, process


async def lifecycle(config, manifest, scope):
    from mop.node.host_scope import encode
    reader, writer, peer, process = await root_connection(config, scope)
    writer.write(encode(dict(schema_version=1, action='body_hello', scope=scope.wire()))+b'\n')
    await writer.drain()
    try: await Broker(config, manifest, scope, reader, writer, peer, process).run()
    finally:
        writer.close()
        await writer.wait_closed()


async def cleanup_lifecycle(config, scope):
    import shutil
    from mop.node.host_backend import Process
    from mop.node.host_scope import decode, encode
    from mop.node.host_scope_supervisor import peer_credentials, read_frame
    from mop.node.host_phases import require_frame
    reader, writer, peer, process = await root_connection(config, scope)
    try:
        writer.write(encode(dict(schema_version=1, action='cleanup_hello', scope=scope.wire()))+b'\n')
        await writer.drain()
        require_frame(decode(await read_frame(reader)), dict(schema_version=1, action='cleanup_approved', scope=scope.wire()))
        if peer_credentials(writer.get_extra_info('socket')) != peer or Process.read(peer[0]) != process:
            raise RuntimeError('cleanup lost its exact root process')
        body = config.installed.body(scope)
        # Только body UID проходит project tree; symlinks не follow.
        with os.scandir(body) as entries:
            for entry in entries:
                if entry.is_dir(follow_symlinks=False): shutil.rmtree(body/entry.name)
                else: os.unlink(body/entry.name)
        writer.write(encode(dict(schema_version=1, action='cleanup_done', scope=scope.wire()))+b'\n')
        await writer.drain()
        # Остаётся MainPID до exact root cgroup.kill; RuntimeMaxSec — backstop.
        await reader.read(1)
    finally:
        writer.close()
        await writer.wait_closed()


def main(release):
    helper_mode, helper_scope = os.environ.get('MOP_PHASE_HELPER'), os.environ.get('MOP_PHASE_SCOPE')
    cleanup = helper_mode is None and len(sys.argv) == 3 and sys.argv[1] == '--cleanup'
    config, manifest = load_installation(release, cleanup=cleanup)
    os.environ.clear()
    if helper_mode is not None:
        return helper(config, manifest, helper_mode, helper_scope)
    if len(sys.argv) != 3 or sys.argv[1] not in ('--scope', '--cleanup'):
        raise RuntimeError('finite installed body mode required')
    from mop.node.host_scope import Scope
    scope = Scope.from_unit(sys.argv[2])
    body = config.installed.body(scope)
    if Path.cwd() != body:
        raise RuntimeError('derived private body working directory required')
    import asyncio
    if sys.argv[1] == '--cleanup':
        asyncio.run(cleanup_lifecycle(config, scope))
    else:
        asyncio.run(lifecycle(config, manifest, scope))


if __name__ == '__main__':
    try: main(installed_entry())
    except Exception:
        # stdout/stderr runner всегда null; secret/tool exception не печатается.
        raise SystemExit(1)
