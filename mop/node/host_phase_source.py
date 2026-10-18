"""Узкие source formats и compiled Git operands; ни shell, ни ambient config."""
import base64
from dataclasses import dataclass, field
from pathlib import Path
import re
import struct
from urllib.parse import urlsplit

from mop.node.host_scope import exact
from mop.node.http_execution import Refused


def source_origin(origin):
    if type(origin) is not str or not 1 <= len(origin) <= 2048 or not origin.isascii():
        raise Refused('bounded canonical Git HTTPS/SSH origin required')
    try:
        u = urlsplit(origin)
        port = u.port
        host = u.hostname
        if (u.scheme not in ('https', 'ssh') or not host or u.password is not None
                or u.query or u.fragment or '%' in origin or '\\' in origin
                or not re.fullmatch('[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?', host)
                or any(not p or p in ('.', '..') or p.startswith('-') for p in u.path.split('/')[1:])
                or not re.fullmatch('/[a-zA-Z0-9_./-]+', u.path)
                or port is not None and not 1 <= port <= 65535
                or u.scheme == 'https' and u.username is not None
                or u.scheme == 'ssh' and (u.username is None or not re.fullmatch('[a-z_][a-z0-9_-]{0,31}', u.username))):
            raise ValueError()
        authority = (u.username+'@' if u.username else '')+host+(':'+str(port) if port else '')
        if origin != u.scheme+'://'+authority+u.path:
            raise ValueError()
        return u.scheme, host, port or (443 if u.scheme == 'https' else 22), u.username, u.path
    except ValueError:
        raise Refused('canonical credential-free Git HTTPS/SSH origin required') from None


def _text(value, maximum):
    if (type(value) is not str or not 0 < len(value.encode('utf-8', errors='replace')) <= maximum
            or any(ord(c) < 32 or ord(c) == 127 or 0xd800 <= ord(c) <= 0xdfff for c in value)):
        raise Refused('bounded source credential text required')
    return value


def _b64(value):
    try:
        raw = base64.b64decode(value, validate=True)
        if base64.b64encode(raw).decode() != value: raise ValueError()
        return raw
    except (ValueError, TypeError):
        raise Refused('canonical source key encoding required') from None


class _Key:
    def __init__(self, raw): self.raw = raw
    def take(self, count):
        if not 0 <= count <= len(self.raw): raise Refused('truncated source key format')
        result, self.raw = self.raw[:count], self.raw[count:]
        return result
    def integer(self): return struct.unpack('>I', self.take(4))[0]
    def string(self): return self.take(self.integer())


def _public(raw):
    k = _Key(raw)
    if k.string() != b'ssh-ed25519' or len(k.string()) != 32 or k.raw:
        raise Refused('only exact Ed25519 source keys are supported')


def _private(value):
    if type(value) is not str or not 1 <= len(value) <= 8192:
        raise Refused('bounded unencrypted OpenSSH Ed25519 source key required')
    lines = value.splitlines()
    if (len(lines) < 3 or lines[0] != '-----BEGIN OPENSSH PRIVATE KEY-----'
            or lines[-1] != '-----END OPENSSH PRIVATE KEY-----'):
        raise Refused('unencrypted OpenSSH Ed25519 source key required')
    k = _Key(_b64(''.join(lines[1:-1])))
    if (k.take(15) != b'openssh-key-v1\0' or k.string() != b'none' or k.string() != b'none'
            or k.string() != b'' or k.integer() != 1):
        raise Refused('encrypted or unknown OpenSSH source keys are unavailable')
    public = k.string()
    _public(public)
    secret = _Key(k.string())
    if k.raw or secret.take(4) != secret.take(4) or secret.string() != b'ssh-ed25519':
        raise Refused('invalid OpenSSH Ed25519 source key')
    pub, private = secret.string(), secret.string()
    secret.string()  # Не authority; комментарий ключа не становится argv/path.
    if (len(pub) != 32 or len(private) != 64 or private[32:] != pub
            or public != struct.pack('>I', 11)+b'ssh-ed25519'+struct.pack('>I', 32)+pub
            or not 1 <= len(secret.raw) <= 8 or secret.raw != bytes(range(1, len(secret.raw)+1))):
        raise Refused('invalid OpenSSH Ed25519 source key payload')


@dataclass(frozen=True)
class SourceCredential:
    format: str
    origin: str
    host: str
    port: int
    username: str
    password: str = field(default='', repr=False)
    private_key: str = field(default='', repr=False)
    known_hosts: str = field(default='', repr=False)

    @classmethod
    def parse(cls, value, origin):
        scheme, host, port, user, _ = source_origin(origin)
        if type(value) is not dict or type(value.get('format')) is not str:
            raise Refused('fixed source credential format required')
        kind = value['format']
        if kind in ('git-https-basic-v1', 'git-https-token-v1'):
            exact(value, ('format', 'origin', 'username', 'password'))
            if scheme != 'https' or value['origin'] != origin:
                raise Refused('source HTTPS credential origin mismatch')
            return cls(kind, origin, host, port, _text(value['username'], 256), _text(value['password'], 8192))
        if kind == 'git-ssh-key-v1':
            exact(value, ('format', 'origin', 'private_key', 'known_hosts'))
            if scheme != 'ssh' or value['origin'] != origin:
                raise Refused('source SSH credential origin mismatch')
            _private(value['private_key'])
            known = value['known_hosts']
            expected = host if port == 22 else '['+host+']:'+str(port)
            if type(known) is not str or not 1 <= len(known) <= 8192 or len(known.splitlines()) != 1:
                raise Refused('one exact approved SSH host key required')
            fields = known.split()
            if len(fields) != 3 or fields[:2] != [expected, 'ssh-ed25519']:
                raise Refused('exact approved SSH host/port Ed25519 key required')
            _public(_b64(fields[2]))
            return cls(kind, origin, host, port, user, private_key=value['private_key'], known_hosts=known)
        raise Refused('source credential format is unavailable')


def git_environment(body, empty, ca):
    return dict(HOME=str(body), PATH='/usr/bin:/bin', LANG='C.UTF-8', LC_ALL='C.UTF-8',
        GIT_CONFIG_NOSYSTEM='1', GIT_CONFIG_SYSTEM='/dev/null', GIT_CONFIG_GLOBAL='/dev/null',
        GIT_ATTR_NOSYSTEM='1', GIT_TERMINAL_PROMPT='0', GIT_TEMPLATE_DIR=str(empty),
        GIT_SSL_CAINFO=str(ca), GIT_ALLOW_PROTOCOL='https:ssh', GIT_PROTOCOL_FROM_USER='0')


def git_prefix(git, empty):
    return (str(git), '-c', 'core.hooksPath='+str(empty), '-c', 'credential.helper=',
        '-c', 'http.proxy=', '-c', 'http.followRedirects=false', '-c', 'http.sslVerify=true',
        '-c', 'core.fsmonitor=false', '-c', 'submodule.recurse=false', '-c', 'protocol.file.allow=never',
        '-c', 'protocol.ext.allow=never', '-c', 'init.defaultBranch=mop-detached')


def ssh_command(source, args, ssh, body):
    """Принимает только Git upload-pack exact origin; все SSH options compiled."""
    scheme, host, port, user, remote = source_origin(source.origin)
    expected = "git-upload-pack '"+remote+"'"
    target = user+'@'+host if user is not None else ''
    transport = ('-p', str(port)) if urlsplit(source.origin).port is not None else ()
    operands = (*transport, target, expected)
    if (source.format != 'git-ssh-key-v1' or scheme != 'ssh' or type(args) is not tuple
            or args not in (operands, ('-o', 'SendEnv=GIT_PROTOCOL', *operands))):
        raise Refused('SSH helper differs from exact selected Git origin operands')
    return (str(ssh), '-F', '/dev/null', '-T', '-o', 'BatchMode=yes',
        '-o', 'IdentitiesOnly=yes', '-o', 'IdentityAgent=none', '-o', 'StrictHostKeyChecking=yes',
        '-o', 'UserKnownHostsFile='+str(body/'.phase/known_hosts'), '-o', 'GlobalKnownHostsFile=/dev/null',
        '-o', 'ProxyCommand=none', '-o', 'ProxyJump=none', '-o', 'ForwardAgent=no',
        '-o', 'ClearAllForwardings=yes', '-o', 'PermitLocalCommand=no', '-o', 'CanonicalizeHostname=no',
        '-o', 'ConnectTimeout=5', '-i', str(body/'.phase/id_ed25519'), '-p', str(port), '--', target, expected)


def identity_commands(git, name, email):
    return tuple((str(git), 'config', '--local', '--replace-all', '--', key, value)
        for key, value in (('user.name', name), ('user.email', email), ('user.useConfigOnly', 'true')))
