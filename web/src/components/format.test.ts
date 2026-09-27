// Запись чисел и корзин как на старой странице (#298).
import { credStatusText, gb, gbOfMb, human, hhmm, nodeKind, ratio, untilText } from "./format";

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

test("#331 the status cell adds the reset time to the bare quota word only", () => {
  const msk = "Europe/Moscow";
  expect(credStatusText("ждёт квоты", at(2026, 9, 30, 2, 0), NOW, msk)).toBe("ждёт квоты до 30.09 05:00");
  expect(credStatusText("ждёт квоты", null, NOW, msk)).toBe("ждёт квоты");
  expect(credStatusText("активен", at(2026, 9, 30, 2, 0), NOW, msk)).toBe("активен");
  // Сервер до #331 сам вписывал время: второго «до» не будет.
  expect(credStatusText("ждёт квоты до 05:00:00", at(2026, 9, 30, 2, 0), NOW, msk)).toBe("ждёт квоты до 05:00:00");
});

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
