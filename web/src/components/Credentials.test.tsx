// Реестр на странице (#300): кнопка входа только у claude, пустой реестр --
// подсказка про команды.
import { screen } from "@testing-library/react";
import { vi } from "vitest";
import { expectHead, renderUi } from "../test-utils";
import { Credentials } from "./Credentials";
import { CRED_COLS } from "./CredentialRow";
import type { Cred } from "../types";

const CREDS: Cred[] = [
  { name: "ermak", profile: "claude", kind: "login", owner: "anton@example.dev", status: "активен",
    resets_at: null, percent: 51, age: "1h", holders: ["pu-mop-6"] },
  // Слово -- голое (#331): время сброса страница берёт из resets_at.
  { name: "z1", profile: "glm", kind: "key", owner: "", status: "ждёт квоты",
    resets_at: 1790425418, percent: 100, age: "5m", holders: [] },
];

test("the authorize button is on claude rows only", () => {
  renderUi(<Credentials creds={CREDS} />);
  expect(screen.getByTestId("auth-ermak")).toBeInTheDocument();
  expect(screen.queryByTestId("auth-z1")).toBeNull();
  expect(screen.getByText("pu-mop-6")).toBeInTheDocument();
  expect(screen.getByText(/^ждёт квоты/)).toBeInTheDocument();
  expect(screen.getByText("100%")).toBeInTheDocument();
});

test("an empty registry points at the commands", () => {
  renderUi(<Credentials creds={[]} />);
  expect(screen.getByText(/mop cred add/)).toBeInTheDocument();
});

// Колонки -- одной записью на заголовок и ячейку (#324): «использовано»
// справа и в заголовке, как его ячейка (прежде заголовок был слева).
test("headers follow the column spec, «использовано» right like its cell", () => {
  renderUi(<Credentials creds={CREDS} />);
  expectHead(Object.values(CRED_COLS));
  expect(screen.getByRole("columnheader", { name: "использовано" })).toHaveStyle({ textAlign: "right" });
  expect(screen.getByText("51%").closest("td")).toHaveStyle({ textAlign: "right" });
});

// #331: ячейка статуса дописывает время сброса из resets_at -- с датой, если
// сброс не сегодня. Часы закреплены; пояс -- машины, поэтому проверяется
// форма, а точная запись -- в format.test.ts с закреплённым поясом.
test("#331 the quota cell reads «до дд.мм чч:мм» from resets_at when the reset is another day", () => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(new Date((1790425418 - 3 * 86400) * 1000));   // за трое суток до сброса
  try {
    renderUi(<Credentials creds={CREDS} />);
    expect(screen.getByText(/^ждёт квоты до \d\d\.\d\d \d\d:\d\d$/)).toBeInTheDocument();
  } finally {
    vi.useRealTimers();
  }
});
