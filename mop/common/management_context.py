"""Конечный post-activation management handoff; stdlib, без origin/path authority.

Request содержит только runtime locators. Ответ — snapshot существующего
Actor credential, не новый lease/Token и не транспортная конфигурация тела.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import re
from uuid import UUID

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
