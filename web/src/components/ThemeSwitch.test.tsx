// Светлая тема (#301, просьба оператора 27.09): по умолчанию светлая,
// переключатель в шапке меняет схему Mantine, выбор помнит браузер.
import { fireEvent, screen } from "@testing-library/react";
import { renderUi } from "../test-utils";
import { ThemeSwitch, DEFAULT_SCHEME } from "./ThemeSwitch";

test("the dashboard starts light", () => {
  expect(DEFAULT_SCHEME).toBe("light");
});

test("the switch changes the colour scheme of the page", () => {
  localStorage.clear();
  renderUi(<ThemeSwitch />, { defaultColorScheme: DEFAULT_SCHEME });
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe("light");
  fireEvent.click(screen.getByLabelText("тёмная тема"));
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe("dark");
  fireEvent.click(screen.getByLabelText("светлая тема"));
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe("light");
});

test("the switch is icons, not words (#304)", () => {
  renderUi(<ThemeSwitch />, { defaultColorScheme: DEFAULT_SCHEME });
  expect(screen.queryByText("тёмная")).toBeNull();
  expect(screen.getByLabelText("как в системе").querySelector("svg")).not.toBeNull();
});
