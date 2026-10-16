"""Только owned Docker fixture (#674): настоящий root gate перекрёстных UID.

Единственная точка входа узкого gate: SETUP объявленного CI job зовёт её
под root после создания пользователей и до su ci; локальный режим
`mop dev controller --check credential_service --cross-uid` -- той же
командой как TESTS. Проверяет реальную пару UID (root и nobody/65534 --
ровно те два пира, между которыми случаи #669 проверяют SO_PEERCRED),
отказывает вне root и гоняет ровно два перенесённых случая через общий
Django runner: настоящий PostgreSQL, настоящий второй UID, не мок.
Никаких host users, pool, docker или секретов установки.
"""
import os
import pwd
import sys

sys.path.insert(0, os.path.dirname(os.path.realpath(__file__)))

GATE_LABELS = (
    '_credential_uid_gate.CredentialSocketUids'
    '.test_distinct_frontend_uid_uses_only_group_protected_socket',
    '_credential_uid_gate.AddressedCredentialUids'
    '.test_addressed_delivery_uses_actual_frontend_uid_without_rights_inference',
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
