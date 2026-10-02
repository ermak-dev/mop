// Страница журнала (#422): контракт события и его представление. at --
// уже серверная метка получения записи (Collector.event); #424 донесёт
// исходную метку агента и прочие безопасные поля -- сюда, optional, без
// выдумывания их на клиенте.
//
// HYPOTHESIS (#422): страница читает журнал отдельным admin-эндпоинтом
// (#424, GET /api/journal), а не из публичного снимка -- журнал виден
// только админу. Хелперы здесь не зависят от источника: та же запись, что
// отдаёт сервер. Статус: жду контракта #424 (STOP от мастера), тесты
// написаны до реализации (RED).
import { expect, test } from "vitest";
import type { JournalEntry } from "./types";
import { entryTitle, fullAt, journalText } from "./journalView";

const E: JournalEntry = {
  at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper",
  project: "mop", text: "from ermak",
};

// Дата «полностью»: и день, и время, не только чч:мм:сс главной страницы.
// 1700000000 -- 2023-11-14 22:13:20 UTC; пояс фиксирован, чтобы проверка
// не зависела от часового пояса того, кто её запускает.
test("fullAt shows date and time with seconds, dash without at", () => {
  expect(fullAt(E.at, "UTC")).toBe("14.11.2023 22:13:20");
  expect(fullAt(null)).toBe("-");
});

// Заголовок карточки: событие и папет, текст только в теле.
test("entryTitle joins event and puppet, dash without puppet", () => {
  expect(entryTitle(E)).toBe("send — pu-mop-1");
  expect(entryTitle({ ...E, name: "-" })).toBe("send");
});

// Осмысленное описание (#422): не сырой text, а фраза по виду события --
// событие без описания остаётся без фразы, неизвестные слова без выдумки.
test("journalText phrases the known kinds, falls back to text", () => {
  expect(journalText(E)).toBe("сообщение от ermak");
  expect(journalText({ ...E, event: "idle", text: "working" }))
    .toBe("дождался простоя: working");
  expect(journalText({ ...E, event: "wipe", text: "" })).toBe("клон снесён");
  expect(journalText({ ...E, event: "type", text: "mop dev test" }))
    .toBe("команда в клоне: mop dev test");
  // «up» агент публикует без папета: серверная нормализация даёт "-"
  expect(journalText({ ...E, event: "up", text: "agent on hyper", name: "-" }))
    .toBe("агент на узле hyper поднялся");
  expect(journalText({ ...E, event: "other", text: "что-то новое" }))
    .toBe("что-то новое");
});

// Фразы не должны зависеть от лишних полей: контракт #424 расширяет запись
// optional-полями, строки выше -- только те, что сервер отдаёт сегодня.
test("send without a name in text keeps the phrase whole", () => {
  expect(journalText({ ...E, text: "from mop" })).toBe("сообщение от mop");
});
