// Общее проверок страницы (#323): отрисовка под MantineProvider одной
// функцией и пустой снимок, от которого проверка меняет только своё.
import type { ReactElement } from "react";
import { render, screen } from "@testing-library/react";
import { expect } from "vitest";
import { MantineProvider, type MantineProviderProps } from "@mantine/core";
import type { Snapshot } from "./types";
import type { Col } from "./components/TableHead";

/** Отрисовать под MantineProvider; providerProps -- например, схема цвета. */
export function renderUi(ui: ReactElement, providerProps?: Omit<MantineProviderProps, "children">) {
  return render(<MantineProvider {...providerProps}>{ui}</MantineProvider>);
}

/** Снимок без папетов, узлов и событий; overrides -- то, что важно проверке. */
export function emptySnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    at: null, projects: [], nodes: [], usage: [], per_puppet: [], per_user: [], journal: [],
    errors: [], masters: [], masters_every: 30,
    counts: { puppets: 0, free: 0, busy: 0, sick: 0, silent: 0, down: 0 },
    load: {
      total: 0, allocated: 0, free_slots: 0,
      segments: (["busy", "free", "sick", "silent", "other", "vacant"] as const)
        .map((kind) => ({ kind, slots: 0 })),
    },
    ...overrides,
  };
}

/** Заголовки единственной таблицы на экране -- ровно колонки спецификации
 *  (#324): те же подписи в том же порядке, и справа ровно те, что `right`. */
export function expectHead(cols: readonly Col[]) {
  const ths = screen.getAllByRole("columnheader");
  expect(ths.map((th) => th.textContent)).toEqual(cols.map((c) => c.label));
  ths.forEach((th, i) => {
    if (cols[i].right) expect(th).toHaveStyle({ textAlign: "right" });
    else expect(th).not.toHaveStyle({ textAlign: "right" });
  });
}
