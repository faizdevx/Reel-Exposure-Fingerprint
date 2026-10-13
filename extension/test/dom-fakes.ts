/** Test doubles for the observers the adapter uses, plus a way to load a saved fixture. */

import { readFileSync } from 'node:fs';
import { join } from 'node:path';

export function fixture(name: string): string {
  return readFileSync(join(__dirname, 'fixtures', name), 'utf8');
}

export function load(html: string): void {
  const parsed = new DOMParser().parseFromString(html, 'text/html');
  document.body.replaceChildren(...Array.from(parsed.body.childNodes));
}

export function videoById(id: string): HTMLVideoElement {
  const el = document.getElementById(id);
  if (!(el instanceof HTMLVideoElement)) throw new Error(`fixture has no video #${id}`);
  return el;
}

export function setVideo(
  el: HTMLVideoElement,
  state: Partial<{ duration: number; paused: boolean; ended: boolean; readyState: number }>,
): void {
  for (const [key, value] of Object.entries(state)) {
    Object.defineProperty(el, key, { value, configurable: true });
  }
}

export class FakeIO {
  static last: FakeIO | null = null;
  observed = new Set<Element>();
  disconnected = false;
  constructor(
    readonly callback: IntersectionObserverCallback,
    readonly options?: IntersectionObserverInit,
  ) {
    FakeIO.last = this;
  }
  observe(el: Element): void {
    this.observed.add(el);
  }
  unobserve(el: Element): void {
    this.observed.delete(el);
  }
  disconnect(): void {
    this.disconnected = true;
    this.observed.clear();
  }
  /** Deliver entries: [element, visible ratio, visible area]. */
  fire(...items: [Element, number, number?][]): void {
    const entries = items.map(([target, ratio, area = 1000]) => ({
      target,
      intersectionRatio: ratio,
      intersectionRect: { width: area, height: 1 },
    }));
    this.callback(entries as unknown as IntersectionObserverEntry[], this as never);
  }
}

export class FakeMO {
  static last: FakeMO | null = null;
  disconnected = false;
  constructor(readonly callback: MutationCallback) {
    FakeMO.last = this;
  }
  observe(): void {}
  disconnect(): void {
    this.disconnected = true;
  }
  fire(added: Node[], removed: Node[] = []): void {
    this.callback(
      [{ addedNodes: added, removedNodes: removed } as unknown as MutationRecord],
      this as never,
    );
  }
}

export function setVisibility(state: string): void {
  Object.defineProperty(document, 'visibilityState', { value: state, configurable: true });
  document.dispatchEvent(new Event('visibilitychange'));
}
