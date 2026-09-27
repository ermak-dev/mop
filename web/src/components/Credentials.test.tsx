// Реестр на странице (#300): кнопка входа только у claude, пустой реестр --
// подсказка про команды.
import { render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { Credentials } from "./Credentials";
import type { Cred } from "../types";

const CREDS: Cred[] = [
  { name: "ermak", profile: "claude", kind: "login", owner: "anton@example.dev", status: "активен",
    resets_at: null, percent: 51, age: "1h", holders: ["pu-mop-6"] },
  { name: "z1", profile: "glm", kind: "key", owner: "", status: "ждёт квоты до 20:23",
    resets_at: 1790425418, percent: 100, age: "5m" },
];

const wrap = (ui: React.ReactElement) => render(<MantineProvider>{ui}</MantineProvider>);

test("the authorize button is on claude rows only", () => {
  wrap(<Credentials creds={CREDS} />);
  expect(screen.getByTestId("auth-ermak")).toBeInTheDocument();
  expect(screen.queryByTestId("auth-z1")).toBeNull();
  expect(screen.getByText("pu-mop-6")).toBeInTheDocument();
  expect(screen.getByText("ждёт квоты до 20:23")).toBeInTheDocument();
  expect(screen.getByText("100%")).toBeInTheDocument();
});

test("an empty registry points at the commands", () => {
  wrap(<Credentials creds={[]} />);
  expect(screen.getByText(/mop cred add/)).toBeInTheDocument();
});
