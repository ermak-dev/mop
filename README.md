# orchestra

Пул claude-воркеров поверх Nomad: как он разворачивается, чем управляется и как
с сиденьями разговаривать.

```
bin/worker     CLI пула: создать/удалить/перезапустить сиденье, list, doctor, login
bin/cc-send    послать сообщение в живую сессию claude code (см. CHANNEL.md)
nomad/         ansible-плейбуки и конфиги: сам Nomad, узлы, уборка диска
CHANNEL.md     протокол канала сообщений claude code
```

## Как это связано

`worker` ходит в Nomad по REST (`https://nomad.ermak.dev`), а команды внутри
аллокаций гоняет через exec-websocket; ssh нужен только для `worker attach`,
ради живого терминала. Токен берётся из `$NOMAD_TOKEN` или
`~/.config/nomad/bootstrap.json`.

Каждое сиденье — это job Nomad, чей врапер доводит узел до состояния «клон
есть, claude живёт в tmux» и держится, пока жива tmux-сессия. Смерть врапера =
рестарт или переезд сиденья силами Nomad.

## Симлинки наружу

Проект живёт отдельным репозиторием, но два пути снаружи на него ссылаются —
это интерфейсы, которые нельзя переименовать:

```
~/bin/worker    -> ../orchestra/bin/worker
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
* `~/.ssh/ai-provider-keys.env` — ключи LLM-провайдеров; `worker` вывозит на
  узлы только те, что называет хоть один профиль в `LLM_PROFILES`.
* `~/.claude/.credentials.json` — логин claude.ai, который `worker login`
  раздаёт на узлы пула.

## Запуск плейбуков

```
net setup nomad                                  # или:
ansible-playbook -i ~/etc/inventory.ini ~/etc/nomad/setup.yml
ansible-playbook -i ~/etc/inventory.ini ~/etc/nomad/claude.yml
```
