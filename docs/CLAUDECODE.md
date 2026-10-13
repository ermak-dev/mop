# Локальный протокол Claude Code

Исследование для подключения собственного мобильного GUI к живой сессии.
Тикет: #556. Дата исследования: 2026-10-10, повторная сверка: 2026-10-11.
Канал доставки пула, построенный на части этих интерфейсов, — [CHANNEL](CHANNEL.md).

Это описание внутренней реализации, а не обещанный Anthropic публичный API.
Проверена установленная Linux-сборка **2.1.296**. Не переносить выводы на другую
версию без повторной проверки обработчиков. Предыдущее исследование 2.1.294
не является основанием для утверждений о текущей сборке.

## 1. Главный результат

«Локальный сокет» — не один протокол. У Claude Code есть по меньшей мере
четыре разные поверхности:

| Поверхность | Адрес / канал | Вход | Выход |
|---|---|---|---|
| Messaging inbox | `messagingSocketPath` из реестра сессий | NDJSON: `auth`, `user`, `control` | Нет синхронного ответа; отдельные квитанции и idle notices на обратный inbox |
| Daemon control | `control.sock` | NDJSON с `proto: 1`, `op` | Ответы, подписка на состояние и терминальный поток; `attach` переключается на терминальные байты |
| Worker rendezvous | `rv/<short>.sock` | NDJSON: авторизация, `reply`, служебные команды | Heartbeat, изменения состояния, маркеры UI |
| Worker PTY | `pty/<short>.sock` | Двоичные кадры с данными терминала и JSON-control | Терминальные данные, hello/live/ping/exit |

Они **не являются JSON-RPC 2.0**. Поля `jsonrpc`, `method`, `params`, `id`
не образуют вызов метода на messaging inbox. Также нельзя послать туда
SDK-кадр `control_request` и получить поведение SDK-транспорта.

Важные поправки к первоначальной оценке возможностей:

* Обычный messaging inbox способен инициировать **прерывание текущего хода**:
  принятый в очередь `user` с `priority: "now"` вызывает abort в контроллере
  интерактивной сессии. Это прерывание с новым сообщением, не отдельный Stop RPC.
* Daemon `reply` имеет более широкую семантику, чем messaging `user`:
  может отвечать на открытый `AskUserQuestion`, выполнять некоторые команды
  немедленно либо доставлять человеческий ввод в очередь / PTY.
* Обычный messaging `user` намеренно превращается в peer-подсказку с
  `skipSlashCommands: true`, а не в произвольный ввод человека в UI.
* Idle notice иногда содержит короткий `detail` из последнего ответа. Это
  не полный ответ и не подписка на токеновый поток.
* `permission-response` в схеме daemon существует, но исследованный серверный
  обработчик только проверяет ключ и возвращает подтверждение. В нём нет
  передачи решения инструменту. Успешный ответ этой операции не доказывает
  согласование запроса разрешения.

## 2. Как получены сведения

### 2.1. Происхождение исходников

Бинарник:

```text
~/.local/share/claude/versions/2.1.296
sha256: 24972e3bc859fab2b46ed4c1e51f7d6130f06d3bd550811a114640de3370d0de
```

Прочитан встроенный JavaScript, находящийся рядом с Bun bytecode. Бинарник
не патчился; его код не запускался ради отправки сообщений. Извлечение
исходников не означает, что редактирование этих строк меняет исполняемый
байткод.

Извлечённые chunks и скрипт изолированных проверок жили во временном каталоге
машины исследования и в репозиторий не входят: это чужой код и проверки чужого
кода, а не проверки mop. Воспроизведение — извлечь встроенный JavaScript из
бинарника с тем же sha256 и найти места по именам ниже. Имена минифицированных
функций и номера извлечённых chunks не стабильны между релизами.

| Место в извлечённых исходниках | Что проверено |
|---|---|
| `chunk-1428.js`: `nn`, `Ie`, `Qe`, `be`, `Je`, `mn` | Парсер NDJSON, auth, session fence, пользовательский кадр, все control actions, запуск inbox |
| `chunk-1427.js`: `Y3n`, `rUt` | Состояние inbox и дедлайн первой строки |
| `chunk-127.js`: `MAs`, `HAs`, `$As`, `FAs` | Peer/child tokens и key-файлы |
| `chunk-296.js`: `vst`, `P4` | Форма `msg_id` и версия конверта |
| `chunk-297.js`: `P8t`, `Mio`, `O8t`, `sst` | Ограничения входа, дедупликация, peer PID |
| `chunk-1400.js`: `H`, `E`, `jUt`, `pht` | Accept/hold/refuse и соответствие режимов прав |
| `chunk-405.js`: `yT`, `QQr`, `JQr` | Добавление в очередь, ingress guards, hooks `session.receive` |
| `chunk-1614.js`: `TurnController._mount`, `_abortOnNowPriorityCommand`, `KCe`, `_Co`, `xCo` | Прерывание по `now`, ответ на вопрос, немедленные команды |
| `chunk-1426.js`: `hUr`, `yUr`, `_Ur`, `V5n`, `ee`, `E` | Idle subscription и краткий detail |
| `chunk-761.js`: `V`, `sWs`, `H4r` | Необязательные файловые вложения |
| `chunk-238.js`: `NAe`, `J2`, `JMn` | Каталог daemon и двоичный framing PTY |
| `chunk-383.js`: `ano` | Схема daemon control operations |
| `chunk-475.js`: switch по `w.op` | Реальное исполнение daemon operations |
| `chunk-482.js`: `reply`, `wirePty`, `connectRv` | Маршрутизация reply и происхождение stream из PTY |
| `chunk-1543.js`: `b.handleLine`, `J`, `cUt`, `U$r` | Worker rendezvous, resolver вопросов и runner команд |

### 2.2. Уровень проверки

* **Чтение исходников:** все описанные маршруты и ограничения.
* **Изолированные проверки обработчиков:** 20 проверок выбранных функций из
  исходников с подставленными зависимостями, в `node:vm`, без сокетов,
  сети и процессов. Все 20 прошли 2026-10-10 и повторно 2026-10-11 на той же
  сборке (sha256 выше).
* **Сверка чисел и ветвей по исходникам, 2026-10-11:** `permission-response`
  после проверки ключа отвечает `ok` без обращения к worker (`chunk-475.js`);
  немедленные RV-команды — множество `effort`, `model`, `rename`, `usage`
  за флагом `tengu_humming_locket` (`chunk-1614.js`); подписки — 32 записи,
  43 200 000 ms, debounce 750 ms (`chunk-1426.js`); ingress — `bucketCapacity`
  30, `refillPerSecond` 0.5, `dedupWindowMs` 30 000, `maxQueuedPeerMessages`
  50 (`chunk-297.js`); PTY — заголовок 5 байт, `readUInt32BE`, предел
  1 048 576 (`chunk-238.js`).
* В тестовом фрагменте `Qe` заменено только `using E=D.queueing` на
  `const E=D.queueing`: освобождение ресурса после hook не тестировалось.
  Криптографический компаратор, политика допуска, hooks и UI dependencies
  в проверках подставлены; их полное реальное поведение этим не доказано.
* **Живые сессии Claude Code не проверялись:** не было отправки сообщений,
  смены модели, разрешения инструмента, прерывания, запуска второго процесса,
  подключения к `rv` действующего worker или изменения состояния пула.
* Запросов к модели и проверок на платном LLM не было. Изолированный abort
  проверял настоящий `AbortController`, но не работающий model/tool turn.

Не называть эти проверки end-to-end испытанием Claude Code или пула.

## 3. Messaging inbox

### 3.1. Обнаружение и идентичность

Реестр по умолчанию — `~/.claude/sessions/*.json`; корень зависит от config home.
Полезные поля записи: `pid`, `cwd`, `sessionId`, `name`, `status`,
`statusUpdatedAt`, `messagingSocketPath`, необязательный `waitingFor`.

Файл может остаться после смерти процесса. Для выбора получателя нужны
действительно живой endpoint, PID и, где доступно, process-start identity.
Имя сессии не уникально. Каталог проекта тоже может иметь несколько записей.

В mop это уже реализовано в `mop/session.py`: `sessions`, `resolve`,
`live_session_for_cwd`, `peer_token`, `write_frames`.

Автоматический путь inbox на POSIX:

```text
<XDG_RUNTIME_DIR или runtime/temp root>/cc-socks/<pid>.sock
```

Fallback — `/tmp/cc-socks-<uid>/<pid>.sock` либо соответствующий tmp в Termux.
При конфликте endpoint автоматически выбранный путь может получить случайный
hex-суффикс. Поэтому брать путь из реестра, а не собирать его по PID.
Есть `--messaging-socket-path` для явного пути; существующий живой сокет по
такому пути не перехватывается. В исследованном коде проверяется предел
103 байта для автоматически формируемого пути.

`session_id` в кадре, если указан, должен совпасть с текущим `K()` получателя.
Несовпадение приводит к игнорированию кадра. Это полезный fence после
`/clear` или `/resume`: тот же процесс и сокет могут обслуживать другой разговор.

### 3.2. Соединение и framing

* `AF_UNIX`, `SOCK_STREAM` на POSIX; локальный named pipe на Windows.
* UTF-8; один JSON-объект на строку, разделитель `\n`.
* На одном соединении можно отправить несколько кадров.
* Парсер собирает фрагменты строки между `data` events.
* При EOF допускается последний законченный JSON без завершающего `\n`.
  Для переносимости клиенту всё равно следует завершать каждый кадр `\n`.
* Default deadline первой законченной строки — **30 000 ms**.
* Накопленный строковый буфер ограничен **1 048 576 JS string code units**.
  Это не точный предел числа UTF-8 bytes. Проверка размера идёт до разбиения
  очередного накопленного буфера на строки: слишком большой пакет со множеством
  небольших строк тоже может быть отвергнут.
* Нет обычного ACK/RPC-response на том же соединении. Обработчики могут только
  залогировать отказ. Закрытие соединения не доказывает доставку в модель.
* `write_frames` в mop отправляет кадры и закрывает соединение; квитанции
  принимаются отдельным inbox.

### 3.3. Авторизация

Первый кадр:

```json
{"type":"auth","token":"<peerToken получателя>"}
```

Каждый процесс генерирует разные `peerToken` и `childToken`: по 16 случайных
байт, в виде 32 hex-символов.

* `peerToken` публикуется в key-файле реестра с правами `0600`.
* Имя key-файла: `<pid>.<sha256(canonical-socket-path)>.key`.
* В нём также могут быть признаки process-start и PID domain.
* `childToken` передаётся дочерним процессам как
  `CLAUDE_CODE_MESSAGING_TOKEN`; его нет в опубликованном peer key-файле.
* `CLAUDE_CODE_MESSAGING_SOCKET` сообщает дочерним процессам адрес inbox.

Оба токена дают разные внутренние классы: `peer` и `child`. Они не делают
пользовательский кадр `origin.kind: human`. Проверка происхождения и политика
допуска существуют отдельно от проверки токена.

По умолчанию в этой сборке auth обязателен на Windows, а на POSIX опционален;
при запуске inbox код допускает явный `requireAuth`. На POSIX также важны
права сокета/каталогов и сведения OS о peer PID. Всегда отправлять подходящий
peer token, даже если текущая установка допускает неавторизованный вход.

Auth проверяется только в первой обработанной строке соединения. Нельзя
сначала послать user, а затем повысить доверие auth-кадром. В auth-required
режиме неправильный токен, отсутствие auth, пустая или неразбираемая первая
строка приводят к закрытию. В optional режиме ошибочный auth не обязательно
закрывает соединение, но не даёт authenticated-класса.

Не читать и не передавать чужие child tokens ради изменения класса отправителя.
Мобильный сервер должен использовать собственную пользовательскую авторизацию;
локальные ключи сессий не должны уходить в браузер.

### 3.4. Пользовательское сообщение

Рекомендуемый конверт:

```json
{
  "msgV": 1,
  "msg_id": "11111111-1111-4111-8111-111111111111",
  "type": "user",
  "session_id": "<текущий sessionId>",
  "from": "uds:/<каталог-сокетов>/<pid-отправителя>.sock",
  "message": {"role":"user","content":"Текст сообщения"},
  "priority": "next"
}
```

Это пример формы, не готовый кадр для отправки в чужую рабочую сессию.
`from` опционален для обычного ввода, но нужен для обратных квитанций.

| Поле | Поведение обработчика |
|---|---|
| `type` | Для user-ветки должно быть `user` |
| `message.content` | Непустая строка; массивы content blocks, null и пустая строка игнорируются |
| `message.role` | Обычный отправитель пишет `user`; данный обработчик поле не валидирует |
| `session_id` | Опциональный fence; при несовпадении кадр игнорируется |
| `priority` | `now`, `next`, `later`; иное или отсутствие превращается в `next` |
| `uuid` | Необязательная строка идентификатора записи; иначе UUID генерируется |
| `msg_id` | Для корреляции; `vst` принимает UUID с группами 8-4-4-4-12 hex |
| `msgV` | Отправители используют 1; основной user dispatcher не выполняет согласование версии по этому полю |
| `from` | Обратный адрес; при отсутствии origin получает `from: unknown` |
| `from_plugin` | Необязательная метка plugin после собственного валидатора; не превращает кадр в человеческий ввод |
| `file_attachments` | Опциональное feature-gated расширение, описано ниже |

Перед добавлением в очередь текст обрабатывается sanitizer, hook
`session.receive`, возможным материализатором файлов и политикой допуска.
Hook может изменить или поглотить сообщение.

Создаваемая запись очереди имеет следующие существенные поля:

```js
{
  mode: "prompt",
  agentId: /* основной agent текущего процесса */,
  value: /* обработанная строка */,
  priority: /* now / next / later */,
  origin: {kind: "peer", /* сведения о from и OS peer */},
  skipSlashCommands: true,
  isMeta: true,
  skipAttachments: true
}
```

Переданные клиентом `origin`, `mode`, `isMeta`, `skipSlashCommands` не
предоставляют управление этими полями. Поэтому `/model ...` в `content`
обычного user-кадра не является гарантированным выполнением команды `/model`:
по этому маршруту это prompt-текст, который может увидеть модель.
То же относится к `!command`: messaging user не запускает произвольный shell.

### 3.5. Приоритеты и прерывание

`now` не просто ускоряет доставку относительно других сообщений. Контроллер
интерактивного UI подписан на изменения очереди:

```js
messageQueue.subscribe(() => {
  this._syncMainLoopBusy();
  this._abortOnNowPriorityCommand();
});
```

Его обработчик проверяет наличие любого queued command с `priority === "now"`
и вызывает `abortController.abort(userAbortReason("interrupt"))`, если
текущий controller ещё не aborted.

Следовательно, **допущенный и реально добавленный в очередь** user-кадр с
`priority: now` способен прервать текущий ход и передать новую подсказку.
Оговорки:

* held/refused/dropped кадр в очередь не попадает и не даёт этого эффекта;
* auth, policy, hook и ingress guards стоят раньше;
* пустой `content` отвергается: нет документированного «пустого now = Stop»;
* abort — сигнал текущему turn; завершение/отмена конкретного инструмента
  зависит от его обработки abort, не равно немедленному уничтожению процесса;
* это не отдельный `interrupt` RPC и не гарантия остановки всех фоновых задач;
* проверенный участок относится к интерактивному TurnController. Нельзя
  автоматически обещать ту же последовательность в любом headless host.

`next` — default-приоритет очереди. `later` также принимается. Точную точку
включения их prompt в текущий/следующий turn определяет потребитель очереди.
Нельзя обещать «next всегда только после полного окончания текущего turn».
При освобождении held peer message код может убрать `later` и использовать
default-приоритет; клиенту не следует придавать приоритетам свойства durable
scheduler.

Для приоритетных user-кадров без файлов `Je` обходит сериализующую цепочку
обработки inbox. Это отдельное ускорение ingress; UI abort происходит уже
после допуска и enqueue.

### 3.6. Политика допуска, mode envelope и дедупликация

Авторизованное сообщение всё ещё может быть `held` или `refused`.
`crossSessionInbound` и managed/repo настройки участвуют в выборе
`accept`, `hold`, `refuse`. Выключенная cross-session messaging — отдельный отказ.

Если нет явной настройки, учитывается режим прав получателя и утверждение
режима отправителя. Неизвестный режим обрабатывается fail-closed как hold.
В bypass-режиме сообщение без достаточных сведений о соответствии режимов
может быть припарковано для решения человека. Mode emission feature flag
также влияет на сравнение.

Форма mode envelope, которую использует mop:

```text
<cross-session-message from="uds:/.../sender.sock" from-name="mobile" from-mode="prompting">
Текст сообщения
</cross-session-message>
```

Возможные утверждения режима — `prompting` и `bypass`. Называть свой режим
честно: не выбирать `bypass` только ради обхода ожидания. Envelope не является
командой смены permission mode получателя. Верхнеуровневое `from_mode`
используется в служебных кадрах; user-ветка получает mode metadata из
распознанного текстового envelope. Порядок атрибутов и экранирование имеют
значение для парсера, поэтому переиспользовать `mop/session.py:envelope`,
а не строить произвольный XML.

Дополнительно ingress guard проверяет повтор тела, token bucket, hop-chain
и заполнение очереди. Исследованные defaults, которые feature flags могут
изменить:

* bucket capacity 30, refill 0.5 сообщения/секунду;
* окно сравнения повторяющегося тела 30 000 ms;
* максимум 50 queued peer messages;
* ограничения на relay loops и длину цепочки пересылки.

Новый `msg_id` не отключает дедупликацию одинакового тела. Retry без понимания
квитанции может повторить действие или быть отвергнут; отсутствие ACK не
разрешает автоматически посылать тот же prompt снова.

### 3.7. Файловые вложения

Если соответствующий feature flag включён, `file_attachments` — массив:

```json
[
  {
    "path":"/<local-spool>/file-transfers/<transfer-copy>",
    "file_name":"diagram.png",
    "file_size":12345,
    "sha256":"<64 lowercase hex>",
    "media_type":"image/png"
  }
]
```

`file_size` обязателен и неотрицателен, `media_type` опционален. Получатель
проверяет локальный абсолютный путь, каталог spool `file-transfers`, regular
file, предел размера, размер и SHA-256. Затем создаёт private upload-копию и
добавляет `@"<upload-path>"` к prompt. Это не base64 image block в
`message.content` и не разрешение передать произвольный удалённый путь.
На несовпадении хеша/недоступности файла появляются сообщения о недоставке.
Расширение зависит от feature flag; его наличие в исходниках не доказывает
включённость на данном host.

### 3.8. Все control actions messaging inbox

Форма:

```json
{"msgV":1,"msg_id":"<uuid>","type":"control","action":"rename","name":"new name"}
```

Опциональный `session_id` проверяется перед action.

| Action | Существенные поля | Реальное назначение |
|---|---|---|
| `rename` | `name: string` | Вызывает зарегистрированный `onRename`; нет отдельного ACK |
| `notify_when_idle` | `from`, UUID `msg_id`, optional `from_mode` | Одноразовая подписка на idle/exited/unavailable |
| `peer_idle_notice` | `orig_msg_id`, `state`, optional `finished_at`, `detail`, `from`, `from_mode` | Входящая квитанция по ранее созданной подписке |
| `peer_message_status` | `orig_msg_id`, `status`; для dropped также `drop_reason`, `dropped_msg_ids` | Входящая квитанция о ранее отправленных сообщениях |
| `yield_artifact_replies` | Структурированный request с `from`, `msg_id` и полями artifact handoff | Передача ответов на комментарии артефактов другой живой сессии того же разговора |
| `unyield_artifact_replies` | Структурированный возврат handoff | Отмена/возврат передачи artifact replies |
| `artifact_replies_yielded` | `orig_msg_id` и outcome handoff | Подтверждение artifact replies handoff |

Последние три action — внутренний artifact handoff, а не универсальное
управление чатом. Для них используются отдельные схемы и проверка корреляции;
набор полей здесь намеренно не выдаётся за внешний стабильный контракт.

Других ветвей исполнения в исследованном `be` нет. В частности, этот inbox
не реализует `control.action: interrupt`, `set_model`, `permission_response`,
`get_history`, `subscribe`, `enable_remote_control`; наличие одноимённой
функции/сеттерa в другом модуле не добавляет ветку в wire dispatcher.
Неизвестный action только логируется. SDK `control_request` тоже не
обрабатывается этим маршрутом.

### 3.9. Квитанции и обратный inbox

Обычное сообщение не получает RPC-result на исходном соединении.
Квитанции приходят новым соединением на `from`, например:

```json
{
  "msgV":1,
  "msg_id":"<uuid ответа>",
  "type":"control",
  "action":"peer_message_status",
  "orig_msg_id":"<uuid исходного user>",
  "status":"held",
  "reason":"<описание>"
}
```

Поддерживаемые `status`: `held`, `denied`, `expired`, `delivered`, `refused`,
`dropped`. Для dropped причины: `rate-limited`, `duplicate`, `hop-loop`,
`hop-runaway`, `queue-full`; возможно перечисление нескольких dropped IDs.
Это не обещание обязательной отдельной квитанции на каждый нормальный
принятый prompt: обычный успешный enqueue может не дать такого подтверждения.

Обратный адрес ограничен локальным namespace. Inbox должен находиться в
допустимом каталоге сокетов с ожидаемой формой имени `<pid>.sock` или
`<pid>-<hex>.sock`; проверяются владельцы и, где доступны, peer PID/start.
Адрес `from` не даёт права писать в произвольный socket path.

Подписка:

```json
{
  "msgV":1,
  "msg_id":"<uuid подписки>",
  "type":"control",
  "action":"notify_when_idle",
  "from":"uds:/<тот же namespace>/<pid отправителя>.sock",
  "from_mode":"prompting"
}
```

Notice:

```json
{
  "type":"control",
  "action":"peer_idle_notice",
  "orig_msg_id":"<uuid подписки>",
  "state":"idle",
  "finished_at":"<optional строка времени>",
  "detail":"<optional короткая строка>"
}
```

Notice коррелируется с **подпиской**, а не с user-кадром. ID подписки
и сообщения должны быть разными. Соединение подписки не является каналом
долгоживущего streaming.

Defaults в этой сборке: 32 записи подписок, время жизни 12 часов,
idle debounce 750 ms. Число подписок отдельного requester тоже ограничено.
Held peer input и очередь могут задерживать idle notice. Само наличие
статуса idle не доказывает успешное окончание model turn: нужен также outcome.

`detail` может быть извлечён из последнего ответа/его status detail,
санитизирован и усечён примерно до 100 символов. Он отправляется только при
дополнительных проверках: закончившийся turn текущего разговора, проверенный
requester PID, совпадение обратного inbox с реестром requester и допуск mode
policy. Поэтому универсальному незарегистрированному GUI нельзя обещать
даже этот краткий текст. Полного ответа или token deltas notice не несёт.

## 4. Daemon control: более широкий интерфейс

### 4.1. Обнаружение и wire format

По умолчанию состояние daemon находится под `<config-home>/daemon/`.
На POSIX каталог сокетов:

```text
/tmp/cc-daemon-<uid>/<sha256(resolve(config-home))[0:8]>/
  control.sock
  rv/<short>.sock
  pty/<short>.sock
```

В Termux root tmp другой. На Windows используются pipes с nonce из `pipe.key`.
`short` — 8 hex-символов, не полный session UUID. Для внешнего обнаружения
предпочтительнее документированный `claude agents --json`, а не предположение
о внутреннем имени socket dir. Не запускать команды, которые попутно
поднимают/перезапускают daemon, ради незаметного чтения чужих сессий.

Запросы на `control.sock` — NDJSON, например:

```json
{"proto":1,"op":"list"}
```

Ответ list: `{"ok":true,"op":"list","jobs":[...]}`.
Операции записи используют daemon control key из
`<config-home>/daemon/control.key`. Это не peerToken messaging inbox.
На POSIX есть проверка uid подключающегося процесса. Нельзя переносить
доверие к локальному uid на неавторизованного пользователя веб-сервера.

### 4.2. Операции

Схема `ano` объявляет следующие operations:

| `op` | Назначение / параметры |
|---|---|
| `ping`, `nudge`, `yield` | Проверка и координация supervisor |
| `lease`, `leases` | Lifetime/клиенты supervisor |
| `await-ack` | Ожидание dispatch acknowledgement: short, nonce?, timeoutMs |
| `dispatch` | Запуск daemon worker: d, timeoutMs, auth |
| `list`, `has` | Список jobs / наличие short |
| `reply` | short, text, auth, optional nextTurn |
| `subscribe` | short, optional tail |
| `attach` | short, cols, rows, auth?, attachId?, caps?, holdingFrame? |
| `resize` | short, cols, rows, attachId? |
| `kill` | short, signal?, handoff?, evict?; это операция жизненного цикла, не Stop текущего turn |
| `ensure-spare` | Подготовка spare для cwd |
| `permission-response` | short, requestId, allow, auth; см. ограничение ниже |
| `respawn-stale` | Проверка/перезапуск устаревшего worker |
| `shutdown` | Остановка supervisor; reapWorkers? |

Операции жизненного цикла перечислены для полноты схемы, не как инструкция
применять их к рабочему пулу. Наличие схемы не доказывает эффект операции:
проверять ветку исполнения. Например, `permission-response` в 2.1.296 после
проверки auth возвращает `ok: true`, не обращаясь к worker, requestId или allow.
Это не рабочий механизм согласования инструментов.

### 4.3. `reply` — не то же самое, что messaging `user`

```json
{
  "proto":1,
  "op":"reply",
  "short":"<8 hex>",
  "text":"Текст или команда",
  "auth":"<daemon control key>"
}
```

Supervisor проверяет auth и состояние worker, затем вызывает `worker.reply`.

Маршрутизация `worker.reply`:

1. В queue-only режиме — отправка в worker RV.
2. Если worker ожидает/blocked, нет читаемого state или задан `nextTurn` —
   попытка RV.
3. В остальных подходящих случаях — bracketed paste в PTY и Enter.
4. Fallback — RV, если он доступен.

`ok: true, op: reply` подтверждает принятие выбранным маршрутом, а не
завершение команды, обновление модели или model response. Возможны
`EAUTH`, `ENOJOB`, `ENOREPLY`, `ERESPAWNING`, version-skew и другие отказы.
Повтор не идемпотентен: в схеме reply нет клиентского dedup request ID.

Внутри worker RV текст обрабатывается как ввод **человека**. Если
`nextTurn !== true`, порядок следующий:

1. resolver открытого вопроса;
2. runner немедленной команды;
3. обычная очередь prompt / bash с `origin: {kind: human}`.

Если `nextTurn: true`, первые две стадии намеренно пропускаются: в очередь
попадает prompt с `priority: next`, `skipSlashCommands: true`.
Поэтому `nextTurn: true` не использовать для ответа на вопрос или исполнения
`/model`.

### 4.4. Ответы на вопросы

В UI `KCe` регистрирует resolver для открытого диалога `AskUserQuestion`.
Resolver:

* отбрасывает пустую строку, `/...` и `!...`;
* берёт **первый вопрос** в текущем payload;
* находит option label без учёта регистра либо принимает введённый свободный текст;
* вызывает ответ на текущий dialog ID с `behavior: allow` и
  `updatedInput.answers[question] = выбранный текст`.

Это реальный маршрут ответа через локальный daemon reply. Но он не является
универсальным JSON-ответом на любые dialog kinds, все вопросы multi-question
формы сразу или произвольный `PermissionRequest`. Не подменять одобрение
вызова инструмента обычным текстом `yes`: такой route не доказан.

### 4.5. Команды

Исследованный runner немедленных RV-команд содержит allowlist:

```text
/effort
/model
/rename
/usage
```

Он дополнительно проверяет наличие/доступность команды, активность turn и
feature flag `tengu_humming_locket`. После распознавания запускает штатный
`runBridgeImmediateCommand`, не новую модель и не второй Claude process.
Если runner не потребил текст, RV передаёт его в обычную human-origin очередь,
где команда может обрабатываться по обычным правилам UI.

Это относится к RV/daemon reply. Переносить эти свойства на messaging user
нельзя: последний выставляет `skipSlashCommands: true`.

### 4.6. `subscribe` и `attach`

```json
{"proto":1,"op":"subscribe","short":"<8 hex>","tail":200}
```

Подписка даёт NDJSON events:

* `snapshot`: record и `streamTail`;
* `stream`: `line`;
* `state`: patch;
* `settled`: outcome, после которого соединение закрывается.

`stream.line` приходит из `wirePty` → `onData` → `pushRing` /
`onStream.emit`. Это терминальные данные с ANSI/control sequences,
**не** SDK `assistant` или `stream_event`. «line» не гарантирует границу
одной текстовой строки. Tail ограничен кольцевым буфером и не заменяет историю.

`attach` сначала отвечает JSON-line `ok/op/…`, затем на том же соединении
идёт терминальный поток и ввод. Это другой framing, чем отдельный PTY socket.
Изменение размеров влияет на отображение. Для mobile terminal следует
использовать terminal emulator, а не regex-удаление ANSI.

Attach работает с daemon workers, а не произвольным foreground Claude.
Legacy, stale, failed или restarting worker может потребовать respawn.
Поэтому attach не является безусловной гарантией «тот же процесс без рестарта».

## 5. Worker rendezvous и PTY

### 5.1. Rendezvous

Worker RV адрес передаётся при запуске через
`CLAUDE_BG_RENDEZVOUS_SOCK`. Авторизация — отдельный `rvAuth`, переданный
supervisor. Это NDJSON, а не PTY framing и не messaging auth-кадр.

Первый кадр авторизации содержит `role` и `auth`; исследованный обработчик
узнаёт его по наличию `role` и сверяет `auth` с RV token. `role` здесь не
роль сообщения модели.

После авторизации worker принимает:

| `type` | Поведение |
|---|---|
| `reply` + `text: string`, `nextTurn?` | Resolver вопроса, command runner или очередь |
| `repaint` | Просит перерисовать текущий терминальный UI |
| `attacher-caps` + caps | Терминальные возможности подключившегося клиента |
| `shutdown` | Завершение процесса worker; не обычный interrupt |

От worker идут `heartbeat` (30 s), `state`, `done`, `shutting-down`,
`detach-request`, `repaint-done`, `interactive-mark` и отказы авторизации.
Набор событий — не история разговора и не SDK stream.

**Не подключаться к RV напрямую при работающем supervisor:**
`onConnection` уничтожает предыдущий `current` socket. Чтение/подключение
такого клиента способно вытеснить supervisor. Внешнему GUI нужен control
supervisor, который уже владеет RV-соединением.

### 5.2. PTY framing

На отдельном worker PTY socket кадр имеет вид:

```text
uint32 big-endian payload_length
uint8  kind
bytes  payload[payload_length]
```

* kind `0`: terminal bytes;
* kind `1`: UTF-8 JSON-control;
* maximum payload — 1 048 576 bytes;
* header — 5 bytes, длина не включает header;
* неверный kind, слишком большой payload или неразбираемый JSON-control — ошибка decoder.

Control frames используют поле `t`, например `hello`, `live`, `ping`, `pong`,
`auth`, `resize`, `exit`. Ввод авторизуется отдельным `ptyAuth`. Ключи
`control.key`, `rvAuth`, `ptyAuth`, messaging `peerToken` и `childToken`
нельзя путать. Полноценный интернет-шлюз обязан сам проверять пользователя и
целевую сессию; внутренний PTY socket не предназначен для прямой публикации.

## 6. Сопоставление с SDK и `/rc`

SDK stdin/stdout `stream-json` использует другой конверт:

```json
{"type":"control_request","request_id":"<id>","request":{"subtype":"interrupt"}}
```

Control responses и запросы разрешений есть в SDK/bridge-протоколе, но
messaging inbox этот кадр не принимает. Текстовая похожесть JSON не делает
транспорты совместимыми. `/rc` связан с callback-интерфейсом живой сессии
и облачным транспортом; изучение локального inbox не даёт универсального
способа заменить `/rc` endpoint.

## 7. Что это означает для мобильного GUI

| Возможность | Messaging inbox | Daemon reply/attach |
|---|---|---|
| Новый prompt в живую сессию | Да, после допуска | Да |
| Прервать turn и дать новый prompt | `priority: now` в интерактивном host | Через терминальный ввод/штатный UI; нет доказанного отдельного Stop RPC control socket |
| Чистый Stop без нового prompt | Не найден в wire dispatcher | Terminal controls; не путать с kill/shutdown |
| Выполнить `/model` как команду | Нет, user route подавляет slash interpretation | Да, есть штатный runner/обычный human input, с условиями доступности |
| Ответить на `AskUserQuestion` | Специальный маршрут не найден | RV resolver первого вопроса |
| Одобрить произвольный tool permission | Не найден | `permission-response` не выполняет действие; можно использовать терминальный UI, если session attach допустим |
| Читать полный structured chat | Нет | subscribe отдаёт PTY, не structured chat |
| Получать краткий результат idle | Иногда optional detail | State/terminal stream |
| Любая foreground-сессия | Messaging inbox, если включён | Только daemon worker |

Для существующих foreground-сессий схема «JSONL-транскрипт + messaging inbox»
остаётся рабочей основой просмотра и prompt/now-перехвата, но не полного
воспроизведения UI. Для daemon-сессий есть более сильный локальный маршрут:
структурированная история отдельно, daemon reply для команд/вопросов,
terminal attach для остальных интерактивных действий.

Не создавать второй `claude --resume` как будто это attach к первому процессу.
Не выполнять скрытый перевод чужой foreground-сессии в background только
ради подключения GUI. Сохранять различие между состоянием доставки,
результатом turn и состоянием конкретного инструмента.

## 8. Проверки перед интеграцией

Живое подтверждение остаётся отдельной задачей на выделенной тестовой сессии,
не занятой работой пользователя. Не выдавать следующие пункты за уже выполненные:

* совпадение зарегистрированного socket/PID/session identity после `/clear`;
* реальные next/later/now во время модели и во время инструмента;
* mode hold/refuse, освобождение held input и dropped receipts;
* повтор тела при новом msg_id;
* ответ на первый/несколько вопросов и свободный текст;
* доступность `/model`/`/effort` в idle и busy worker;
* отсутствие эффекта daemon permission-response;
* reconnect, worker epoch/version skew, attach legacy и PTY resize;
* файловые attachments при включённом feature flag;
* видимые JSONL-записи и их задержка относительно терминального вывода.

Эти проверки не требуют вмешательства в рабочие папеты. Внешние HTTP/SSE
эндпоинты чата должны быть приватными, с авторизацией на каждом подключении
и проверкой Origin для записи; public `/events` mop не подходит для содержимого
разговора.

## 9. Существующие адаптеры mop

* `mop/session.py: sessions`, `live_session_for_cwd`, `resolve`, `peer_token` —
  discovery и идентичность endpoint.
* `mop/session.py: user_frame`, `control_frame`, `write_frames`, `Inbox` —
  кадры и отдельный обратный inbox.
* `mop/session.py: send`, `wait_idle` — адресная доставка и одноразовые notices.
* `docs/CHANNEL.md` — существующий канал доставки пула, не спецификация всех
  интерфейсов Claude Code.
* `docs/BUS.md` — доставка через NATS до узла; локальный IPC остаётся на узле.
* `docs/WEB.md` — существующий просмотр терминального экрана и admin send.

Модуль `mop/session.py` остаётся standalone/std-lib-only. Новый доступ к
локальному протоколу на узле не должен превращаться в передачу узлу
привилегий сервера или произвольного RPC от браузера.

### 9.1. Ограниченный reader транскрипта (#597, ещё не принят)

`session.py history <cwd> [--cursor CURSOR]` читает историю только текущей
доказанной живой сессии. Узловой verb `history` доступен только master в своём
проекте; принимает `name` и необязательный `cursor`, не путь/config home.
Содержимое не отправляется в публичные события, ошибки не отражают приватный
stdout/stderr. Это локальный файловый reader, не вызов `get_history` на inbox.

Успешный ответ имеет ровно поля `epoch`, `session_id`, `cursor`, `records`,
`reset`, `has_more`, `partial`. `records` сохраняет произвольные валидные
JSON-объекты, включая незнакомые типы: reader не интерпретирует инструменты.
Cursor ограничен 2048 символами и содержит byte-позицию и fences, не полномочия
или разрешённый путь. Смена идентичности/файла и нарушение byte fence начинают
новую эпоху с `reset`; смена во время чтения закрывает ответ отказом.

По явному уточнению контракта отображения завершённая битая JSONL-строка
(в том числе неверный UTF-8, дубли ключей или нечисловые числа) даёт ровно:

```json
{"type":"mop_notice","reason":"invalid_record","offset":123,"skipped_bytes":42,"complete":true}
```

`offset` — начальный byte offset, `skipped_bytes` — размер строки вместе с
newline. Исходник, traceback и текст исключения не выдаются. Cursor продвигается
к следующим завершённым записям; незаконченная обычная строка остаётся pending.
Для слишком длинной строки сохранён отдельный ограниченный chunk-контракт:
`mop_history_notice`, `reason: record_too_large`, byte offset, `skipped_bytes`
и `complete`; куски явно пропускаются, не выдаются за JSON-объекты исходника.
Чтение ограничено 64 KiB исходника на страницу, обычная запись — 32 KiB,
сериализованные records — менее 256 KiB с запасом под ответ.

До появления первого transcript доказанная живая сессия даёт пустой pending:
`records: []`, `partial: true`, `has_more: false`, стабильные epoch/cursor.
Появление файла начинает новую файловую эпоху. Недоступный config home,
небезопасный namespace, неверный cursor и ошибки выбранной живой идентичности
не превращаются в пустую страницу. Незаконченные, повреждённые и мёртвые
записи discovery не выбираются; несколько доказанных живых кандидатов — отказ.

Текущая реализация ограничена Linux (`/proc`, `SO_PEERCRED`) и отказывает при
symlink в пути config home/projects/transcript/key. Обычный project slug
заменяет каждый не-ASCII-alnum символ cwd дефисом; валидный override
`CLAUDE_CODE_PROJECT_DIR_NAME` учитывается только с `CLAUDE_CONFIG_DIR`.
Длинный slug свыше 200 символов **не поддержан**: Claude использует сокращение
с хешем, но reader не выдумывает алгоритм и не ищет соседние файлы. Приём
алгоритма и совпадения `procStart`/config home/socket identity реального
Claude — задача #596; до её принятия #597 нельзя считать готовой интеграцией.

Герметические проверки #597 используют синтетические registry/key/transcript,
настоящий UNIX listener своего процесса (проверен peer PID/UID/inode, ни одного
кадра) и собственный спящий дочерний Python-процесс (окружение/cwd/start ticks).
Это не проверка живого Claude Code, model turn или пула; платных вызовов нет.

## 10. Внешние справочные материалы

* [Claude Code: agent view и attach](https://code.claude.com/docs/en/agent-view)
* [Claude Code: sessions и расположение транскриптов](https://code.claude.com/docs/en/sessions)
* [Claude Code: Remote Control](https://code.claude.com/docs/en/remote-control)
* [Claude Agent SDK: streaming input](https://code.claude.com/docs/en/agent-sdk/streaming-vs-single-mode)

Документация описывает поддерживаемые пользовательские команды. Точные
локальные кадры и ограничения выше получены из конкретной сборки, а не
приписаны документации.
