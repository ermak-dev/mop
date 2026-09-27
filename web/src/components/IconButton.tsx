// Кнопка-иконка с подсказкой (#304, #323): подпись одна -- и в Tooltip, и в
// aria-label, по ней её находят и человек, и проверка. Прежде подпись
// писалась дважды у каждой кнопки и однажды разошлась бы.
import type { ReactNode } from "react";
import { ActionIcon, Tooltip, type ActionIconProps } from "@mantine/core";

type Props = ActionIconProps & {
  label: string;
  children: ReactNode;
  onClick?: () => void;
  /** Ссылка во вкладку вместо кнопки. */
  href?: string;
  "data-testid"?: string;
};

export function IconButton({ label, children, href, ...rest }: Props) {
  const icon = href
    ? <ActionIcon component="a" href={href} target="_blank" rel="noopener" aria-label={label} {...rest}>{children}</ActionIcon>
    : <ActionIcon aria-label={label} {...rest}>{children}</ActionIcon>;
  return <Tooltip label={label} withArrow>{icon}</Tooltip>;
}

/** Иконка без кнопки (сегмент переключателя) с той же единственной подписью. */
export function IconLabel({ label, children }: { label: string; children: ReactNode }) {
  return (
    <Tooltip label={label} withArrow>
      <span aria-label={label} style={{ display: "flex" }}>{children}</span>
    </Tooltip>
  );
}
