import "@testing-library/jest-dom/vitest";
import { beforeEach } from 'vitest';

// Session fixture включает CSRF cookie; проверки отказа удаляют его явно.
beforeEach(() => { document.cookie = 'csrftoken=fixtureSessionCsrfToken0123456789; path=/'; });

// Часовой пояс проверок фиксирован (#422, ревью 6): ICU даёт смещению
// разный вид по средам (GMT+3 в node, +03:00 в браузере), а «пояс
// браузера» -- и вовсе зависит от машины, запустившей тесты. Одна зона
// для всех проверок -- UTC (подмена Intl.DateTimeFormat ниже).
// eslint-disable-next-line @typescript-eslint/no-explicit-any
(globalThis as any).process = { ...(globalThis as any).process, env: { ...((globalThis as any).process?.env ?? {}), TZ: "UTC" } };

// Autosize у Mantine слушает загрузку шрифтов; FontFaceSet в jsdom нет.
if (!document.fonts) Object.defineProperty(document, "fonts", { value: new EventTarget() });

// Mantine в jsdom: matchMedia и ResizeObserver у jsdom нет.
window.matchMedia = window.matchMedia || ((query: string) => ({
  matches: false, media: query, onchange: null,
  addListener: () => {}, removeListener: () => {},
  addEventListener: () => {}, removeEventListener: () => {}, dispatchEvent: () => false,
} as MediaQueryList));
(globalThis as unknown as { ResizeObserver: unknown }).ResizeObserver =
  (globalThis as unknown as { ResizeObserver?: unknown }).ResizeObserver
  || class { observe() {} unobserve() {} disconnect() {} };

// Приложение тоже в UTC внутри проверок (#422): страница в браузере
// остаётся в поясе смотрящего (fullAt без аргумента), а jsdom-среда
// тестов фиксирует зону. Подменяется один конструктор, остальной Intl
// (NumberFormat и прочие) -- как есть: спред по Intl терял классы и
// ронял Mantine.
try {
  const realDateTimeFormat = Intl.DateTimeFormat;
  class FixedDateTimeFormat extends realDateTimeFormat {
    constructor(...args: ConstructorParameters<typeof Intl.DateTimeFormat>) {
      const opts = args[1];
      super(...(opts && !opts.timeZone
        ? [args[0], { ...opts, timeZone: "UTC" }] as [string, Intl.DateTimeFormatOptions]
        : args));
    }
  }
  Object.defineProperty(Intl, "DateTimeFormat", {
    value: FixedDateTimeFormat,
    writable: true, configurable: true,
  });
} catch {
  // среда без Intl -- тесты даты всё равно задают пояс явно
}
