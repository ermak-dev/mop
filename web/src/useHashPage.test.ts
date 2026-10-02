// Клиентская навигация (#422): страница журнала живёт на #journal, главная --
// на пустом хеше. Адресная строка остаётся источником правды: back/forward
// браузера переключают страницы без перезагрузки.
//
// HYPOTHESIS (#422): web.py отдаёт index.html только на "/", путь с любым
// суффиксом -- 404; потому маршрут -- в хеше, и первый же вход
// (обновление страницы на #journal) работает без сервера.
// RESULT: тесты написаны до реализации (RED).
import { act, renderHook } from "@testing-library/react";
import { afterEach, expect, test } from "vitest";
import { useHashPage } from "./useHashPage";

afterEach(() => { window.location.hash = ""; });

test("an empty hash is the main page, #journal is the journal", async () => {
  window.location.hash = "";
  const view = renderHook(() => useHashPage());
  expect(view.result.current.page).toBe("main");
  // hashchange в jsdom приходит asynchronously -- ждём его в act
  await act(async () => { view.result.current.go("journal"); });
  expect(view.result.current.page).toBe("journal");
  expect(window.location.hash).toBe("#journal");
});

test("opening the page on #journal starts on the journal", () => {
  window.location.hash = "#journal";
  expect(renderHook(() => useHashPage()).result.current.page).toBe("journal");
});

test("the browser back returns to the main page", () => {
  window.location.hash = "";
  const view = renderHook(() => useHashPage());
  act(() => view.result.current.go("journal"));
  act(() => { window.history.back(); });
  expect(view.result.current.page).toBe("main");
});

test("an unknown hash is the main page, not a white screen", () => {
  window.location.hash = "#no-such-page";
  expect(renderHook(() => useHashPage()).result.current.page).toBe("main");
});
