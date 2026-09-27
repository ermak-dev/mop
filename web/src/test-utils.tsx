// Общее проверок страницы (#323): отрисовка под MantineProvider одной
// функцией и пустой снимок, от которого проверка меняет только своё.
import type { ReactElement } from "react";
import { render } from "@testing-library/react";
import { MantineProvider, type MantineProviderProps } from "@mantine/core";
import type { Snapshot } from "./types";

/** Отрисовать под MantineProvider; providerProps -- например, схема цвета. */
export function renderUi(ui: ReactElement, providerProps?: Omit<MantineProviderProps, "children">) {
  return render(<MantineProvider {...providerProps}>{ui}</MantineProvider>);
}

/** Снимок без папетов, узлов и событий; overrides -- то, что важно проверке. */
export function emptySnapshot(overrides: Partial<Snapshot> = {}): Snapshot {
  return {
    at: null, projects: [], nodes: [], usage: [], per_puppet: [], per_user: [], journal: [],
    errors: [], creds: [], masters: [],
    counts: { puppets: 0, free: 0, busy: 0, sick: 0, silent: 0, down: 0 },
    ...overrides,
  };
}
