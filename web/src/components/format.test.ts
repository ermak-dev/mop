// Запись чисел и корзин как на старой странице (#298).
import { gb, gbOfMb, hhmmss, human, listOrDash, nodeKind, percentText, plural, presentKinds, ratio, statusColor } from "./format";

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

test("ratio and node kind follow the old page", () => {
  expect(ratio(null, null)).toBe("-");
  expect(ratio(2, 7)).toBe("2/7");
  expect(ratio(null, 7)).toBe("-/7");
  // те же случаи, что CASES в tests/render.py (#243)
  expect(ratio(0, 5)).toBe("0/5");
  expect(ratio(5, 5)).toBe("5/5");
  expect(ratio(0, 0)).toBe("0/0");
  expect(ratio(2, null)).toBe("2/-");
  expect(nodeKind("ready")).toBe("free");
  expect(nodeKind("ineligible draining")).toBe("busy");
  expect(nodeKind("down")).toBe("down");
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
test("present kinds, list or dash, credential status and percent", () => {
  expect(presentKinds({ puppets: 3, free: 1, busy: 0, sick: 2, silent: 0, down: 0 })).toEqual(["free", "sick"]);
  expect(listOrDash([])).toBe("-");
  expect(listOrDash(["pu-mop-1", "pu-mop-3"])).toBe("pu-mop-1, pu-mop-3");
  expect(statusColor("активен")).toBe("green");
  expect(statusColor("ждёт квоты до 20:23")).toBe("yellow");
  expect(statusColor("ждёт ручной авторизации")).toBe("red");
  expect(statusColor("?")).toBe("gray");
  expect(percentText(null)).toBe("-");
  expect(percentText(51)).toBe("51%");
});
