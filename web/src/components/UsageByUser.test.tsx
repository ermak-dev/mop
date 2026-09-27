// Расход по пользователям (#299): те же колонки, что на прежней странице,
// порядок строк -- порядок сервера, числа -- человеческие.
import { screen, within } from "@testing-library/react";
import { renderUi } from "../test-utils";
import { UsageByUser, USER_COLUMNS } from "./UsageByUser";
import type { UsageUser } from "../types";

const USERS: UsageUser[] = [
  { login: "ermak", input: 11_000_000, output: 2_100_000, cache_write: 0, cache_read: 822_000_000, total: 835_000_000 },
  { login: "-", input: 50_000, output: 11_000, cache_write: 0, cache_read: 285_000, total: 347_000 },
];

test("the table has the old page's columns in order", () => {
  renderUi(<UsageByUser users={USERS} />);
  const heads = screen.getAllByRole("columnheader").map((h) => h.textContent);
  expect(heads).toEqual([...USER_COLUMNS]);
});

test("rows keep the server's order and human numbers", () => {
  renderUi(<UsageByUser users={USERS} />);
  const rows = screen.getAllByRole("row").slice(1);
  expect(rows).toHaveLength(2);
  expect(within(rows[0]).getAllByRole("cell").map((c) => c.textContent))
    .toEqual(["ermak", "11M", "2.1M", "0", "822M", "835M"]);
  expect(within(rows[1]).getAllByRole("cell")[0].textContent).toBe("-");
});

test("no rows: a hint instead of a table", () => {
  renderUi(<UsageByUser users={[]} />);
  expect(screen.getByText("расход по пользователям появится после первого разбора транскриптов")).toBeInTheDocument();
  expect(screen.queryByRole("table")).toBeNull();
});
