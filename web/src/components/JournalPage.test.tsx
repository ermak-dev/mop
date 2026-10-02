// Страница журнала (#422): полная таблица события шины для админа --
// дата/время целиком, событие, папет, узел, проект, описание; клик по
// строке или клавиатурой открывает доступный диалог с полной записью.
//
// HYPOTHESIS (#422): события приходят из useJournal (GET /api/journal,
// #424), новые сверху; диалог показывает все поля записи, какие сервер
// ни дал, -- без выдумывания своих.
// RESULT: тесты написаны до реализации (RED).
import { fireEvent, screen, waitFor } from "@testing-library/react";
import { expect, test, vi } from "vitest";
import { MantineProvider } from "@mantine/core";
import { renderUi } from "../test-utils";
import { JournalPage } from "./JournalPage";
import { JournalModal } from "./JournalModal";
import type { JournalEntry } from "../types";

const J: JournalEntry[] = [
  { at: 1700000060, event: "idle", name: "pu-mop-1", node: "hyper", project: "mop", text: "" },
  { at: 1700000000, event: "send", name: "pu-mop-1", node: "hyper", project: "mop",
    text: "from ermak", result: "доставлено" },   // безопасное поле #424: показывается, а не теряется
];

test("rows carry full date, event, puppet, node, project and the phrase", () => {
  renderUi(<JournalPage journal={J} open={() => {}} sourceKey="anton\0admin" />);
  const row = screen.getByText("14.11.2023 22:14:20 UTC").closest("tr")!;
  // дата «полная»: не чч:мм:сс главной, а день и время с секундами
  expect(row.textContent).toContain("idle");
  expect(row.textContent).toContain("pu-mop-1");
  // осмысленное описание: пустой text у idle -- фраза без хвоста
  expect(row.textContent).toContain("дождался простоя");
});

test("clicking a row opens the modal, keyboard Enter too", () => {
  const open = vi.fn();
  renderUi(<JournalPage journal={J} open={open} sourceKey="anton\0admin" />);
  // клик по строке: старейшая запись (новые сверху) -- с текстом from ermak
  fireEvent.click(screen.getByText(/сообщение от ermak/));
  expect(open).toHaveBeenCalledWith(J[1]);
  const row = screen.getByText("14.11.2023 22:14:20 UTC").closest("tr")!;
  fireEvent.keyDown(row, { key: "Enter" });
  expect(open).toHaveBeenCalledTimes(2);
});

test("keyboard: Enter on the focused row opens the modal too", () => {
  const open = vi.fn();
  renderUi(<JournalPage journal={J} open={open} sourceKey="anton\0admin" />);
  const row = screen.getAllByRole("row")[1];   // [0] -- заголовок
  fireEvent.keyDown(row, { key: "Enter" });
  expect(open).toHaveBeenCalledWith(J[1]);
});

test("an empty journal keeps the old note", () => {
  renderUi(<JournalPage journal={[]} open={() => {}} sourceKey="anton\0admin" />);
  expect(screen.getByText(/событий пока нет/)).toBeInTheDocument();
});

test("the refusal reason shows instead of a stale journal", () => {
  renderUi(<JournalPage journal={[]} error="журнал недоступен: прав не хватает" open={() => {}} sourceKey="anton\0admin" />);
  expect(screen.getByText(/прав не хватает/)).toBeInTheDocument();
  // прежних записей нет: только причина
  expect(screen.queryByRole("row")).toBeNull();
});

// Ревью #422: открытый диалог не переживает отказ источника. Журнал --
// только действующему админу: как только приходит error, диалог закрывается
// и таблица уходит -- ни одного кадра устаревших данных на экране.
test("review: an open modal closes the moment the source refuses", async () => {
  const view = renderUi(<JournalPage journal={J} error={null} open={() => {}} initial={J[1]} sourceKey="anton\0admin" />);
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  // источник отказал: 401/403/роль ушла -- hook положил error и пустой журнал
  view.rerender(<MantineProvider>
    <JournalPage journal={[]} error="журнал недоступен: прав не хватает" open={() => {}} sourceKey="anton\0admin" />
  </MantineProvider>);
  // диалог закрыт сразу, без ждущего клика; записей нет
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByText(/прав не хватает/)).toBeInTheDocument();
});

// Ревью 7 (#422): full_entry хранит произвольный JSON не только в text --
// объект в name/event/node/project ронял строку таблицы («Objects are not
// valid as a React child»). Ячейки таблицы и заголовок рисуются строками
// (объект -- его JSON); модал остаётся носителем сырого значения. Вид
// события, не являющийся строкой, фразу не заслужил -- колонка описания
// честно показывает сырой text.
test("review7: object name/event/node render as text, not a crash", () => {
  const odd = [
    { at: 1700000000, event: { kind: "send" }, name: { x: 1 }, node: [1, 2],
      project: "mop", text: "from ermak" },
  ] as unknown as JournalEntry[];
  renderUi(<JournalPage journal={odd} open={() => {}} sourceKey="anton\0admin" />);
  const row = screen.getByText("14.11.2023 22:13:20 UTC").closest("tr")!;
  expect(row.textContent).toContain('{"kind":"send"}');   // event -- строкой
  expect(row.textContent).toContain('{"x":1}');           // name -- строкой
  expect(row.textContent).toContain("mop");
  expect(row.textContent).toContain("from ermak");        // default: сырой text
});

// Ревью 4 (#422): обычный 10s опрос приносит новый массив с теми же
// событиями -- ссылка другая, источник тот же. Диалог полного события
// переживает такой кадр: закрыть его может смена источника или отказ,
// не тик опроса.
test("review4: an open modal survives a same-source poll frame", async () => {
  const view = renderUi(<JournalPage journal={J} error={null} open={() => {}} initial={J[1]}
                                     sourceKey="anton\0admin" />);
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  // очередной опрос: новый массив, то же содержимое, тот же источник
  view.rerender(<MantineProvider>
    <JournalPage journal={[...J]} error={null} open={() => {}} sourceKey="anton\0admin" />
  </MantineProvider>);
  await new Promise((r) => setTimeout(r, 50));
  expect(screen.getByRole("dialog")).toBeInTheDocument();
});

// Ревью 3 (#422): открытый диалог записи A не переживает смену источника
// A→B (error нет, журнал просто новый): диалог -- часть показанного
// журнала, чужой записи в нём быть не должно. Ключ источника -- от App
// (кто смотрит), не ссылка массива: опрос её меняет каждую 10 с.
test("review3: an open modal does not survive a source switch", async () => {
  const BJ: JournalEntry[] = [{ at: 1700000120, event: "wipe", name: "pu-mop-2", node: "gpu",
                                project: "other", text: "" }];
  const view = renderUi(<JournalPage journal={J} error={null} open={() => {}} initial={J[1]}
                                     sourceKey="anton\0admin" />);
  expect(await screen.findByRole("dialog")).toBeInTheDocument();
  view.rerender(<MantineProvider>
    <JournalPage journal={BJ} error={null} open={() => {}} sourceKey="bob\0admin" />
  </MantineProvider>);
  await waitFor(() => expect(screen.queryByRole("dialog")).toBeNull());
  expect(screen.getByText("pu-mop-2")).toBeInTheDocument();
});

// Ревью 2 (#422): модал показывает и сырое поле text -- отдельно от
// человекочитаемого описания: полное событие обязано нести запись как
// она есть, описание -- надстройка, не замена.
test("review2: the modal shows the raw text field besides the phrase", () => {
  renderUi(<JournalModal entry={J[1]} onClose={() => {}} />);
  const dlg = screen.getByRole("dialog");
  // сырая строка события агента -- как пришла
  expect(dlg.textContent).toContain("text:");
  expect(dlg.textContent).toContain("from ermak");
  // и человекочитаемая фраза рядом
  expect(dlg.textContent).toContain("сообщение от ermak");
});

// Блокер ревью (4): oversized-маркер #424 несёт только {event,text,at,
// received_at} -- name/node/project в записи нет. Диалог обязан рисовать
// такой маркер без crash'а, пустые поля показывая прочерком.
test("review: an oversized marker without name/node/project renders, not crashes", () => {
  const marker = { at: 1700000000, received_at: 1700000001, event: "type",
                   text: "[oversized: 64 KB]" } as JournalEntry;
  renderUi(<JournalModal entry={marker} onClose={() => {}} />);
  const dlg = screen.getByRole("dialog");
  expect(dlg.textContent).toContain("type");
  expect(dlg.textContent).toContain("[oversized: 64 KB]");
  expect(dlg.textContent).not.toContain("undefined");
});

// Блокер ревью (5): полное событие копируем в буфер одной кнопкой.
test("review: the modal copies the full entry JSON to the clipboard", async () => {
  const E: JournalEntry = { at: 1700000000, event: "send", name: "pu-mop-1",
                            node: "hyper", project: "mop", text: "from ermak" };
  const writeText = vi.fn().mockResolvedValue(undefined);
  vi.stubGlobal("navigator", { clipboard: { writeText } });
  renderUi(<JournalModal entry={E} onClose={() => {}} />);
  fireEvent.click(screen.getByRole("button", { name: /копировать/i }));
  await waitFor(() => expect(writeText).toHaveBeenCalledTimes(1));
  // запись целиком, читаемо отступами; проверяем содержимое, не формат
  const sent = writeText.mock.calls[0][0] as string;
  expect(JSON.parse(sent)).toEqual(E);
  vi.unstubAllGlobals();
});
