// Светлая тема (#301, просьба оператора 27.09): по умолчанию светлая,
// переключатель в шапке меняет схему Mantine, выбор помнит браузер.
import { fireEvent, render, screen } from "@testing-library/react";
import { MantineProvider } from "@mantine/core";
import { ThemeSwitch, DEFAULT_SCHEME } from "./ThemeSwitch";

test("the dashboard starts light", () => {
  expect(DEFAULT_SCHEME).toBe("light");
});

test("the switch changes the colour scheme of the page", () => {
  localStorage.clear();
  render(<MantineProvider defaultColorScheme={DEFAULT_SCHEME}><ThemeSwitch /></MantineProvider>);
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe("light");
  fireEvent.click(screen.getByText("тёмная"));
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe("dark");
  fireEvent.click(screen.getByText("светлая"));
  expect(document.documentElement.getAttribute("data-mantine-color-scheme")).toBe("light");
});
