// Ширина ростера сохраняется при закрытии/смене цели, без браузерного хранилища (#666).
import { useLayoutEffect, useRef, useState, type CSSProperties, type ReactNode } from 'react';

const DEFAULT_ROSTER = 378;
const MIN_ROSTER = 260;
const MIN_SCREEN = 440;
const DIVIDER = 12;

export function DashboardSplit({ open, main, screen }: { open: boolean; main: ReactNode; screen: ReactNode }) {
  const body = useRef<HTMLDivElement>(null);
  const drag = useRef<{ id: number; x: number; width: number } | null>(null);
  const [chosen, setChosen] = useState(DEFAULT_ROSTER);
  const [size, setSize] = useState({ width: 0, desktop: window.innerWidth > 900 });
  useLayoutEffect(() => {
    const measure = () => {
      setSize({ width: body.current?.getBoundingClientRect().width || window.innerWidth - 24, desktop: window.innerWidth > 900 });
      drag.current = null;
    };
    measure();
    window.addEventListener('resize', measure);
    const observer = new ResizeObserver(measure);
    if (body.current) observer.observe(body.current);
    return () => { window.removeEventListener('resize', measure); observer.disconnect(); };
  }, []);
  const max = Math.max(MIN_ROSTER, size.width - DIVIDER - MIN_SCREEN);
  const clamp = (width: number) => Math.min(max, Math.max(MIN_ROSTER, width));
  const width = clamp(chosen);
  const showDivider = open && size.desktop;
  useLayoutEffect(() => { if (!showDivider) drag.current = null; }, [showDivider]);

  return <div ref={body} className={`dashboard-body${open ? ' has-screen' : ''}`}
    style={{ '--roster-width': `${width}px` } as CSSProperties}>
    {main}
    {showDivider ? <div className="dashboard-splitter" role="separator" tabIndex={0}
      aria-label="Resize roster and screen" aria-orientation="vertical"
      aria-valuemin={MIN_ROSTER} aria-valuemax={max} aria-valuenow={width}
      onKeyDown={event => {
        const next = { ArrowLeft: width - 20, ArrowRight: width + 20, Home: MIN_ROSTER, End: max }[event.key];
        if (next === undefined) return;
        event.preventDefault();
        setChosen(clamp(next));
      }}
      onPointerDown={event => {
        if (event.button !== 0) return;
        event.preventDefault();
        event.currentTarget.focus();
        event.currentTarget.setPointerCapture(event.pointerId);
        drag.current = { id: event.pointerId, x: event.clientX, width };
      }}
      onPointerMove={event => {
        const start = drag.current;
        if (start && start.id === event.pointerId) setChosen(clamp(start.width + event.clientX - start.x));
      }}
      onPointerUp={event => {
        if (drag.current?.id !== event.pointerId) return;
        drag.current = null;
        if (event.currentTarget.hasPointerCapture(event.pointerId)) event.currentTarget.releasePointerCapture(event.pointerId);
      }}
      onPointerCancel={() => { drag.current = null; }}
      onLostPointerCapture={() => { drag.current = null; }}
    /> : null}
    {screen}
  </div>;
}
