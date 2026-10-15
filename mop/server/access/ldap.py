"""ldap3 adapter с явной ORM configuration и protected secret port, без файлов."""
from contextlib import contextmanager
import re
import ssl
from typing import Protocol
from urllib.parse import urlsplit
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
                or not re.fullmatch('[A-Za-z][A-Za-z0-9-]*', cfg.login_attribute)):
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
                yield None
            else:
                yield conn
        except (LDAPException, OSError, UnicodeError, ValueError):
            raise DirectoryUnavailable('directory transport or trust is unavailable') from None
        finally:
            if conn is not None:
                conn.unbind()

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
            conn.search(cfg.user_base, filt, attributes=[cfg.login_attribute], size_limit=2)
            if conn.result.get('result') not in (0, 4):
                raise DirectoryUnavailable('directory user search failed')
            entries = tuple(e for e in conn.response if e.get('type') == 'searchResEntry')
            if len(entries) != 1 or conn.result.get('result') == 4:
                return tuple(DirectoryAccount(e['dn'], username, ()) for e in entries) if len(entries) > 1 else (
                    DirectoryAccount('ambiguous-a', username, ()), DirectoryAccount('ambiguous-b', username, ())) if conn.result.get('result') == 4 else ()
            dn = entries[0]['dn']
            member = 'member:1.2.840.113556.1.4.1941:' if cfg.nested_ad else 'member'
            group_filter = '(|(' + member + '=' + escape_filter_chars(dn) + ')(uniqueMember=' + escape_filter_chars(dn) + ')(memberUid=' + escape_filter_chars(username) + '))'
            conn.search(cfg.group_base, group_filter, attributes=['cn'], size_limit=500)
            if conn.result.get('result') != 0:
                raise DirectoryUnavailable('directory group search failed or exceeded limit')
            groups = tuple(e['dn'] for e in conn.response if e.get('type') == 'searchResEntry')
            return (DirectoryAccount(dn, username, groups, bind_dn=dn),)

    @sensitive_variables('password')
    def check_password(self, cfg, subject, password):
        if not password:
            return False
        with self._connection(cfg, subject, password) as conn:
            return conn is not None
