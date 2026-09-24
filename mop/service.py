"""Каркас RPC-сервиса сервера на шине: cluster, bootstrap, builder (#149).

Раньше каждый сервис нёс свою копию: подключение с одними и теми же
параметрами переподключения, разбор JSON, исполнитель, строку журнала и
ответ. Копии разошлись -- проект из субъекта разбирался двумя правилами, --
и все три печатали из библиотеки.

Сервис здесь -- обработчик, который возвращает ответ, и две функции, которые
возвращают строки журнала. Печатает командлет: он отдаёт сюда log.
"""
import asyncio
import json

from . import bus


def project_from_subject(subject):
    """Проект из субъекта `mop.<проект>.<канал>.rpc`.

    Из субъекта, а не из тела запроса: субъект проверен правами NATS, тело
    пишет кто угодно. Нет третьего токена -- нет проекта: подписки сервисов
    ловят ровно четыре, короче сюда ничего не приходит."""
    parts = (subject or "").split(".")
    return parts[1] if len(parts) > 2 else ""


async def serve(name, subject, handler, log, journal, banner):
    """Подписчик сервера: живёт, пока жив процесс.

    handler(project, req, send) -> ответ; зовётся в отдельном потоке: вызовы
    Nomad и прогоны ansible блокирующие, а петля обязана отвечать остальным,
    пока один запрос ждёт. send(**событие) -- промежуточное событие в инбокс
    просителя (поток сборки, bus.ask_stream). journal(project, req, ответ) ->
    [строки журнала], banner() -> строка старта; обе уходят в log."""
    import nats
    nc = await nats.connect(**bus.auth(bus.config()), name=name,
                            allow_reconnect=True, max_reconnect_attempts=-1,
                            reconnect_time_wait=2)
    loop = asyncio.get_running_loop()

    async def handle(msg):
        project = project_from_subject(msg.subject)
        try:
            req = json.loads(msg.data.decode())
        except ValueError:
            req = {}

        def send(**ev):
            data = json.dumps(ev, ensure_ascii=False).encode()
            asyncio.run_coroutine_threadsafe(nc.publish(msg.reply, data), loop)

        out = await loop.run_in_executor(None, handler, project, req, send)
        for line in journal(project, req, out):
            log(line)
        try:
            await msg.respond(json.dumps(out, ensure_ascii=False).encode())
        except Exception:
            pass

    async def on_msg(msg):
        # Каждый запрос -- своя задача: последовательная обработка заперла
        # бы сервис на первом долгом запросе.
        asyncio.create_task(handle(msg))

    await nc.subscribe(subject, cb=on_msg)
    log(banner())
    await asyncio.Event().wait()
