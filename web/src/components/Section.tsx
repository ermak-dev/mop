// Заголовок секции и подсказка пустой секции (#324): одна запись на семь
// секций страницы. Заголовок -- имя и приглушённая пометка рядом; порядок
// заголовка -- проп (секции расхода вложены глубже, order 4).
import type { ReactNode } from "react";
import { Text, Title, type MantineSize, type MantineSpacing } from "@mantine/core";

export function SectionTitle({ title, note, order = 3 }: { title: ReactNode; note?: ReactNode; order?: 3 | 4 }) {
  return (
    <Title order={order}>
      {title}{" "}
      <Text span c="dimmed" size="sm">{note}</Text>
    </Title>
  );
}

/** Приглушённая строка вместо таблицы, когда показывать нечего. */
export function EmptyNote({ children, size, mt }: { children: ReactNode; size?: MantineSize; mt?: MantineSpacing }) {
  return <Text c="dimmed" size={size} mt={mt}>{children}</Text>;
}
