"""Чистая валидация каталога, без settings/ORM и чтения прежних реестров."""
from dataclasses import dataclass
import re
from urllib.parse import urlsplit

from ... import driver

MAX_ORIGIN_BYTES = 2048
MAX_PROJECT_NAME = 128


@dataclass(frozen=True)
class ProjectOrigin:
    origin: str
    name: str


def project_origin(value):
    if not isinstance(value, str):
        raise ValueError("project origin must be a string")
    origin = value.strip()
    if (not origin or len(origin.encode("utf-8")) > MAX_ORIGIN_BYTES
            or origin.startswith("-") or re.match(r"^[A-Za-z][A-Za-z0-9+.-]*::", origin)
            or any(c.isspace() or ord(c) < 32 or ord(c) == 127
                                             for c in origin)):
        raise ValueError("invalid project origin")
    parsed = driver.parse_origin(origin)
    if parsed is None:
        raise ValueError("project origin must name a repository location")
    scheme, user, host, _, path = parsed
    if scheme not in {"file", "ssh", "http", "https", "git"} or not path:
        raise ValueError("invalid project origin")
    if scheme != "file" and (not host or host.startswith("-")):
        raise ValueError("project origin has no valid host")
    if "://" in origin:
        try:
            url = urlsplit(origin)
            port = url.port
            if scheme != "file" and not url.hostname:
                raise ValueError
        except ValueError:
            raise ValueError("invalid project origin") from None
        if (url.password is not None or url.query or url.fragment
                or (scheme != "ssh" and url.username is not None)):
            raise ValueError("project origin must not contain credentials or URL parameters")
        if port is not None and not 1 <= port <= 65535:
            raise ValueError("invalid project origin port")
    name = driver.project_of(origin)
    if len(name) > MAX_PROJECT_NAME or not driver.valid_name(f"{driver.PREFIX}{name}-1"):
        raise ValueError("project origin has no valid project name")
    return ProjectOrigin(origin, name)


def project_limit(value):
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError("project limit must be a nonnegative integer or null")
    return value
