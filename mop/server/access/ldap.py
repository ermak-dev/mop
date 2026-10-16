"""ldap3 adapter с явной ORM configuration и protected secret port, без файлов."""
from contextlib import contextmanager
import re
import ssl
from typing import Protocol
from urllib.parse import urlsplit
from uuid import UUID
from django.views.decorators.debug import sensitive_variables
from .directory import DirectoryAccount, DirectoryUnavailable


class BindSecrets(Protocol):
    def bind_password(self, source_id, reference: str) -> bytes: ...
    def ca_pem(self, source_id, reference: str) -> bytes: ...


class UnboundSecrets:
    def bind_password(self, source_id, reference):
        raise DirectoryUnavailable('directory bind secret resolver is not bound')

    def ca_pem(self, source_id, reference):
        raise DirectoryUnavailable('directory trust resolver is not bound')


class LdapDirectory:
    def __init__(self, *, secrets: BindSecrets | None = None):
        self.secrets = secrets if secrets is not None else UnboundSecrets()

    def _server(self, cfg):
        parsed = urlsplit(cfg.url)
        if (parsed.scheme not in ('ldap', 'ldaps') or not parsed.hostname or parsed.username is not None
                or parsed.password is not None or parsed.query or parsed.fragment or parsed.path not in ('', '/')
                or (parsed.scheme == 'ldap' and not cfg.starttls)
                or not re.fullmatch('[A-Za-z][A-Za-z0-9-]*', cfg.login_attribute)
                or cfg.subject_attribute not in ('entryUUID', 'objectGUID')):
            raise DirectoryUnavailable('invalid secure directory configuration')
        import ldap3
        ca = self.secrets.ca_pem(cfg.source_id, cfg.ca_secret_ref).decode('ascii') if cfg.ca_secret_ref else None
        tls = ldap3.Tls(validate=ssl.CERT_REQUIRED, ca_certs_data=ca)
        return ldap3.Server(cfg.url, use_ssl=parsed.scheme == 'ldaps', tls=tls,
            get_info=ldap3.NONE, connect_timeout=3)

    @contextmanager
    @sensitive_variables('password', 'conn')
    def _connection(self, cfg, dn, password):
        import ldap3
        from ldap3.core.exceptions import LDAPException
        conn = None
        try:
            conn = ldap3.Connection(self._server(cfg), user=dn, password=password,
                receive_timeout=3, raise_exceptions=False, auto_referrals=False)
            conn.open()
            if cfg.starttls and not conn.start_tls():
                raise DirectoryUnavailable('directory TLS negotiation failed')
            if not conn.bind():
                # RFC 4511: только invalidCredentials (49) — отказ credentials.
                # busy/unavailable/unwillingToPerform не означают неверный пароль.
                if conn.result.get('result') != 49:
                    raise DirectoryUnavailable('directory bind is unavailable')
                yield None
            else:
                yield conn
        except (LDAPException, OSError, UnicodeError, ValueError):
            raise DirectoryUnavailable('directory transport or trust is unavailable') from None
        finally:
            if conn is not None:
                try:
                    conn.unbind()
                except (LDAPException, OSError, UnicodeError, ValueError):
                    raise DirectoryUnavailable('directory transport cleanup failed') from None

    @staticmethod
    def _subject(cfg, entry):
        attributes = entry.get('raw_attributes', entry.get('attributes', {}))
        values = [value for name, value in attributes.items()
                  if name.casefold() == cfg.subject_attribute.casefold()]
        if len(values) != 1:
            raise DirectoryUnavailable('directory immutable subject is missing or ambiguous')
        values = values[0]
        if isinstance(values, (str, bytes)):
            values = (values,)
        if not isinstance(values, (tuple, list)) or len(values) != 1:
            raise DirectoryUnavailable('directory immutable subject is missing or ambiguous')
        value = values[0]
        try:
            # AD хранит первые три компонента GUID в little-endian порядке.
            if cfg.subject_attribute == 'objectGUID' and isinstance(value, bytes):
                return str(UUID(bytes_le=value))
            if isinstance(value, bytes):
                value = value.decode('ascii')
            if not isinstance(value, str):
                raise ValueError
            return str(UUID(value))
        except (ValueError, UnicodeError, TypeError):
            raise DirectoryUnavailable('directory immutable subject is invalid') from None

    @sensitive_variables('secret')
    def lookup(self, cfg, username):
        # Секрет запрашивается первым: без #637 не происходит даже network connect.
        secret = self.secrets.bind_password(cfg.source_id, cfg.bind_secret_ref)
        if not secret:
            raise DirectoryUnavailable('directory bind credential is unavailable')
        from ldap3.utils.conv import escape_filter_chars
        with self._connection(cfg, cfg.bind_dn, secret.decode('utf-8')) as conn:
            if conn is None:
                raise DirectoryUnavailable('directory service bind failed')
            filt = '(' + cfg.login_attribute + '=' + escape_filter_chars(username) + ')'
            attributes = [cfg.login_attribute, cfg.subject_attribute]
            conn.search(cfg.user_base, filt, attributes=attributes, size_limit=2)
            if conn.result.get('result') != 0:
                raise DirectoryUnavailable('directory user search failed or exceeded limit')
            entries = tuple(e for e in conn.response if e.get('type') == 'searchResEntry')
            if not entries:
                return ()
            if len(entries) != 1:
                raise DirectoryUnavailable('directory login is ambiguous')
            dn = entries[0]['dn']
            subject = self._subject(cfg, entries[0])
            # Одинаковый immutable id у разных DN — коллизия, не rename.
            if cfg.subject_attribute == 'objectGUID':
                encoded = ''.join('\\' + format(byte, '02x') for byte in UUID(subject).bytes_le)
            else:
                encoded = escape_filter_chars(subject)
            conn.search(cfg.user_base, '(' + cfg.subject_attribute + '=' + encoded + ')',
                attributes=attributes, size_limit=2)
            matches = tuple(e for e in conn.response if e.get('type') == 'searchResEntry')
            if (conn.result.get('result') != 0 or len(matches) != 1
                    or matches[0]['dn'] != dn or self._subject(cfg, matches[0]) != subject):
                raise DirectoryUnavailable('directory immutable subject is ambiguous')
            member = 'member:1.2.840.113556.1.4.1941:' if cfg.nested_ad else 'member'
            group_filter = '(|(' + member + '=' + escape_filter_chars(dn) + ')(uniqueMember=' + escape_filter_chars(dn) + ')(memberUid=' + escape_filter_chars(username) + '))'
            conn.search(cfg.group_base, group_filter, attributes=['cn'], size_limit=500)
            if conn.result.get('result') != 0:
                raise DirectoryUnavailable('directory group search failed or exceeded limit')
            groups = tuple(e['dn'] for e in conn.response if e.get('type') == 'searchResEntry')
            return (DirectoryAccount(subject, username, groups, bind_dn=dn),)

    @sensitive_variables('password')
    def check_password(self, cfg, subject, password):
        if not password:
            return False
        with self._connection(cfg, subject, password) as conn:
            return conn is not None
