"""Токен посадки проекта (#42): ровно один папет между merge и push.

Два гейта параллельно гоняются друг с другом, и второй push отскакивает
non-fast-forward, прогнав гейт по интеграции, которой уже нет. Пока мастер
проекта был один, токен жил у него в голове; у двух мастеров одного проекта
головы две, и токена не было вовсе. Теперь он у сервиса кластера (глагол
`landing`), по проекту из субъекта.

Держатель -- логин мастера, папет, которому токен выдан, и с какого времени.
Истечения нет: зависший держатель снимается `force`, и ответ называет, у
кого забрали. Чистая часть -- take/give; файл -- сервиса, рядом с лимитами.
"""
import json
import os

from . import fsutil

# Файл сервиса кластера на сервере, рядом с limits.json. Не прогона: deploy
# его не кладёт и не трогает, иначе прогон посреди посадки ронял бы токен.
FILE = os.path.expanduser("~/.config/mop/landing.json")


def holder():
    """Кто берёт токен: логин человека на шине, как владелец задания (#161).

    Сегодня его называет сам проситель -- одно это место и заменит проверка
    личности (#207)."""
    from . import bus
    return bus.login()


def _held(token):
    return (f"{token['holder']} for {token['puppet']} since {token['since']}")


def take(tokens, project, who, puppet, now, force=False):
    """CAS: взять токен проекта. -> (новые токены, ответ).

    Свободен -- взят. У того же держателя с тем же папетом -- повтор, а не
    отказ: ответ мог потеряться, и время остаётся прежним. Тот же держатель,
    но другой папет -- отказ: токен -- ровно один папет, и выдать его второму,
    не забрав у первого, значит снова две посадки разом. force забирает у
    любого и называет, у кого."""
    held = tokens.get(project)
    if held and not force:
        if held["holder"] == who and held["puppet"] == puppet:
            return tokens, {"ok": True, "token": held}
        return tokens, {"error": (
            f"landing token of {project} is held by {_held(held)}; "
            f"wait for mop landing give, or take it from a gone master: "
            f"mop landing take {puppet} --force")}
    token = {"holder": who, "puppet": puppet, "since": now}
    return dict(tokens, **{project: token}), {"ok": True, "token": token,
                                              "previous": held}


def give(tokens, project, who, force=False):
    """Вернуть токен проекта. -> (новые токены, ответ).

    Только держатель; force -- кто угодно, с именем прежнего держателя.
    Свободный токен вернуть -- не ошибка: повтор после потерянного ответа."""
    held = tokens.get(project)
    if held and held["holder"] != who and not force:
        return tokens, {"error": (
            f"landing token of {project} is held by {_held(held)}, not {who}; "
            f"take it back from a gone master: mop landing give --force")}
    rest = {k: v for k, v in tokens.items() if k != project}
    return rest, {"ok": True, "previous": held}


def read(path=None):
    """{проект: {holder, puppet, since}}; нет файла или он битый -- токенов
    нет: глагол не должен падать на файле, который пишет он сам."""
    try:
        with open(path or FILE) as f:
            got = json.load(f)
    except (OSError, ValueError):
        return {}
    return got if isinstance(got, dict) else {}


def write(tokens, path=None):
    path = path or FILE
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fsutil.write_atomic(path, json.dumps(tokens, sort_keys=True))
