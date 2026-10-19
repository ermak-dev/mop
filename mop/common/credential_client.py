"""Rootless sync adapter для web/worker: один приватный use case на соединение.

Не сохраняет credentials, не повторяет мутации после неизвестного исхода.
Вызывать вне SQL; async потребитель использует свой bounded blocking runtime.
"""
from dataclasses import fields
from datetime import datetime, timezone
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
        try:
            p.parse_request(request)
        except p.CredentialError as error:
            if command in (p.Command.SEND_REPORT, p.Command.TAKE_DELIVERY):
                raise p.CredentialInput(str(error)) from None
            raise
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
            if result['error']['code'] == 'refused':
                raise p.CredentialRefused(message)
            if (command in (p.Command.SEND_REPORT, p.Command.TAKE_DELIVERY)
                    and result['error']['code'] in ('conflict', 'capacity', 'not_found', 'invalid')):
                raise p.PrivateDeliveryError(result['error']['code'], message)
            raise p.CredentialError(message)
        p.exact(result, ('ok', 'result'))
        return result['result']

    @staticmethod
    def _result(kind, value):
        p.exact(value, (f.name for f in fields(kind)))
        value = dict(value)
        for name in ('id', 'project_id', 'active_version_id', 'secret_id', 'binding_id', 'version_id',
                     'target_id', 'incarnation', 'credential_version_id', 'revision_id', 'content_version_id'):
            if name in value and value[name] is not None:
                value[name] = p.identifier(value[name])
        for name in ('value', 'manifest', 'prompt', 'tasks', 'variables'):
            if name in value:
                value[name] = p.binary(value[name])
        if 'secret_refs' in value:
            value['secret_refs'] = p.identifiers(value['secret_refs'])
        return kind(**value)

    def send_private_report(self, auth: p.Credential, *, expected_epoch: UUID, address: str,
                            message_id: UUID, text: str, not_after: datetime,
                            operation_request_id: UUID | None = None, notify_when_idle: bool = False,
                            priority: str = 'next', sender_endpoint_id: UUID | None = None,
                            sender_connection_nonce: UUID | None = None):
        """Конечный send: daemon сам резолвит actor и optional conversation holder."""
        from .endpoint import DeliveryResult
        if type(not_after) is not datetime or not_after.utcoffset() is None:
            raise p.CredentialInput('aware delivery deadline required')
        result = self._invoke(auth, p.Command.SEND_REPORT, dict(expected_epoch=str(expected_epoch),
            address=address, message_id=str(message_id), text=text, not_after=not_after.isoformat(),
            operation_request_id=str(operation_request_id) if operation_request_id is not None else None,
            notify_when_idle=notify_when_idle, priority=priority,
            sender_endpoint_id=str(sender_endpoint_id) if sender_endpoint_id is not None else None,
            sender_connection_nonce=str(sender_connection_nonce) if sender_connection_nonce is not None else None))
        p.exact(result, (f.name for f in fields(DeliveryResult)))
        if (type(result['state']) is not str or result['state'] not in
                ('queued', 'attempting', 'accepted', 'unknown', 'unavailable') or type(result['idle']) is not bool):
            raise p.CredentialError('invalid private delivery receipt')
        return DeliveryResult(p.identifier(result['id']), p.identifier(result['message_id']),
            p.identifier(result['operation_request_id']) if result['operation_request_id'] is not None else None,
            result['state'], result['idle'])

    def take_private_delivery(self, auth: p.Credential, *, endpoint_id: UUID, connection_nonce: UUID):
        """Один take current receiver: неизвестный ответ не разрешает replay."""
        from .endpoint import DeliveryFrame
        result = self._invoke(auth, p.Command.TAKE_DELIVERY,
            dict(endpoint_id=str(endpoint_id), connection_nonce=str(connection_nonce)))
        if result is None:
            return None
        p.exact(result, (f.name for f in fields(DeliveryFrame)))
        p.delivery_options(result)
        if result['reply_address'] is not None:
            p.private_address(result['reply_address'])
        return DeliveryFrame(p.identifier(result['id']), p.identifier(result['message_id']),
            p._text(result['external_session_id'], 256), p._text(result['text'], 65536),
            result['priority'], result['notify_when_idle'], result['reply_address'])

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

    def issue_addressed(self, auth: p.Credential, *, target_id: UUID) -> tuple[p.DeliveredSecret, ...]:
        result = self._invoke(auth, p.Command.ADDRESSED, {'target_id': str(target_id)})
        if type(result) is not list or len(result) > 128:
            raise p.CredentialError('invalid credential delivery response')
        return tuple(self._result(p.DeliveredSecret, row) for row in result)

    def issue_rotation(self, auth: p.Credential, rotation_id: UUID, *, claim_id: UUID, generation: int) -> tuple[p.DeliveredSecret, ...]:
        result = self._invoke(auth, p.Command.ROTATION, {'rotation_id': str(rotation_id),
            'claim_id': str(claim_id), 'generation': generation})
        if type(result) is not list or len(result) > 128:
            raise p.CredentialError('invalid credential delivery response')
        return tuple(self._result(p.DeliveredSecret, row) for row in result)

    def publish_definition(self, auth: p.Credential, *, project_id: UUID,
                           manifest: bytes, prompt: bytes, provenance: str) -> p.DefinitionInfo:
        result = self._invoke(auth, p.Command.PUBLISH_DEFINITION, {'project_id': str(project_id),
            'manifest': p.encoded(manifest), 'prompt': p.encoded(prompt), 'provenance': provenance})
        return self._result(p.DefinitionInfo, result)

    def approve_definition(self, auth: p.Credential, definition_id: UUID) -> UUID:
        return p.identifier(self._invoke(auth, p.Command.APPROVE_DEFINITION, {'definition_id': str(definition_id)}))

    def reserve_grant(self, auth: p.Credential, *, project_id: UUID, definition_id: UUID,
                      target_id: UUID, incarnation: UUID, request_id: UUID,
                      expires_at: datetime) -> p.GrantReceipt:
        if type(expires_at) is not datetime or expires_at.utcoffset() is None:
            raise p.CredentialError('aware grant expiry required')
        result = self._invoke(auth, p.Command.RESERVE_GRANT, {'project_id': str(project_id),
            'definition_id': str(definition_id), 'target_id': str(target_id), 'incarnation': str(incarnation),
            'request_id': str(request_id), 'expires_at': expires_at.astimezone(timezone.utc).isoformat()})
        return self._result(p.GrantReceipt, result)

    def reserve_initial_grant(self, auth: p.Credential, reservation_id: UUID) -> p.GrantReceipt:
        """Только locator committed initial intent; proof выбирается из SQL, не wire."""
        result = self._invoke(auth, p.Command.RESERVE_INITIAL_GRANT, {'reservation_id': str(reservation_id)})
        return self._result(p.GrantReceipt, result)

    def provision_grant(self, auth: p.Credential, grant_id: UUID, *, expected_revision: int) -> p.GrantReceipt:
        result = self._invoke(auth, p.Command.PROVISION_GRANT,
            {'grant_id': str(grant_id), 'expected_revision': expected_revision})
        return self._result(p.GrantReceipt, result)

    def prepare_replacement(self, auth: p.Credential, operation_id: UUID) -> p.GrantReceipt:
        """Один claimed prepare по locator, без retry и сохранения human credential."""
        result = self._invoke(auth, p.Command.PREPARE_REPLACEMENT, {'operation_id': str(operation_id)})
        return self._result(p.GrantReceipt, result)

    def activate_grant(self, auth: p.Credential, grant_id: UUID, *, expected_revision: int) -> p.GrantReceipt:
        """Только claimed create: authority service enrollment проверяет сервер."""
        result = self._invoke(auth, p.Command.ACTIVATE_GRANT,
            {'grant_id': str(grant_id), 'expected_revision': expected_revision})
        return self._result(p.GrantReceipt, result)

    def publish_preparation(self, auth: p.Credential, *, project_id: UUID, tasks: bytes,
                            variables: bytes, provenance: str, commit: str,
                            secret_refs: tuple[UUID, ...] = ()) -> p.PreparationInfo:
        result = self._invoke(auth, p.Command.PUBLISH_PREPARATION, {'project_id': str(project_id),
            'tasks': p.encoded(tasks), 'variables': p.encoded(variables), 'provenance': provenance,
            'commit': commit, 'secret_refs': [str(ref) for ref in secret_refs]})
        return self._result(p.PreparationInfo, result)

    def read_preparation(self, auth: p.Credential, revision_id: UUID) -> p.PreparationBytes:
        result = self._invoke(auth, p.Command.READ_PREPARATION, {'revision_id': str(revision_id)})
        return self._result(p.PreparationBytes, result)

    def approve_preparation(self, auth: p.Credential, revision_id: UUID) -> p.ApprovalReceipt:
        result = self._invoke(auth, p.Command.APPROVE_PREPARATION, {'revision_id': str(revision_id)})
        return self._result(p.ApprovalReceipt, result)

    def read_launch(self, auth: p.Credential, work: dict) -> dict:
        """Только текущая machine Work; выбор definition -- внутри службы."""
        result = self._invoke(auth, p.Command.LAUNCH, {'work': work})
        if type(result) is not dict:
            raise p.CredentialError('invalid addressed launch response')
        return result

    def read_initial_launch(self, auth: p.Credential, work: dict) -> dict:
        """Конечные purpose-specific bytes; старый read_launch не расширяется."""
        from .initialcreate import parse_delivery
        return parse_delivery(self._invoke(auth, p.Command.INITIAL_LAUNCH, {'work': work}), work)

    def read_management_context(self, auth: p.Credential, request):
        """Только current runtime locators; original machine token остаётся authority."""
        from .management_context import ManagementContext, ManagementRequest
        wanted = ManagementRequest.parse(request)
        result = self._invoke(auth, p.Command.MANAGEMENT_CONTEXT, {'request': wanted.wire()})
        try:
            return ManagementContext.parse(result, wanted)
        except ValueError:
            raise p.CredentialError('invalid addressed management context response') from None

    def read_definition(self, auth: p.Credential, definition_id: UUID) -> p.DefinitionBytes:
        result = self._invoke(auth, p.Command.DEFINITION, {'definition_id': str(definition_id)})
        return self._result(p.DefinitionBytes, result)


class GrantActivator:
    """Worker port: хранит только явный service token, не credential создателя.

    Транспорт не повторяет мутацию; потерянный ответ сверяется чтением гранта.
    Enrollment/RBAC и claimed create проверяются credential-службой на каждый вызов.
    """
    def __init__(self, client: CredentialClient, credential: p.Credential):
        if type(credential) is not p.Credential or credential.kind != 'token':
            raise p.CredentialError('explicit service token required for activation worker')
        self.client, self._credential = client, credential

    def __repr__(self):
        return 'GrantActivator(<service credential>)'

    def activate_grant(self, grant_id: UUID, *, expected_revision: int) -> p.GrantReceipt:
        return self.client.activate_grant(self._credential, grant_id, expected_revision=expected_revision)
