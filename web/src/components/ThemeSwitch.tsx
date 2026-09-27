// Переключатель темы (#301): светлая по умолчанию, тёмная и «как в системе».
// Выбор хранит Mantine в localStorage браузера, сервер о нём не знает.
// Варианты -- иконки с подсказкой и aria-label (#304), без слов.
import { SegmentedControl, useMantineColorScheme, type MantineColorScheme } from "@mantine/core";
import { IconDeviceDesktop, IconMoon, IconSun } from "@tabler/icons-react";
import type { ComponentType } from "react";
import { IconLabel } from "./IconButton";

export const DEFAULT_SCHEME: MantineColorScheme = "light";

const CHOICES: { value: MantineColorScheme; label: string; Icon: ComponentType<{ size?: number; stroke?: number }> }[] = [
  { value: "light", label: "светлая тема", Icon: IconSun },
  { value: "dark", label: "тёмная тема", Icon: IconMoon },
  { value: "auto", label: "как в системе", Icon: IconDeviceDesktop },
];

export function ThemeSwitch() {
  const { colorScheme, setColorScheme } = useMantineColorScheme();
  return (
    <SegmentedControl
      size="xs"
      value={colorScheme}
      onChange={(v) => setColorScheme(v as MantineColorScheme)}
      aria-label="тема"
      data={CHOICES.map(({ value, label, Icon }) => ({
        value,
        label: <IconLabel label={label}><Icon size={16} stroke={1.8} /></IconLabel>,
      }))}
    />
  );
}
