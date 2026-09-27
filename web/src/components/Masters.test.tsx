// Живые мастера (#305): строка на мастера -- проект, логин, сессия, каталог,
// адрес для send и папеты, чья аренда на его логине.
import { screen } from "@testing-library/react";
import { expectHead, renderUi } from "../test-utils";
import { COLS, EMPTY, Masters } from "./Masters";
import type { Master } from "../types";

const M: Master[] = [
  { project: "mop", master: "ermak.mate-7", user: "ermak", session: "mop-ab", cwd: "/home/ermak/mop",
    puppets: ["pu-mop-1", "pu-mop-3"] },
  { project: "rugent", master: "ivan.box-3", user: "ivan", session: "-", cwd: "-", puppets: [] },
];

test("a row per master with its address and puppets", () => {
  renderUi(<Masters masters={M} />);
  expectHead(COLS);
  expect(screen.getByText("ermak.mate-7")).toBeInTheDocument();
  expect(screen.getByText("mop-ab")).toBeInTheDocument();
  expect(screen.getByText("/home/ermak/mop")).toBeInTheDocument();
  expect(screen.getByText("pu-mop-1, pu-mop-3")).toBeInTheDocument();
  expect(screen.getByText("ivan.box-3")).toBeInTheDocument();
  expect(screen.getByText(/2 на шине/)).toBeInTheDocument();
});

// #326: период опроса -- из снимка (masters_every, #325), не своей копией.
test("#326 the poll period comes from the snapshot", () => {
  renderUi(<Masters masters={M} every={45} />);
  expect(screen.getByText(/опрос who раз в 45 с/)).toBeInTheDocument();
});

test("#326 a snapshot without the period: no invented number", () => {
  renderUi(<Masters masters={[]} />);
  expect(screen.getByText("опрос who")).toBeInTheDocument();
});

test("no masters: says so", () => {
  renderUi(<Masters masters={[]} />);
  expect(screen.getByText(EMPTY)).toBeInTheDocument();
});
