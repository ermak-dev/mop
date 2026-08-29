# orchestra

Пул claude-слейвов поверх Nomad: как он разворачивается, чем управляется и как
с слейвами разговаривать.

```
orchestra/         библиотека: всё знание о пуле, возвращает данные и не печатает
  nomad.py           связь с Nomad, exec внутрь аллокации
  slaves.py           спека job'а, LLM-профили, состояние слейва, диагностика
  session.py         файлы сессий claude и протокол канала (standalone: ездит на узлы)
  remote.py          запуск session.py внутри аллокации
  keys.py            раздача кредов и ключей провайдеров на узлы
  render.py          таблицы
bin/slave         CLI пула: add/list/delete/change/restart/attach/tail/doctor/login/llm
bin/cc-send        послать сообщение в живую сессию claude code
bin/orchestra-mcp  MCP-сервер: тот же пул как инструменты для claude
nomad/             ansible-плейбуки и конфиги: сам Nomad, узлы, уборка диска
CHANNEL.md         протокол канала сообщений claude code
MCP.md             архитектура MCP-сервера и его инструменты
```

Печатают только фронтенды в `bin/`. Библиотека возвращает данные — иначе
MCP-сервер начал бы разбирать текст, свёрстанный для терминала.

## Как это связано

`slave` ходит в Nomad по REST (`https://nomad.ermak.dev`), а команды внутри
аллокаций гоняет через exec-websocket; ssh нужен только для `slave attach`,
ради живого терминала. Токен берётся из `$NOMAD_TOKEN` или
`~/.config/nomad/bootstrap.json`.

Каждое слейв — это job Nomad, чей врапер доводит узел до состояния «клон
есть, claude живёт в tmux» и держится, пока жива tmux-сессия. Смерть врапера =
рестарт или переезд слейва силами Nomad.

## Симлинки наружу

Проект живёт отдельным репозиторием, но два пути снаружи на него ссылаются —
это интерфейсы, которые нельзя переименовать:

```
~/bin/slave    -> ../orchestra/bin/slave
~/bin/cc-send   -> ../orchestra/bin/cc-send
~/etc/nomad     -> ../orchestra/nomad
```

`~/etc/nomad` обязателен: `net setup nomad` разворачивается в
`~/etc/nomad/setup.yml` по одному только соглашению о пути, и `~/etc/site.yml`
импортирует `nomad/setup.yml` относительно `~/etc`. Таблицы «имя → плейбук»
нигде нет, путь и есть таблица.

## Внешние зависимости

* `~/etc/inventory.ini` — инвентарь ansible, общий с остальными плейбуками
  (живёт в репозитории backup, сюда не переезжает).
* `~/.ssh/ai-provider-keys.env` — ключи LLM-провайдеров; `slave` вывозит на
  узлы только те, что называет хоть один профиль в `LLM_PROFILES`.
* `~/.claude/.credentials.json` — логин claude.ai, который `slave login`
  раздаёт на узлы пула.

## Запуск плейбуков

```
net setup nomad                                  # или:
ansible-playbook -i ~/etc/inventory.ini ~/etc/nomad/setup.yml
ansible-playbook -i ~/etc/inventory.ini ~/etc/nomad/claude.yml
```
