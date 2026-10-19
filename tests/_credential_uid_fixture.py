"""Только owned Docker fixture (#674): настоящий root gate перекрёстных UID.

Единственная точка входа узкого gate: SETUP объявленного CI job зовёт её
под root после создания пользователей и до su ci; локальный режим
`mop dev controller --check credential_service --cross-uid` -- той же
командой как TESTS. Проверяет реальную пару UID (root и nobody/65534 --
ровно те два пира, между которыми случаи #669 проверяют SO_PEERCRED),
отказывает вне root и гоняет исходные три случая плюс private send/take
через общий Django runner: настоящий PostgreSQL, разные UID, не мок.
Новый startup использует только существующих пользователей owned контейнера,
не host users, pool или секреты установки.
"""
import os
import pwd
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

GATE_LABELS = (
    '_endpoint_credential_uid_gate.EndpointCredentialUids'
    '.test_private_wss_default_root_crosses_actual_frontend_uid',
    '_credential_uid_gate.CredentialSocketUids'
    '.test_distinct_frontend_uid_uses_only_group_protected_socket',
    '_credential_uid_gate.AddressedCredentialUids'
    '.test_addressed_delivery_uses_actual_frontend_uid_without_rights_inference',
    '_credential_uid_gate.InstalledCredentialUids'
    '.test_fresh_installer_starts_rootless_daemon_with_separate_pg_login_and_frontend_uid',
    '_endpoint_credential_uid_gate.EndpointCredentialUids'
    '.test_private_reports_cross_real_uid_without_root_or_caller_authority',
    '_credential_uid_gate.ManagementContextUids'
    '.test_management_context_actual_frontend_uid_has_no_human_or_generic_token_authority',
)


def uid_pair():
    """Существующая пара UID gate: (сервер 0, фронтенд 65534).

    root и nobody есть в любом Debian-образе тест-контейнера; отсутствие
    любого -- отказ fixture, а не молчаливый обход границы.
    """
    if os.geteuid() != 0:
        raise SystemExit('the uid gate runs only as root inside the owned test container')
    try:
        peer = pwd.getpwnam('nobody').pw_uid
    except KeyError:
        raise SystemExit('owned fixture is missing the nobody uid') from None
    if peer == os.geteuid():
        raise SystemExit('the uid gate requires two distinct uid')
    return os.geteuid(), peer


def main():
    uid_pair()
    from _controller_fixture import main as run
    return run(list(GATE_LABELS))


if __name__ == '__main__':
    sys.exit(main())
