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
  expect(fullAt(E.at, "UTC")).toBe("14.11.2023 22:13:20 UTC");
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

// Ревью 6 (#422): text -- произвольный JSON, каким его сохранил сервер
// (full_entry): числа, отсутствие поля. Фраза обязана строиться без
// краха, сырое значение -- не теряться (его показывает модал как есть).
test("review6: a non-string or missing text phrases safely", () => {
  const num = { ...E, text: 42 } as unknown as typeof E;
  expect(journalText(num)).toBe("сообщение от 42");
  const noText = { at: E.at, event: "send", name: "p", node: "n", project: "x" } as typeof E;
  expect(journalText(noText)).toBe("сообщение");
  const upNum = { ...E, event: "up", text: 7 } as unknown as typeof E;
  expect(() => journalText(upNum)).not.toThrow();
  expect(journalText(upNum)).toBe("агент поднялся");
});

// Ревью 2 (#422): полная запись -- с поясом. Критерий тикета: дата/время с
// часовым поясом; пояс смотрящего меняет запись, и без «UTC»/«+03» строка
// о поясе молчит.
test("review2: fullAt ends with the timezone offset", () => {
  expect(fullAt(E.at, "UTC")).toBe("14.11.2023 22:13:20 UTC");
  const msk = fullAt(E.at, "Europe/Moscow");
  // вид смещения зависит от ICU (GMT+3 или +03:00) -- дата и число зоны
  expect(msk).toMatch(/(GMT\+3|\+03:00)$/);
  expect(msk).toContain("15.11.2023");
});

// Пояс прилагается и без фиксированного пояса в аргументе (пояс браузера):
// строка всегда заканчивается смещением.
test("review2: fullAt shows the browser timezone offset too", () => {
  // в среде проверок зона фиксирована UTC (setupTests) -- запись всегда
  // несёт смещение, и проверка не зависит от машины
  expect(fullAt(E.at)).toBe("14.11.2023 22:13:20 UTC");
  expect(fullAt(null)).toBe("-");
});

// Ревью 3 (#422): безумная метка не ломает страницу. Бэкенд допускает
// конечные значения вроде 1e20, а Date(1e23 мс) вне диапазона -- RangeError
// из formatToParts ронял бы весь журнал; такая метка -- прочерк.
test("review3: an out-of-range timestamp renders a dash, not a crash", () => {
  expect(fullAt(1e20, "UTC")).toBe("-");
  expect(fullAt(1e20)).toBe("-");
  expect(Number.isFinite(1e20)).toBe(true);   // метка конечна -- тест про Date, не про NaN
});
