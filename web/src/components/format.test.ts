// Запись чисел и корзин как на старой странице (#298).
import { gb, gbOfMb, human, hhmm, nodeKind, ratio } from "./format";

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
  expect(hhmm(null)).toBe("-");
});
