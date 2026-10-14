// HYPOTHESIS: FitAddon вычитает padding у .xterm, но не у родителя;
// внешний padding даёт лишнюю строку и обрезает нижний край. Базовый
// xterm overflow-y:scroll показывает полосу даже без истории.
// SOLUTION: без padding у родителя FitAddon мерит видимую высоту;
// auto оставляет настоящую прокрутку истории, но убирает пустую полосу.
// RESULT: три свойства сначала упали, затем прошли.
// STATUS: FIXED — see #621
import { afterEach, expect, test, vi } from 'vitest';
const { readFileSync } = await vi.importActual<{ readFileSync: (path: string, encoding: string) => string }>('node:fs');
const css = readFileSync('src/components/hybridWorkspace.css', 'utf8');

let style: HTMLStyleElement | null = null;
afterEach(() => { style?.remove(); style = null; });

test('#621 measured FitAddon height fits the visible xterm viewport', () => {
  style = document.createElement('style');
  style.textContent = css;
  document.head.append(style);
  expect(css).toContain('.hybrid-terminal');
  const parent = document.createElement('div');
  parent.className = 'hybrid-terminal';
  parent.style.height = '594px';
  parent.style.boxSizing = 'border-box';
  const element = document.createElement('div');
  element.className = 'xterm';
  parent.append(element);
  document.body.append(parent);
  try {
    const outer = getComputedStyle(parent);
    const inner = getComputedStyle(element);
    const borderBoxHeight = parseFloat(parent.style.height);
    const offered = borderBoxHeight - parseFloat(inner.paddingTop) - parseFloat(inner.paddingBottom);
    const visible = borderBoxHeight - parseFloat(outer.paddingTop) - parseFloat(outer.paddingBottom);
    expect(offered).toBeLessThanOrEqual(visible);
  } finally { parent.remove(); }
});

test('#621 terminal frame can shrink with the modal on short screens', () => {
  style = document.createElement('style');
  style.textContent = css;
  document.head.append(style);
  const parent = document.createElement('div');
  parent.className = 'hybrid-terminal'; document.body.append(parent);
  try { expect(getComputedStyle(parent).minHeight).toBe('0px'); }
  finally { parent.remove(); }
});

test('#621 terminal scrollbar appears only for real scrollback', () => {
  style = document.createElement('style');
  style.textContent = '.xterm .xterm-viewport { overflow-y: scroll; }\n' + css;
  document.head.append(style);
  const parent = document.createElement('div');
  parent.className = 'hybrid-terminal';
  const element = document.createElement('div');
  element.className = 'xterm';
  const viewport = document.createElement('div');
  viewport.className = 'xterm-viewport';
  element.append(viewport); parent.append(element); document.body.append(parent);
  try { expect(getComputedStyle(viewport).overflowY).toBe('auto'); }
  finally { parent.remove(); }
});
