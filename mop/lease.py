"""Владелец задания: кто сейчас ведёт папета (#161). Данные, без печати.

Только stdlib: модуль читает и агент узла, и мастер.

Владеет не папетом, а заданием. Папет принадлежит проекту, и свободного
отдать другому ничего не стоит: «free» и значит, что в клоне нет
несохранённой работы. Аренда нужна ровно в двух случаях — в клоне чужая
работа, или папет только что получил чужой тикет и ещё не завёл ветку.

Запись лежит в клоне, `.git/mop-owner`, и живёт столько же, сколько работа:
`wipe` и рецикл уносят её вместе с клоном. Реестр в Nomad Variables, который
пережил бы работу, отложен вместе с остальным эпиком #36.

Это координация, а не защита: логин называет сам отправитель, агент видит
только проект из субъекта.
"""
FILE = ".git/mop-owner"

# Окно диспатча: столько чужая аренда держит папета, у которого в клоне ещё
# пусто. За это время папет заводит ветку, и дальше держит уже работа.
WINDOW = 600

# Сама запись -- значение domain.Owner (#204): строка файла -- его render и
# parse, здесь только политика над ним.


def holds_work(clone):
    """Есть ли в клоне работа: несохранённое, неотправленное или ветка не
    по умолчанию. Клон неизвестен -- держит: «не знаю» не значит «пусто»."""
    if not clone:
        return True
    if clone.get("dirty") or clone.get("ahead"):
        return True
    cur, default = clone.get("cur"), clone.get("def")
    return bool(cur and default and cur != default)


def live(owner, clone, now):
    """Держит ли аренда папета: в клоне работа или она моложе окна."""
    if not owner:
        return False
    return holds_work(clone) or now - owner.at < WINDOW


def may_touch(owner, me, clone, now, force=False, operator=False, retry="repeat"):
    """Можно ли me изменить папета, чьё задание ведёт owner (#40).
    -> (можно, причина).

    Одни ворота на все изменяющие действия: send, slash/type, wipe, restart,
    stop, update, delete, recycle. Пока владельца сверял один send, любой
    мастер проекта рестартом или сносом убивал работу другого посреди тикета.

    Свой или ничей -- можно. Чужой -- нельзя, пока аренда живая (в клоне
    работа или диспатч моложе окна); безымянному тоже нельзя, иначе ворота
    обходятся тем, что не назваться. force -- можно, причина называет, у
    кого. Оператор (субъект admin: gc, doctor, sweep) проходит всегда: узел
    и пул -- его, и упереться в аренду значит не вылечить пул. retry --
    глагол подсказки в отказе: у send она прежняя, «send with force»."""
    if operator or not owner or owner.user == me:
        return True, None
    if force:
        return True, f"taken from {owner.user}"
    if not live(owner, clone, now):
        return True, None
    minutes = max(int((now - owner.at) // 60), 0)
    what = "work in the clone" if holds_work(clone) else "dispatched, no branch yet"
    return False, (f"led by {owner.user} ({what}, last sent {minutes} min ago): "
                   f"ask them, or {retry} with force to take it over")


def verdict(owner, me, clone, now, force=False):
    """Что делать с `send` от me. owner -- domain.Owner или None.
    -> (действие, причина).

    действие: "pass" -- слать, аренду не трогать (отправитель не назвался:
    папет пишет соседу); "take" -- слать и записать me владельцем;
    "refuse" -- не слать. Причина -- для отказа и для отобранной аренды.
    Решает may_touch (#40): send отличается только тем, что пишет аренду."""
    if not me:
        return "pass", None
    ok, why = may_touch(owner, me, clone, now, force, retry="send")
    return ("take" if ok else "refuse"), why
