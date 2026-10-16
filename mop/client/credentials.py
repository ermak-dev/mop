"""Rootless sync adapter для web/worker: один приватный use case на соединение.

Не сохраняет credentials, не повторяет мутации после неизвестного исхода.
Вызывать вне SQL; async потребитель использует свой bounded blocking runtime.
"""
from dataclasses import fields
import os
import socket
import stat
import time
from uuid import UUID

from mop.common import credentials as p


class CredentialClient:
    def __init__(self, path: str, *, server_uid: int, timeout: float = 5):
        self.path = p.socket_path(path)
        if type(server_uid) is not int or server_uid < 0 or not 0 < timeout <= 10:
            raise p.CredentialError('explicit service UID and bounded socket timeout required')
        self.server_uid, self.timeout = server_uid, timeout

    def __repr__(self):
        return 'CredentialClient(<protected socket>)'

    def _invoke(self, auth, command, args):
        if type(auth) is not p.Credential:
            raise p.CredentialError('current token or session credential required')
        request = {'version': 1, 'auth': {'kind': auth.kind, 'value': auth.value},
                   'command': command.value, 'args': args}
        p.parse_request(request)
        wire = p.frame(request)
        fd = p.directory_fd(self.path, self.server_uid)
        deadline = time.monotonic() + self.timeout
        try:
            info = os.stat(self.path.name, dir_fd=fd, follow_symlinks=False)
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != self.server_uid or info.st_mode & 0o007:
                raise p.CredentialError('protected credential socket is invalid')
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                # dirfd закрепляет родителя даже при rename после проверки.
                sock.connect(f'/proc/self/fd/{fd}/{self.path.name}')
                if p.peer_uid(sock) != self.server_uid:
                    raise p.CredentialError('credential service UID does not match')
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError()
                sock.settimeout(remaining)
                sock.sendall(wire)
                def receive(size):
                    chunks = bytearray()
                    while len(chunks) < size:
                        remaining = deadline - time.monotonic()
                        if remaining <= 0:
                            raise TimeoutError()
                        sock.settimeout(remaining)
                        part = sock.recv(size - len(chunks))
                        if not part:
                            raise OSError()
                        chunks.extend(part)
                    return bytes(chunks)
                result = p.loads(receive(p.frame_size(receive(4))))
        except (OSError, TimeoutError):
            raise p.CredentialError('credential service transport unavailable; mutation outcome unknown, do not replay') from None
        finally:
            os.close(fd)
        if type(result) is not dict or type(result.get('ok')) is not bool:
            raise p.CredentialError('invalid credential service response')
        if not result['ok']:
            p.exact(result, ('ok', 'error'))
            p.exact(result['error'], ('code', 'message'))
            message = result['error']['message']
            if type(message) is not str or len(message) > 512:
                raise p.CredentialError('invalid credential service response')
            raise p.CredentialError(message)
        p.exact(result, ('ok', 'result'))
        return result['result']

    @staticmethod
    def _result(kind, value):
        p.exact(value, (f.name for f in fields(kind)))
        value = dict(value)
        for name in ('id', 'project_id', 'active_version_id', 'secret_id', 'binding_id', 'version_id'):
            if name in value and value[name] is not None:
                value[name] = p.identifier(value[name])
        for name in ('value', 'manifest', 'prompt'):
            if name in value:
                value[name] = p.binary(value[name])
        return kind(**value)

    def create(self, auth: p.Credential, *, project_id: UUID | None, name: str,
               kind: str, purpose: str, value: bytes) -> p.SecretInfo:
        result = self._invoke(auth, p.Command.CREATE, {'project_id': str(project_id) if project_id else None,
            'name': name, 'kind': kind, 'purpose': purpose, 'value': p.encoded(value)})
        return self._result(p.SecretInfo, result)

    def stage(self, auth: p.Credential, secret_id: UUID, value: bytes, *, expected_revision: int) -> p.VersionInfo:
        result = self._invoke(auth, p.Command.STAGE, {'secret_id': str(secret_id),
            'value': p.encoded(value), 'expected_revision': expected_revision})
        return self._result(p.VersionInfo, result)

    def update(self, auth: p.Credential, secret_id: UUID, value: bytes, *, expected_revision: int) -> p.SecretInfo:
        result = self._invoke(auth, p.Command.UPDATE, {'secret_id': str(secret_id),
            'value': p.encoded(value), 'expected_revision': expected_revision})
        return self._result(p.SecretInfo, result)

    def issue_addressed(self, auth: p.Credential) -> tuple[p.DeliveredSecret, ...]:
        result = self._invoke(auth, p.Command.ADDRESSED, {})
        if type(result) is not list or len(result) > 128:
            raise p.CredentialError('invalid credential delivery response')
        return tuple(self._result(p.DeliveredSecret, row) for row in result)

    def issue_rotation(self, auth: p.Credential, rotation_id: UUID, *, claim_id: UUID, generation: int) -> tuple[p.DeliveredSecret, ...]:
        result = self._invoke(auth, p.Command.ROTATION, {'rotation_id': str(rotation_id),
            'claim_id': str(claim_id), 'generation': generation})
        if type(result) is not list or len(result) > 128:
            raise p.CredentialError('invalid credential delivery response')
        return tuple(self._result(p.DeliveredSecret, row) for row in result)

    def read_bundle(self, auth: p.Credential, bundle_id: UUID) -> p.BundleBytes:
        result = self._invoke(auth, p.Command.BUNDLE, {'bundle_id': str(bundle_id)})
        return self._result(p.BundleBytes, result)
