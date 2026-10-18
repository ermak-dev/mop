# Bootstrap песочницы

Что проект настраивает при **каждом старте** папета: `.mop/bootstrap.yaml`,
сыгранный сервером в песочницу до того, как откроется сессия claude (#62).
Вторая половина манифеста — `.mop/sandbox.yaml` — печётся в образ один раз
([DRIVER.md](DRIVER.md), #61).

Библиотека — `mop/server/bootstrap.py` (хранение и серверная половина), узловая
половина — `bootstrap_sandbox` в `mop driver run` (слои по машине,
`tests/layers.py`), командлет — `mop server bootstrap`, плейбук прогона —
`deploy/bootstrap.yml`, роль сервера — `deploy/roles/bootstrap`.

## Новая установка #623: граница Preparation (#636)

`mop.server.preparation` — отдельный ORM-владелец данных новой установки, не
fallback старого bootstrap. Описанная ниже файловая реализация относится только
к прежней версии; новый модуль её не читает и не импортирует.

| Инвариант | Владелец / путь изменения | Граница |
|---|---|---|
| Exact tasks/vars, provenance, commit, digest и ссылки секретов | `Preparation.publish` → immutable `BootstrapRevision` | Шифрование через публичный `RetainedSecrets.prepare` вне SQL; ciphertext и revision коммитятся вместе |
| Approval одобряет одну версию, не latest pointer | `approve`; отзыв — отдельный immutable `ApprovalRevocation` | Короткая локальная SQL-транзакция, текущий human action/project/owner; approved bytes не переписываются |
| Child наследует первый подтверждённый снимок | `approval_query().pin/validate` → `access.Grants` | Совместимый `ApprovedPolicy` (#635), exact approval/version/digest; retry валидирует старый proof, не выбирает новый latest |
| Результат адресован одному run/version/incarnation | `create_run`, `begin_step`, `progress`, `record_step`, `finish`, `readback` | `RunBinding` + expected version, row lock/CAS; provider, redactor и effect не входят в SQL |
| Неопределённый эффект не повторяется | `mark_unknown`, deadline readback | Unknown не failure и не success; повторное claim неизвестного run закрыто, reconciliation принадлежит executor |
| Приватные bytes не становятся публичным audit | `read_revision`/`payload`, `steps` | Повторная текущая authority и assigned-target gate, никакого generic serializer/Admin |

Содержимое хранится в AEAD `SecretVersion` (#637), а не в `FileField` и не в
прежнем каталоге. Задачи ограничены 256 KiB, vars — 128 KiB, ссылки — 32
защищённые версии project variables/files. После отзыва или смены active
secret version прежняя ссылка не выдаётся: требуется новая revision/approval,
а не молчаливая замена bytes. Provenance до 1024 символов, commit — hex object id; digest
покрывает весь канонический envelope, включая точные bytes и ссылки.
Материализация файлов исполнителем может быть только временной производной.

У run не более 64 конкретных шагов, не произвольный workflow DSL. Предел
провалов 1–10: подтверждённая ошибка задачи увеличивает счёт, недоступность,
отказ и unknown его не увеличивают; успех сбрасывает. Новый request_id не
обнуляет gave_up той же revision/incarnation и не обходит неизвестный run.
Новая одобренная revision или incarnation — новая область подготовки, но
safety внешнего эффекта по-прежнему проверяет execution gate.

Произвольный stdout не хранится по умолчанию. Для сохранения приватного tail
composition root обязан связать доверенный redactor, удаляющий реальные
значения credentials, включая секреты модулей Ansible. Его отсутствие —
отказ записи stdout, не passthrough. Один вход ограничен 64 KiB, tail шага —
4 KiB UTF-8, совокупный верхний предел logs run — 256 KiB; управляющие
последовательности вычищаются. Логи доступны только приватному readback,
не публичному журналу. Удаление evidence требует отдельной retention-политики;
произвольный delete/переписывание revision/approval запрещены.

### Явно оставшиеся подключения

Это data/approval/readback, не готовый запуск Ansible. `RuntimeGate.check`
должен быть локальным sync query с актуальным authenticated actor, assigned
node/target, project, полным role/digest/incarnation, pinned creation intent,
active grant/owner permissions и fencing. Незаданный gate закрыт. Он повторяется
при выдаче payload и readback после долгого прогона. Для `record` gate
аутентифицирует текущего exact receipt writer и назначение, но запись факта
уже выполненного эффекта не требует живого approval: при отзыве step outcome
сохраняется, run получает отказ с причиной, а неизвестный эффект остаётся
unknown. Это не допуск к новому эффекту или credential release. Сам `Operation` id не
даёт права получить bytes или credentials. Preparation возвращает ссылки
секретов, не их значения; адресная выдача остаётся публичным защищённым
`SecretStore.issue_addressed` с gate #637/#639.

- #625/#639 связывают эти use cases с admission/worker/execution claim,
  assigned-node query, reconciliation unknown, outbox и актуальной доставкой.
- #639 и назначенные задачи node/driver удаляют потребителей старого
  `server/bootstrap.py`, файловые readers/writers, NATS bootstrap RPC и
  подключают disposable materialization. В этой задаче они не переключены
  и не объявлены устранёнными; новый ORM-модуль их не вызывает.
- SSH private key bootstrap и Nomad token — отдельные защищённые credentials
  #637; эта схема не принимает их как vars или metadata.
- DRF/Admin, composition-root binding, live run на новой цели и итоговая
  проверка отзыва во время реального I/O — зависимые задачи и приёмка эпика,
  не доказательство локального ORM gate. Старый пул не используется.

## FreshNode: private transport export (#722)

Это отдельный bootstrap OS-потребителя, не `.mop/bootstrap.yaml` проекта и не
источник бизнес-политики. Исключение minimal bootstrap #624 остаётся конечным:
на узел не едут DB credentials, root crypto, Django key, Nomad token, proxy
management, человеческий login или снимок permissions/проектов/настроек.

Поддержанный серверный вход — ранняя maintenance-команда, до старого
context/`.env`/`node.env`; установка выбрана только защищённым
`MOP_CONTROLLER_BOOTSTRAP`. Нужны роль `maintenance`, актуальные schema,
installation identity/root и готовый restore guard. Узел — явный UUID текущей
строки Catalog в этой БД, не имя из inventory или старых файлов.

```sh
mop server controller enroll-node --node NODE_UUID \
  --origin https://controller.example.invalid \
  --ca-file /etc/mop-node/controller-ca.pem \
  --file /private/controller-exports/new-node/bootstrap.json \
  --node-file /var/lib/mop-node/private/bootstrap.json
```

`/private/controller-exports` уже существует, принадлежит maintenance uid и
имеет private mode. Каталог `new-node` обязан отсутствовать: команда создаёт
его как `0700`, bootstrap и `delivery.json` — как `0600`, независимо от umask.
Вся цепочка открывается через dirfd/no-follow; symlink, чужой/публичный parent,
существующий каталог/файл и коллизии отвергаются без перезаписи. Ни destination,
ни CA-файл на Node OS здесь не открываются. Успех молчит; raw credential бывает
только в private bootstrap и стандартной таблице DRF Token, не в argv, stdout,
repr результата или audit payload.

### Замороженный JSON-контракт Node OS v1

Bootstrap — UTF-8 JSON object с **ровно** этими ключами; неизвестные ключи,
дубликаты и иная версия должны отвергаться будущим Node parser:

| Ключ | Тип / значение |
|---|---|
| `schema_version` | JSON integer `1`, не boolean |
| `installation_id` | Nonsecret namespace текущей установки: `[A-Za-z0-9][A-Za-z0-9_.-]{0,63}` |
| `origin` | HTTPS origin без userinfo, path (также без завершающего `/`), query или fragment; явный порт допустим |
| `ca_file` | Канонический абсолютный путь к заранее доставленному CA на **Node OS**; не bytes сертификата и не controller path |
| `node_id` | Строка UUID конкретного текущего Catalog Node |
| `token` | Стандартный DRF Token: 40 lowercase hex; только `Authorization: Token …` по проверенному TLS |

Файл **не содержит** installation epoch, executor epoch, effects/readiness,
список actions/проектов, owner или grant. Внешнее хранение этих полей превратило
бы transport в authority. Технический начальный `NodeExecutor.executor_epoch`
создаётся в SQL до старта пустого узла; существующий epoch не сбрасывается.
`GET /api/v1/machine/executor?schema_version=1&node_id=…` отдаёт предыдущий epoch
только живому enrolled исполнителю. Он не является доказательством quiescence:
реальная physical reconciliation до `executor/restarted` и обычные execution
claims/fences обязательны; export не включает effects и не связывает physical
adapters.

### Enrollment, reexport и доставка

Создаётся один стандартный `Actor(kind='service')` с unusable password,
`ServiceEnrollment(use_case='node-executor', node=…)`, DRF Token и штатным
CredentialBinding. Права — фиксированные восемь canonical `execution.*`:
`poll_work`, `claim_work`, `report_result`, `read_facts`, `read_launch`,
`register_producer`, `ingest_telemetry`, `ingest_observations`. Это не весь
machine registry: нет catalog administration/receipts, глобальных proxy rights,
secret management или grant activation. Membership — ровно текущие не-retired
NodeProjects, через enabled local source; нет all-project scope, Groups,
LoginIdentity или human-owner impersonation.

`delivery.json` — отдельный private nonsecret receipt, не Node authority:
`schema_version: 1`, `node_id`, `enrollment_id`, `controller_file`, `node_file`,
`mode: "0600"`, `directory_mode: "0700"`, `no_log: true`. Он только описывает
источник/destination, не запускает Ansible, SSH, sudo, изменение OS account или
live writes. Внешняя явно разрешённая доставка обязана сама проверять владельца,
private destination/parent, переносить только этот файл и защищать copy через
`no_log: true`; CA доставляется отдельно. `delivery.json` не отправляется узлу.

Для повторного экспорта нужен новый каталог и `--enrollment UUID` из receipt.
Issuer проверяет **тот же exact live enrollment**, узел/use case, Actor flags,
полный точный набор permissions/membership и живой token/binding. Отзыв, другой
UUID/узел, collision username (в том числе человеческого), изменённые права,
проекты, purpose или отсутствие epoch — отказ, не repair. Нельзя добавить
права/проекты, ротировать token, усыновить username или породить второй Actor.
При обычном I/O/SQL отказе новый bundle удаляется, SQL откатывается. Это не
распределённая транзакция DB/filesystem при аварийном завершении процесса:
оставшийся файл/каталог сам по себе ничего не разрешает, а повторная команда
его не перезаписывает и не принимает за authority.

### Оставшиеся Node bindings и проверка

#722 реализует только server-layer issuer/export. Private-file parser,
consumer entry/daemon, проверенный TLS transport с Node CA, discovery текущего
installation epoch через живую authority, native physical runtime/reconciliation,
producers/telemetry и доставка в OS — отдельные Node/composition задачи.
Никакой fallback к старым bus.json/ready.json/node.env не добавлен. Если будущий
consumer выбирает файл через node.env/systemd, там может быть только nonsecret
путь (новая переменная node.env должна быть объявлена NodeScoped), а не token,
копия controller bootstrap/.env или policy. Новых global required settings нет.

`tests/node_bootstrap_export.py` проверяет private paths/modes, no-overwrite,
redaction и early CLI. `tests/node_bootstrap.py` на настоящем PostgreSQL 17
проверяет issue/reexport/revocation, текущую identity, точные purpose/permissions,
project/node denial и initial epoch discovery. У HTTP poll в проверке привязан
пустой test work port; положительный facts query использует bounded test reader.
Production physical ports этим не объявляются связанными: unbound facts всё
ещё возвращает unavailable. Живой Node/пул/rollout здесь не проверяются.

## Зачем

До #62 манифест при подъёме не играл никто: единственным механизмом было
копирование env-файлов врапером по `PU_SEED`, и оно глотало отказ (`|| true`).
Решение оператора 22.09 — играть сервером при каждом старте, ценой времени
холодного старта ради гибкости настройки окружения.

## Как это устроено

```
узел (mop driver run)                   сервер (mop server bootstrap serve)
  ensure тело
  клон проекта в теле (#247)
  admit: ключ сервера в тело  ───────►
  request mop.<проект>.server.rpc  ────► bootstrap {name, address, alloc, started_at}
        ждёт                           ansible-playbook deploy/bootstrap.yml
                                         -i <адрес>, ключом сервера
  ◄──────────────────────────────────  {ok, seconds, tail}
  admit: ключ сервера вон
  внутренний врапер, tmux
```

Для «Экрана» до tmux (#612) сервер сохраняет текущий вывод Ansible в
`<папет>-progress.log` (0600) и состояние в `-progress.json`, с идентификатором
аллокации и отметкой запроса по часам узла (как событие Nomad `Started`,
не по часам контроллера). Файл обновляется самим процессом во время
прогона, не только после его завершения. Приватный read-only глагол
`startup` возвращает ограниченную проекцию: заголовки задач и их статусы,
не command/stdout/JSON результатов. Предыдущая аллокация или попытка не
выдаётся за текущий bootstrap. Вместе с ограниченными стадиями внешней
обёртки это читает `mop tail <имя> --startup` и admin-only `/tail` дашборда.
В публичном ростере и SSE этих логов нет; появившийся tmux имеет приоритет.
Удаление workspace удаляет и оба файла прогресса. Credential-пары и Bearer
в именах задач редактируются тем же механизмом, что приватный экран;
произвольные значения модулей и диагностический stderr в UI не копируются.

Четыре решения, каждое названо:

* **ansible на сервере.** Ставит роль `bootstrap`. Сервер — машина, которая
  включена всегда, и у неё есть дорога к телам (`mop-routes`, #59); мастера в
  момент рестарта аллокации может не быть вовсе.
* **Ключ в тело — на время.** У сервера свой ключ `~/.ssh/mop-bootstrap`; в
  образе его нет. Узел впускает его в тело глаголом `admit` (обёртка `keys`:
  ключ узла плюс ключ сервера) и выпускает после ответа — в любом исходе.
  Постоянный ключ был бы второй дорогой к телу мимо агента, то есть мимо
  единственной проверки проектирования. На узле, где тело равно узлу, ключ лежит
  в `authorized_keys` пользователя пула постоянно: сервер-контроллер туда и так
  ходит.
* **Файл лежит на сервере заранее, у каждого папета свой** —
  `~/.config/mop/bootstrap/<папет>-tasks.yml` и `-vars.yml` (#133). `sandbox.yaml`
  — проекта (он в образе), workspace (`.mop/bootstrap.yaml`) — папета, и каждый
  отвечает за своё. workspace кладут только глаголы жизненного цикла папета:
  `mop add`, `mop update`, `mop recycle` шлют его сервису кластера вместе с
  регистрацией — из рабочей копии, откуда их зовут (вне её — из origin), и
  отсутствие файла снимает копию; `mop delete` уносит её вместе с папетом.
  Рецикл без рабочей копии (`mop server gc`) оставляет положенное. `mop project add` и
  `mop server deploy` workspace не трогают; глагола `put` больше нет — в субъект
  сервера пишут и узлы. Из клона папета файл не едет никогда — на host-узле это
  было бы исполнение с рабочей ветки в общем теле.
* **Что уехало и чем кончилось — рядом с файлом** (#334). С workspace клиент шлёт
  его происхождение (`bootstrap_sent`: рабочая копия или origin, коммит, правлен
  ли сам файл, число задач); сервис кластера кладёт его в `<папет>-sent.json` с
  меткой регистрации по часам сервера и отдаёт метку в ответе `add`/`update`
  (`bootstrap_marker`; у `update` ещё `replaced` — спека без изменений Nomad не
  переразмещает, и прогона не будет). После каждого прогона сервер пишет
  `<папет>-result.json`: `{ok, played, seconds, rc, task, message, last, at,
  sent, commit}`; глагол `bootstrap_result` сервиса кластера отдаёт его, и
  `mop add|update` ждут итог своей метки. Старый клиент без поля — ни файла, ни
  новых полей ответа.
* **Сдаться после трёх провалов** (#345). Политика рестарта папета безгранична
  намеренно — он переживает лежащий сервер. Провал же прогона детерминирован,
  поэтому сервер считает подряд идущие провалы одного ключа (метка регистрации
  плюс sha хранимых файлов) в `<папет>-result.json`: считается только прогон,
  дошедший до конца с упавшей задачей (ansible rc 2 и названная задача, #333);
  недоступный хост, таймаут и отказы до и после прогона — нет. На третьем ответ
  несёт `gave_up`, и через `GIVE_UP_GRACE` после ответа (узел успел закрыть дверь
  в тело) mop-bootstrap просит глагол `give_up` сервиса кластера остановить джоб
  (purge false: клон и тело на месте). Следующий запрос того же ключа — `gave_up`
  без прогона и новая просьба. Ростер: `FAILED: bootstrap gave up after 3
  attempts at task «…»: … — fix .mop/bootstrap.yaml, then mop update`;
  `mop update`/`mop recycle` дают новую метку и начинают счёт заново.
* **Кред папета едет ответом.** В ответе на `bootstrap` сервер отдаёт узлу
  `{url, user, password}` пользователя `puppet-<проект>` (#83), и внешний
  врапер кладёт его в тело по пути, который ждёт внутренний. Так узел
  перестаёт хранить кред проекта, у которого на нём сейчас никто не живёт:
  файл появляется на время жизни папета и только для его проекта. Серверу
  это власти не добавляет — на шину он и так ходит под `service` со всем
`mop.>`. Пароли ему
  везёт роль сервера из `secrets/` контроллера, в отдельный каталог
  `~/.config/mop/puppets`: в `servers/<адрес>/` их класть нельзя, оттуда их
  увёз бы первый же `mop join`.
* **Клон стоит до прогона (#247).** `mop driver run` клонирует проект в
  тело сам, между `ensure` и вызовом сервера, тем же сниппетом, что и
  внутренний врапер (`driver.CLONE_SH`, одно определение на оба места).
  Поэтому `mop_clone` — путь клона, который уже есть, а не «где он появится»:
  задачи манифеста вправе делать в нём `chdir`. Пока клонировал врапер после
  ответа сервера, первый старт контейнерного тела играл bootstrap в пустую
  песочницу, и проектам приходилось повторять клон у себя.
* **Триггер — узел.** `mop driver run` после `ensure` и клона зовёт сервер и
  ждёт до `bootstrap.TIMEOUT`. Нет файла для проекта — сервер отвечает сразу, без
  прогона. Отказ — это отказ: врапер выходит ненулём, Nomad перезапускает,
  ростер показывает падающую аллокацию. Папет не поднимается «наполовину».
  Семя врапера тоже перестало глотать отказ: `cp`, который не смог, роняет
  подъём.

## Субъект и права

`mop.<проект>.server.rpc` — сервер как адресат; подписчик слушает `mop.*.server.rpc`
под машинным пользователем `service` (#104, тот же каталог кредов, что у
дашборда). Пишут туда узлы (`node-<узел>`, глагол `bootstrap`);
папетам субъект не выдан. Проект берётся из субъекта, имя папета — из тела запроса,
и их расхождение — отказ.

Прогон идёт под пользователем пула, без вопроса о ключе хоста
(`StrictHostKeyChecking=no`): тело пересоздаётся и приезжает с новым, а
известного заранее у сервера нет. Цена названа — сеть тел локальная, за NAT
узла, дорога в неё только с сервера.

## Что писать в bootstrap.yaml

Одна игра, как у `sandbox.yaml`: `vars` — конфигурация своим задачам, `tasks` —
задачи. Переменные прогона: `mop_project`, `mop_puppet`, `mop_clone` (путь клона
папета в песочнице), плюс настройки установки (`MOP_HOME`, `MOP_USER`). Задачи
идут от root; что трогает дом пользователя пула, называет его явно
(`become_user`). Пути `src` — на **сервере**: там лежит рабочая копия
контроллера и его секреты.

Просьбы о размерах (`MOP_MEM_MB` и прочее из `PROJECT_SCOPED`) здесь не на
месте — они дело `sandbox.yaml` — и отбрасываются с именем.

**Потолок времени.** Bootstrap идёт и при лечении залипшего папета
(`doctor --fix`, `restart`). Вырастет до минут — лечение станет дороже
болезни. Держать в секундах; тяжёлое — в `sandbox.yaml`, оно печётся один раз.

## Проверка

* `mop server bootstrap check` — отвечает ли сервер и для каких проектов держит файл.
* `journalctl -u mop-bootstrap` на сервере — каждый прогон строкой, отказ — с
  хвостом вывода ansible.
* Отказ bootstrap'а виден как падающая аллокация в `mop list`; причина — в
  логе задачи на узле (`bootstrap of <имя> failed: …`).

## Что останется непроверенным

Чистыми функциями проверяются хранение файла, аргументы прогона и проверка
проекта (`tests/bootstrap.py`). Сам прогон, ключ в тело и ожидание врапера —
только на живом пуле.

## Секреты проекта

Файлы и переменные, которые проект не держит в репозитории (`.env`, ключи
провайдеров), мастер кладёт из рабочей копии на сервер:
`mop secret file|var add|list|remove` (#127). Сервер хранит их в
`~/.config/mop/project-secrets/<проект>/` — `files/` по путям от корня клона
и `vars.env` строками `KEY=VALUE`, 0700/0600, и правит их сервис кластера на
субъекте проекта: мастеру своего проекта и оператору, узлам — нет.

При каждом старте bootstrap (#128) сносит в теле тот же каталог и кладёт
серверный заново — новое появляется, удалённое исчезает — до задач проекта,
которые на него могут опираться. Рамка играется, если у проекта есть задачи
ИЛИ секреты. Путь едет прогону переменной, значения — только файлами: argv
прогона виден в `ps`. Дальше врапер кладёт файлы в клон и переменные в сессию
(#129).

Файл на машине, играющей bootstrap (`src: ~/проект/.env` в задачах
проекта), с тех пор как её играет сервер, туда не попадает: для этого и
заведены секреты.
