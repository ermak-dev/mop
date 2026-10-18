"""Значения нового запуска. Никаких ORM, node imports и файлов authority.

Registration приходит из доверенной композиции нового job registration;
LaunchResponse — только из authenticated addressed API. Сам digest не доказывает
approval: проверку владельца/approval/актуальности выполняет серверный source.
"""
import base64
import binascii
from dataclasses import dataclass, field
import ipaddress
import os
from pathlib import Path
import re
from urllib.parse import urlsplit
from uuid import UUID

from mop.common import soul
from mop.driver import valid_name


def _uuid(value):
    if type(value) is not str or str(UUID(value)) != value:
        raise ValueError('canonical launch UUID required')


def _digest(value):
    if type(value) is not str or not re.fullmatch('[0-9a-f]{64}', value):
        raise ValueError('pinned SHA256 digest required')


def _host(value):
    if type(value) is not str or not value or len(value) > 253:
        raise ValueError('explicit machine host required')
    try:
        ipaddress.ip_address(value)
    except ValueError:
        if not re.fullmatch(r'[a-zA-Z0-9](?:[a-zA-Z0-9.-]*[a-zA-Z0-9])?', value):
            raise ValueError('machine host must not contain URI, port or shell syntax') from None


@dataclass(frozen=True)
class MachineConfig:
    control_origin: str
    routing_reference: str
    bootstrap_host: str
    ssh_port: int
    driver: str

    def __post_init__(self):
        if (type(self.control_origin) is not str or len(self.control_origin) > 2048
                or any(ord(c) < 33 or ord(c) == 127 for c in self.control_origin)):
            raise ValueError('explicit control origin without whitespace required')
        parts = urlsplit(self.control_origin)
        if (parts.scheme != 'https' or not parts.hostname or parts.username or parts.password
                or parts.path not in ('', '/') or parts.query or parts.fragment):
            raise ValueError('explicit verified HTTPS control origin required')
        parts.port
        _host(self.routing_reference)
        _host(self.bootstrap_host)
        if type(self.ssh_port) is not int or not 1 <= self.ssh_port <= 65535:
            raise ValueError('explicit bootstrap SSH port required')
        if self.driver not in ('host', 'pve'):
            raise ValueError('unsupported body driver')

    @property
    def bootstrap_address(self):
        host = '['+self.bootstrap_host+']' if ':' in self.bootstrap_host else self.bootstrap_host
        return host if self.ssh_port == 22 else f'{host}:{self.ssh_port}'


@dataclass(frozen=True)
class Registration:
    # work — metadata DTO #640; common-driver не зависит от node или Django.
    # Это in-memory input от registration root, не второй persistent реестр.
    work: object
    bootstrap_revision: str
    bootstrap_digest: str
    image_digest: str
    display_name: str

    def __post_init__(self):
        for name in ('epoch', 'operation_id', 'step_id', 'target_id', 'incarnation_id',
                     'node_id', 'executor_epoch', 'grant_id'):
            _uuid(getattr(self.work, name))
        for name in ('owner_id', 'generation', 'revision'):
            value = getattr(self.work, name)
            if type(value) is not int or not 1 <= value < 2**63:
                raise ValueError('positive launch generation and revision required')
        if not re.fullmatch('[a-z][a-z0-9_-]{0,63}', self.work.role):
            raise ValueError('pinned role required')
        _digest(self.work.bundle_digest)
        _uuid(self.bootstrap_revision)
        _digest(self.bootstrap_digest)
        _digest(self.image_digest)
        if (type(self.display_name) is not str or not valid_name(self.display_name)
                or any(ord(c) < 33 or ord(c) == 127 for c in self.display_name)):
            raise ValueError('registered display name required')

    def matches(self, work):
        # Включает operation/step/revision/deadline, не только имя или role triple.
        return self.work == work


@dataclass(frozen=True)
class BundlePin:
    """Метаданные одобренного определения без байтов (#643).

    Регистрирующий job контроллер байтов не видит -- они зашифрованы и
    расшифровываются только credential-сервисом; спеке хватает роли, digest и
    драйвера, а байты сверяет узел в LaunchResponse.check."""
    role: str
    digest: str
    driver: str

    def __post_init__(self):
        if type(self.role) is not str or not re.fullmatch('[a-z][a-z0-9_-]{0,63}', self.role):
            raise ValueError('pinned role required')
        _digest(self.digest)
        if self.driver not in ('any', 'isolating'):
            raise ValueError('known runtime driver required')


@dataclass(frozen=True)
class RegisteredLayout:
    home: Path
    registration: Registration

    def __post_init__(self):
        if (not isinstance(self.home, Path) or not self.home.is_absolute()
                or '..' in self.home.parts or type(self.registration) is not Registration):
            raise ValueError('trusted absolute registered layout required')
        # sockaddr_un вмещает 108 байт sun_path c завершающим NUL (#677):
        # layout, чей session.sock не помещается, отказывает здесь, до spawn --
        # иначе тело умирает на bind молча, до первой готовности. Граница
        # проверена на настоящем kernel: 107 байт bind проходит, 108 -- нет.
        if len(os.fsencode(self.runtime/'session.sock')) > 107:
            raise ValueError('registered runtime path does not fit sockaddr_un')

    @property
    def clone(self):
        return self.home/'puppets'/self.registration.display_name

    @property
    def runtime(self):
        w = self.registration.work
        # Incarnation UUID глобален; полное значение, без хеш-усечения. Два
        # UUID в Unix pathname не помещались в sockaddr_un уже при обычном HOME.
        return self.home/'runtime'/w.incarnation_id

    @property
    def tmux_socket(self):
        return self.runtime/'tmux'

    @property
    def launcher(self):
        return self.runtime/'launch'


@dataclass(frozen=True)
class LaunchResponse:
    registration: Registration
    bundle: soul.Bundle
    # ActionInput проверяет node adapter; driver не импортирует верхний слой.
    inputs: object = field(repr=False)

    def check(self, registration):
        if self.registration != registration:
            raise ValueError('launch assignment or bootstrap pin changed')
        if (type(self.bundle) is not soul.Bundle or self.bundle.role != registration.work.role
                or self.bundle.digest != registration.work.bundle_digest
                or soul.digest(self.bundle.manifest.to_dict(), self.bundle.prompt) != self.bundle.digest):
            raise ValueError('approved bundle bytes do not match registration')

    @classmethod
    def parse(cls, value, registration, *, input_factory=None):
        # input_factory — конечный typed constructor composition, не wire class.
        # Импорт node здесь запрещён; по умолчанию framework-free input значение.
        names = {'schema_version', 'work', 'bootstrap_revision', 'bootstrap_digest',
                 'image_digest', 'manifest', 'prompt', 'git_name', 'git_email', 'message', 'peer_token'}
        if (type(value) is not dict or set(value) != names
                or type(value['schema_version']) is not int or value['schema_version'] != 1):
            raise ValueError('unknown launch response fields or schema')
        if (type(value['work']) is not dict
                or type(value['work'].get('schema_version')) is not int
                or value['work'] != registration.work.wire()
                or any(value[key] != getattr(registration, key) for key in
                       ('bootstrap_revision', 'bootstrap_digest', 'image_digest'))):
            raise ValueError('addressed launch response changed registration pins')
        try:
            encoded = value['prompt']
            if type(encoded) is not str or len(encoded) > 262144:
                raise ValueError('bounded approved prompt required')
            prompt = base64.b64decode(encoded, validate=True)
            if base64.b64encode(prompt).decode() != encoded:
                raise ValueError('canonical approved bytes required')
            bundle = soul.from_manifest(value['manifest'], prompt)
        except (binascii.Error, TypeError) as error:
            raise ValueError('invalid approved bundle bytes') from error
        if input_factory is None:
            raise ValueError('typed action input constructor must be bound by node composition')
        inputs = input_factory(value['git_name'], value['git_email'], value['message'], value['peer_token'])
        inputs.check(registration.work.action)
        result = cls(registration, bundle, inputs)
        result.check(registration)
        return result
