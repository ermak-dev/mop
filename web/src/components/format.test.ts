// Запись чисел и корзин как на старой странице (#298).
import { gb, gbOfMb, hhmmss, human, plural, ratio, untilText } from "./format";

test("gb: kilobytes to GB or MB, dash without data", () => {
  expect(gb(null)).toBe("-");
  expect(gb(512)).toBe("1 MB");
  expect(gb(3 * 1048576)).toBe("3 GB");
  expect(gbOfMb(null)).toBe("-");
  expect(gbOfMb(2048)).toBe("2 GB");
});

test("human: short token counts", () => {
  expect(human(999)).toBe("999");
  expect(human(1200)).toBe("1.2k");
  expect(human(15_000_000)).toBe("15M");
  expect(human(2_300_000_000)).toBe("2.3G");
});

test("ratio follows the old page", () => {
  expect(ratio(null, null)).toBe("-");
  expect(ratio(2, 7)).toBe("2/7");
  expect(ratio(null, 7)).toBe("-/7");
  // те же случаи, что CASES в tests/render.py (#243)
  expect(ratio(0, 5)).toBe("0/5");
  expect(ratio(5, 5)).toBe("5/5");
  expect(ratio(0, 0)).toBe("0/0");
  expect(ratio(2, null)).toBe("2/-");
  expect(hhmmss(null)).toBe("-");
});

// Перенесено из usage-format (#299) и App (#297) вместе с помощниками (#323).
test("human numbers read like the old page", () => {
  expect(human(171_000_000)).toBe("171M");
  expect(human(2_900)).toBe("2.9k");
  expect(human(836_000_000)).toBe("836M");
  expect(human(1_200_000_000)).toBe("1.2G");
  expect(human(0)).toBe("0");
});

test("plural", () => {
  expect(plural(1, "папет", "папета", "папетов")).toBe("1 папет");
  expect(plural(3, "папет", "папета", "папетов")).toBe("3 папета");
  expect(plural(11, "папет", "папета", "папетов")).toBe("11 папетов");
  expect(plural(22, "папет", "папета", "папетов")).toBe("22 папета");
});

test("days decline in Russian", () => {
  const days = (n: number) => plural(n, "день", "дня", "дней");
  expect(days(1)).toBe("1 день");
  expect(days(3)).toBe("3 дня");
  expect(days(14)).toBe("14 дней");
  expect(days(21)).toBe("21 день");
});

// Вынесены из компонентов и creds-api (#323): поведение прежнее.

// Время сброса квоты (#331): в поясе смотрящего, «до чч:мм» сегодня и
// «до дд.мм чч:мм» в другой день. Пояс и «сейчас» закреплены: проверка не
// зависит от часов и пояса машины, где её гоняют.
const at = (y: number, mo: number, d: number, h: number, mi: number) => Date.UTC(y, mo - 1, d, h, mi) / 1000;
const NOW = at(2026, 9, 27, 10, 0);             // 27.09 13:00 по Москве

test("#331 the reset time reads in the viewer's zone, with a date when not today", () => {
  const msk = "Europe/Moscow";
  expect(untilText(at(2026, 9, 27, 14, 30), NOW, msk)).toBe("до 17:30");
  expect(untilText(at(2026, 9, 30, 2, 0), NOW, msk)).toBe("до 30.09 05:00");
  expect(untilText(at(2026, 9, 27, 21, 5), NOW, msk)).toBe("до 28.09 00:05");  // уже завтра по Москве
  expect(untilText(at(2026, 9, 27, 21, 5), NOW, "UTC")).toBe("до 21:05");       // а по UTC ещё сегодня
  expect(untilText(null, NOW, msk)).toBe("");
});

// #326: виды -- из снимка (#325), а не из слов. Цвет бейджа -- по
// status_kind, время сброса -- только к quota_wait. Снимок сервера до #325
// видов не несёт: бейдж серый, слово как есть, без «до …».


// Свободная память узла (#329): вниз, как `mop node` и строка пула. Узел с
// 1536 МБ -- «1 GB»: папет резервирует 8 ГБ, и показать места больше, чем
// есть, значит обещать то, чего планировщик не даст.
test("#329 gbOfMb floors: never more GB than there are", () => {
  expect(gbOfMb(1536)).toBe("1 GB");
  expect(gbOfMb(2047)).toBe("1 GB");
  expect(gbOfMb(2048)).toBe("2 GB");
  expect(gbOfMb(64511)).toBe("62 GB");
  expect(gbOfMb(null)).toBe("-");
});
