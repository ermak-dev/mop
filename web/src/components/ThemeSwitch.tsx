// Переключатель темы (#301): светлая по умолчанию, тёмная и «как в системе».
// Выбор хранит Mantine в localStorage браузера, сервер о нём не знает.
import { SegmentedControl, useMantineColorScheme, type MantineColorScheme } from "@mantine/core";

export const DEFAULT_SCHEME: MantineColorScheme = "light";

const CHOICES = [
  { label: "светлая", value: "light" },
  { label: "тёмная", value: "dark" },
  { label: "как в системе", value: "auto" },
];

export function ThemeSwitch() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  return (
    <SegmentedControl
      size="xs"
      data={CHOICES}
      value={colorScheme}
      onChange={(v) => setColorScheme(v as MantineColorScheme)}
      aria-label="тема"
    />
  );
}
