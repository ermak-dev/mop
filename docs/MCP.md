# mop mcp

Новый management MCP (#642) использует stdio SDK и authenticated HTTPS API
контроллера. Это не старый NATS/channel runtime и не клиент локальных сокетов.
Приватный inbox остаётся неподключённым (#649): запуск управления не является
приёмкой endpoint registration, native delivery или push в сессию.

## Контекст и запуск

Контекст выбирает вызывающий явно: `MOP_API_ORIGIN` (HTTPS), transient
`MOP_API_TOKEN` и необязательный `MOP_API_CA`; origin/CA также принимаются
общими prefix-флагами `--api-origin`/`--api-ca`. Токен не передаётся аргументом,
не пишется в файл и не выводится. `.env`, `connection.json`, git config,
окружение роли и чужие пользовательские сессии не являются источником authority.

```
mop mcp --project-id <UUID>
mop mcp --project-id <UUID> --node-id <UUID> --client-id <UUID> --check
```

Каждый scope selector — явный canonical UUID. Отсутствующая область даёт пустой
каталог действий, не все проекты установки; остаются только authenticated
identity/health diagnostics. Неизвестная/чужая область сужает каталог, а не
включает fallback. Комбинированные области требуют общего допуска.

`--check` запрашивает текущий `/api/v1/capabilities` и возвращает JSON:
версию каталога, epoch, факты Actor, scope и
`private_inbox: {available: false, reason: "not_bound", issue: 649}`.
Это отдельный процесс без живого адреса; он ничего не регистрирует.
`health` проверяет здоровье контроллера, а не доставку. `profile` читает только
факты identity с сервера; ни имя программы, ни kind не дают прав.

## Discovery и вызовы

`mop/cli/service/mcp.py` — тонкий entry point. `mop/cli/_mcp.py` обслуживает
stdio через native SDK; `mop/client/control.py` — единственный HTTPS transport.
На старте, при `tools/list` и перед каждым `tools/call` runtime читает текущий
scoped catalogue. Клиент принимает только v1, конечные canonical action IDs,
точную форму Actor/scope и совпадающий scope ответа. Неизвестный action/version,
неполный каталог и смена epoch/Actor не превращаются в разрешение. После смены
контекста требуется новый запуск. Сервер остаётся последней authority для
ресурса, текущих прав и бизнес-предпосылок; discovery — только advisory snapshot.

Инструмент появляется только при одновременном выполнении трёх условий:

1. поверхность помечена `bound` в `_control.SURFACES`;
2. сам командлет объявил `MCP` с canonical `actions`;
3. свежий серверный каталог содержит все требуемые действия.

Django human, ordinary/delegated agent и enrolled service используют одинаковые
действия. Kind проверяет допустимую форму Actor, но не выдаёт permissions.
Legacy service-with-grant отвергается. `human_only` остаётся лишь у реальных
бизнес-контрактов: создание с human owner и приватный presence query.

Имена — слова команды через подчёркивание (`node_drain`, `project_list`).
Описание, arguments и annotations читаются из собственного объявления командлета
через AST, без его импорта. Например:

```python
MCP = {"annotations": "readonly", "actions": ["catalog.view_pool"], "args": [
    {"name": "project_id", "type": "string", "flag": "--project-id"}]}
```

Schema запрещает неизвестные поля; типы проверяются до исполнения. Общий
`cli.tool_argv` преобразует их в argv; общий `_control.tool_call` вызывает тот же
`cli.main` и handler, что обычный CLI. Второй реализации pool operation нет.
Явные scope selectors нельзя расширить параметрами инструмента; project selector
подставляется там, где декларация его поддерживает. Остальные resource bindings
проверяет сервер. Epoch команды должен совпадать с текущим каталогом.

Stdout командлетов захватывается под process-wide lock и попадает только в
MCP result; протокольный stdout принадлежит SDK. Отказ возвращается через `loud`
с причиной, без сырых неожиданных исключений и credentials. Ошибка командлета —
`isError`, тогда как accepted/pending/unknown сохраняют свою честную operation
расписку: это не completed effect. Нет скрытого POST retry и фонового обещания
«итог придёт в сессию» без inbox.

## Чего нет

`agents`, `report`, idle notifications, `dispatch`, host-session scan,
private history/terminal и другие неподключённые surfaces не публикуются.
Нет fallback на старую шину, bundle capabilities, NATS identity или сокет сессии.
`send`, если разрешён, — существующий finite message commandlet по resource ID,
не произвольный локальный адрес и не обещание приватного обратного inbox.
Приватная группа `dev` остаётся вне MCP даже при наличии объявления.

## Проверки

`tests/modern_mcp.py` проверяет строгий codec, finite vocabulary parity,
canonical Actor filtering, отсутствие legacy imports, scope/epoch fencing,
редакцию ошибок и настоящий stdio initialize/list/call с собственным HTTPS
listener. `_modern_mcp_cases` в `mop dev controller` связывает native SDK stdio
с настоящими DRF Token/Access и PostgreSQL 17, включая отзыв прав между list/call.
Физическая доставка и регистрация inbox этими проверками не принимаются.
