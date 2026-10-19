"""Конечный post-activation management handoff; stdlib, без origin/path authority.

Request содержит только runtime locators. Ответ — snapshot существующего
Actor credential, не новый lease/Token и не транспортная конфигурация тела.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from uuid import UUID
from urllib.parse import urlsplit
import base64
import ssl

from .telemetry import SessionScope, counter, fields, positive, text, uuid


def version(value):
    if type(value) is not int or value != 1:
        raise ValueError('unsupported management context version')


@dataclass(frozen=True)
class ManagementRequest:
    scope: SessionScope
    generation: int

    def __post_init__(self):
        if type(self.scope) is not SessionScope:
            raise ValueError('typed current runtime scope required')
        positive(self.generation)

    def wire(self):
        return dict(schema_version=1, scope=self.scope.wire(), generation=str(self.generation))

    @classmethod
    def parse(cls, value):
        fields(value, ('schema_version', 'scope', 'generation'))
        version(value['schema_version'])
        return cls(SessionScope.parse(value['scope']), counter(value['generation']))


@dataclass(frozen=True)
class ManagementContext:
    request: ManagementRequest
    installation_id: str
    restore_generation: UUID
    project_id: UUID
    grant_id: UUID
    subject_id: int
    credential_id: UUID
    assignment_revision: int
    expires_at: datetime
    token: str = field(repr=False)

    def __post_init__(self):
        if (type(self.request) is not ManagementRequest or not text(self.installation_id, 64)
                or any(type(value) is not UUID for value in
                    (self.restore_generation, self.project_id, self.grant_id, self.credential_id))
                or type(self.token) is not str or re.fullmatch('[0-9a-f]{40}', self.token) is None
                or type(self.expires_at) is not datetime or self.expires_at.tzinfo is not timezone.utc):
            raise ValueError('invalid bound management context')
        positive(self.subject_id)
        positive(self.assignment_revision)

    def wire(self):
        return dict(schema_version=1, request=self.request.wire(), installation_id=self.installation_id,
            restore_generation=str(self.restore_generation), project_id=str(self.project_id),
            grant_id=str(self.grant_id), subject_id=self.subject_id, credential_id=str(self.credential_id),
            assignment_revision=str(self.assignment_revision), expires_at=self.expires_at.isoformat(), token=self.token)

    @classmethod
    def parse(cls, value, expected):
        fields(value, ('schema_version', *cls.__dataclass_fields__))
        version(value['schema_version'])
        request = ManagementRequest.parse(value['request'])
        wanted = expected if type(expected) is ManagementRequest else ManagementRequest.parse(expected)
        if request != wanted:
            raise ValueError('management context runtime binding changed')
        try:
            expiry = datetime.fromisoformat(value['expires_at'])
        except (TypeError, ValueError):
            raise ValueError('invalid management context expiry') from None
        if (expiry.tzinfo is not timezone.utc or expiry.isoformat() != value['expires_at']
                or expiry <= datetime.now(timezone.utc)):
            raise ValueError('expired or invalid management context')
        return cls(request, value['installation_id'], uuid(value['restore_generation']), uuid(value['project_id']),
            uuid(value['grant_id']), value['subject_id'], uuid(value['credential_id']),
            counter(value['assignment_revision']), expiry, value['token'])


MAX_CA_BYTES = 32768
MAX_BODY_WAIT = 300


def body_deadline(value):
    """Launcher передаёт UTC deadline явно, не через ambient environment."""
    try:
        result = datetime.fromisoformat(value)
        left = (result - datetime.now(timezone.utc)).total_seconds()
        if result.tzinfo is timezone.utc and result.isoformat() == value and 0 < left <= MAX_BODY_WAIT:
            return result
    except (TypeError, ValueError):
        pass
    raise ValueError('body deadline must be future UTC within 300 seconds')


@dataclass(frozen=True)
class BodyBootstrap:
    """Локальный transport DTO nodeRoot после producer ACK, не источник прав.

    Root обязан взять origin/CA из verified installed coordinates. Здесь нет
    ca_file, node bearer, permission snapshot или server-side issuer API.
    Canonical Actor.node_id не связывает физический runtime node.
    """
    request: ManagementRequest
    installation_id: str
    restore_generation: UUID
    project_id: UUID
    grant_id: UUID
    expires_at: datetime
    origin: str = field(repr=False)
    ca_pem: str = field(repr=False)
    body_uid: int

    def __post_init__(self):
        if (type(self.request) is not ManagementRequest or not text(self.installation_id, 64)
                or any(type(value) is not UUID for value in
                    (self.restore_generation, self.project_id, self.grant_id))
                or type(self.expires_at) is not datetime or self.expires_at.tzinfo is not timezone.utc
                or self.expires_at <= datetime.now(timezone.utc)
                or type(self.body_uid) is not int or not 0 <= self.body_uid < 2**31):
            raise ValueError('invalid body bootstrap binding')
        try:
            url = urlsplit(self.origin)
            valid = (type(self.origin) is str and len(self.origin) <= 2048
                and all(32 < ord(c) < 127 for c in self.origin) and '\\' not in self.origin
                and url.scheme == 'https' and bool(url.hostname) and url.username is None
                and url.password is None and not url.path and not url.query and not url.fragment
                and '?' not in self.origin and '#' not in self.origin and not url.netloc.endswith(':')
                and (url.port is None or 0 < url.port <= 65535))
        except (TypeError, ValueError, AttributeError):
            valid = False
        if not valid:
            raise ValueError('invalid body HTTPS origin')
        try:
            if type(self.ca_pem) is not str or len(self.ca_pem.encode('ascii')) > MAX_CA_BYTES:
                raise ValueError()
            blocks = re.findall(r'-----BEGIN CERTIFICATE-----\n([A-Za-z0-9+/=\n]+)-----END CERTIFICATE-----\n?',
                self.ca_pem)
            if not blocks or ''.join('-----BEGIN CERTIFICATE-----\n'+b+'-----END CERTIFICATE-----\n'
                    for b in blocks).rstrip('\n') != self.ca_pem.rstrip('\n'):
                raise ValueError()
            for block in blocks:
                base64.b64decode(block.replace('\n', ''), validate=True)
            tls = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
            tls.load_verify_locations(cadata=self.ca_pem)
        except (ValueError, UnicodeError, ssl.SSLError):
            raise ValueError('invalid bounded body CA certificates') from None

    def wire(self):
        return dict(schema_version=1, request=self.request.wire(), installation_id=self.installation_id,
            restore_generation=str(self.restore_generation), project_id=str(self.project_id),
            grant_id=str(self.grant_id), expires_at=self.expires_at.isoformat(), origin=self.origin,
            ca_pem=self.ca_pem, body_uid=self.body_uid)

    @classmethod
    def parse(cls, value):
        fields(value, ('schema_version', *cls.__dataclass_fields__))
        version(value['schema_version'])
        try:
            expiry = datetime.fromisoformat(value['expires_at'])
            if expiry.isoformat() != value['expires_at']:
                raise ValueError()
        except (TypeError, ValueError):
            raise ValueError('invalid body bootstrap expiry') from None
        return cls(ManagementRequest.parse(value['request']), value['installation_id'],
            uuid(value['restore_generation']), uuid(value['project_id']), uuid(value['grant_id']),
            expiry, value['origin'], value['ca_pem'], value['body_uid'])
