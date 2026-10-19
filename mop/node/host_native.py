"""Закрытые значения initial HOST native; никаких HTTP/process/path RPC.

ProviderProfile задаёт installed composition, а не consumer или machine wire.
Пока такого явно одобренного профиля нет, конфигурация не допускает запуск.
"""
from dataclasses import dataclass, field
from datetime import datetime, timezone
import hashlib
from pathlib import Path
import re
from urllib.parse import urlsplit

from mop.driver.launch import MachineConfig
from mop.node.host_inputs import (Descriptor, _delivery, _json, _sha, admission_wire,
    canonical_json, validate_admission, validate_scope)
from mop.node.host_phases import Projection, parse_admission
from mop.node.host_scope import Reference, canonical, decode, encode, exact
from mop.node.http_execution import Admission, Refused, Work, counter, stamp


def model_id(value):
    # Ни slash-command, ни flags, пробелы, unicode aliases или terminal controls.
    if type(value) is not str or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._:-]{0,127}', value) is None:
        raise Refused('exact bounded native model identifier required')
    return value


@dataclass(frozen=True)
class ProviderProfile:
    identifier: str
    origin: str
    ca: Path
    auth: str

    def __post_init__(self):
        if (type(self.identifier) is not str or re.fullmatch(r'[a-z][a-z0-9-]{0,63}', self.identifier) is None
                or self.auth != 'api-key-v1' or not isinstance(self.ca, Path)
                or not self.ca.is_absolute() or '..' in self.ca.parts):
            raise Refused('explicit installed verified provider profile required')
        try:
            MachineConfig(self.origin, 'unused.invalid', 'unused.invalid', 22, 'host')
            if urlsplit(self.origin).path:
                raise ValueError
        except (TypeError, ValueError):
            raise Refused('fixed verified HTTPS provider origin required') from None

    @property
    def digest(self):
        return hashlib.sha256(canonical_json(dict(identifier=self.identifier,
            origin=self.origin, ca=str(self.ca), auth=self.auth))).hexdigest()


@dataclass(frozen=True)
class NativePins:
    config_version: str
    config_digest: str
    credential_version: str
    provider_profile: str
    provider_digest: str
    bootstrap_model: str
    desired_model: str

    def __post_init__(self):
        canonical(self.config_version)
        canonical(self.credential_version)
        _sha(self.config_digest)
        _sha(self.provider_digest)
        if (type(self.provider_profile) is not str
                or re.fullmatch(r'[a-z][a-z0-9-]{0,63}', self.provider_profile) is None
                or self.config_version == self.credential_version):
            raise Refused('distinct exact native config/credential pins required')
        model_id(self.bootstrap_model)
        model_id(self.desired_model)
        if self.bootstrap_model == self.desired_model:
            raise Refused('explicit distinct bootstrap-to-desired models required')

    def wire(self):
        return {name: getattr(self, name) for name in NativePins.__dataclass_fields__}

    @classmethod
    def parse(cls, value):
        exact(value, cls.__dataclass_fields__)
        return cls(**value)


@dataclass(frozen=True)
class ModelInputs(NativePins):
    token: str = field(repr=False)

    def __post_init__(self):
        super().__post_init__()
        if (type(self.token) is not str or not 1 <= len(self.token) <= 8192
                or any(not 33 <= ord(c) <= 126 for c in self.token)):
            raise Refused('bounded private native provider credential required')

    @classmethod
    def parse(cls, delivery, profile):
        from mop.common.credentials import CredentialError, binary
        if type(profile) is not ProviderProfile:
            raise Refused('explicit approved installed provider profile required')
        try:
            if type(delivery) is not dict:
                raise Refused('exact native delivery required')
            work = Work.parse(delivery['work'])
            if work.action != 'native_exec':
                raise Refused('native model bytes only belong to native_exec')
            value = _delivery(delivery, work)
            consumers = value['consumers']
            configs = [item for item in consumers if item['purpose'] == 'model_config']
            credentials = [item for item in consumers if item['purpose'] == 'model_credential']
            if len(configs) != 1 or len(credentials) != 1 or len(consumers) != 2:
                raise Refused('exactly one approved native config and credential required')
            config, credential = configs[0], credentials[0]
            config_raw, credential_raw = binary(config['value']), binary(credential['value'])
            if len(config_raw) > 4096 or len(credential_raw) > 16384:
                raise Refused('bounded native model consumer bytes required')
            c, k = _json(config_raw), _json(credential_raw)
            exact(c, ('schema_version', 'profile_version', 'provider_profile', 'credential_version',
                      'proof_strategy', 'bootstrap_model', 'desired_model'))
            exact(k, ('schema_version', 'provider_profile', 'token'))
            if (type(c['schema_version']) is not int or c['schema_version'] != 1
                    or type(c['profile_version']) is not int or c['profile_version'] != 1
                    or type(k['schema_version']) is not int or k['schema_version'] != 1
                    or c['provider_profile'] != profile.identifier or k['provider_profile'] != profile.identifier
                    or c['proof_strategy'] != 'bootstrap-to-desired'
                    or c['credential_version'] != credential['version_id']
                    or c['desired_model'] != value['selection']['desired_model']):
                raise Refused('approved native strategy/version/provider/model pins disagree')
            return cls(config['version_id'], value['selection']['model_config_digest'],
                credential['version_id'], profile.identifier, profile.digest,
                c['bootstrap_model'], c['desired_model'], k['token'])
        except Refused:
            raise
        except (CredentialError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
            # Ни raw parser exception, ни consumer bytes не выходят в диагностику.
            raise Refused('invalid exact approved native model inputs') from None

    def pins(self):
        return NativePins(*(getattr(self, name) for name in NativePins.__dataclass_fields__))

    def environment(self, profile):
        if (type(profile) is not ProviderProfile or profile.identifier != self.provider_profile
                or profile.digest != self.provider_digest):
            raise Refused('native credential cannot change its installed provider')
        # Names/kinds consumers не определяют имена env, файлов или argv.
        return dict(ANTHROPIC_BASE_URL=profile.origin, ANTHROPIC_API_KEY=self.token,
                    NODE_EXTRA_CA_CERTS=str(profile.ca))


@dataclass(frozen=True)
class NativeSubmission:
    admission: Admission
    descriptor: Descriptor
    projection: Projection
    reference: Reference
    pins: NativePins

    def __post_init__(self):
        validate_admission(self.admission)
        if (self.admission.work.action != 'native_exec' or type(self.descriptor) is not Descriptor
                or type(self.projection) is not Projection or type(self.reference) is not Reference
                or type(self.pins) is not NativePins):
            raise Refused('exact native Admission/descriptor/projection/reference/pins required')
        validate_scope(self.descriptor.scope, self.admission)
        if (self.descriptor.attempt_id != self.admission.attempt_id
                or self.reference.scope != self.descriptor.scope
                or (self.projection.role, self.projection.bundle_digest) !=
                   (self.admission.work.role, self.admission.work.bundle_digest)):
            raise Refused('native submission changed exact admitted scope/attempt/role')

    def wire(self):
        return dict(schema_version=1, action='native_exec', admission=admission_wire(self.admission),
            descriptor=self.descriptor.wire(), projection=self.projection.wire(),
            reference=self.reference.wire(), pins=self.pins.wire())

    def encode(self):
        return encode(self.wire())

    @classmethod
    def decode(cls, raw):
        value = decode(raw)
        exact(value, ('schema_version', 'action', 'admission', 'descriptor', 'projection', 'reference', 'pins'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['action'] != 'native_exec':
            raise Refused('unsupported native submission schema/action')
        return cls(parse_admission(value['admission']), Descriptor.parse(value['descriptor']),
            Projection.parse(value['projection']), Reference.parse(value['reference']), NativePins.parse(value['pins']))


def confirm_admission(admission):
    # confirm не получает input Envelope: не расширять PURPOSES transport #746.
    if (type(admission) is not Admission or type(admission.work) is not Work
            or Work.parse(admission.work.wire()) != admission.work or admission.work.action != 'confirm_ready'):
        raise Refused('actual confirm_ready Admission required')
    canonical(admission.attempt_id)
    canonical(admission.request_id)
    if (type(admission.revision) is not int or not 0 < admission.revision < 2**63
            or type(admission.lease_until) is not datetime or admission.lease_until.tzinfo is None
            or not datetime.now(timezone.utc) < admission.lease_until <= admission.work.not_after):
        raise Refused('current nonrenewable confirm admission lease required')
    return admission


@dataclass(frozen=True)
class ConfirmSubmission:
    admission: Admission
    reference: Reference
    projection: Projection
    native_attempt: str

    def __post_init__(self):
        confirm_admission(self.admission)
        canonical(self.native_attempt)
        if (type(self.reference) is not Reference or type(self.projection) is not Projection
                or self.native_attempt == self.admission.attempt_id):
            raise Refused('confirm requires the original distinct native attempt and exact retained reference')
        validate_scope(self.reference.scope, self.admission)
        if (self.projection.role, self.projection.bundle_digest) != (self.admission.work.role, self.admission.work.bundle_digest):
            raise Refused('confirm changed immutable native role binding')

    def wire(self):
        return dict(schema_version=1, action='confirm_ready', admission=admission_wire(self.admission),
            reference=self.reference.wire(), projection=self.projection.wire(), native_attempt=self.native_attempt)

    def encode(self):
        return encode(self.wire())

    @classmethod
    def decode(cls, raw):
        value = decode(raw)
        exact(value, ('schema_version', 'action', 'admission', 'reference', 'projection', 'native_attempt'))
        if type(value['schema_version']) is not int or value['schema_version'] != 1 or value['action'] != 'confirm_ready':
            raise Refused('unsupported confirm submission schema/action')
        a = value['admission']
        exact(a, ('work', 'request_id', 'attempt_id', 'revision', 'lease_until'))
        admission = Admission(Work.parse(a['work']), canonical(a['request_id']), canonical(a['attempt_id']),
                              counter(a['revision']), stamp(a['lease_until']))
        return cls(admission, Reference.parse(value['reference']), Projection.parse(value['projection']), value['native_attempt'])
