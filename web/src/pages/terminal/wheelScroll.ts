interface WheelEventLike {
  deltaMode: number;
  deltaY: number;
  preventDefault(): void;
  stopPropagation(): void;
  stopImmediatePropagation?: () => void;
}

interface ScrollableTerminal {
  rows: number;
  scrollLines(amount: number): void;
  attachCustomWheelEventHandler(handler: (event: WheelEvent) => boolean): void;
}

const PIXELS_PER_TERMINAL_LINE = 24;
export const WHEEL_DELTA_PIXEL = 0;
export const WHEEL_DELTA_LINE = 1;
export const WHEEL_DELTA_PAGE = 2;

export function wheelDeltaToTerminalLines(deltaY: number, deltaMode: number, rows: number): number {
  if (!Number.isFinite(deltaY) || deltaY === 0) return 0;

  const direction = Math.sign(deltaY);
  const distance = Math.abs(deltaY);

  if (deltaMode === WHEEL_DELTA_LINE) {
    return direction * Math.max(1, Math.ceil(distance));
  }

  if (deltaMode === WHEEL_DELTA_PAGE) {
    return direction * Math.max(1, Math.ceil(distance * Math.max(1, rows)));
  }

  return direction * Math.max(1, Math.ceil(distance / PIXELS_PER_TERMINAL_LINE));
}

export function handleTerminalWheelScroll(
  event: WheelEventLike,
  terminal: ScrollableTerminal,
): boolean {
  const lines = wheelDeltaToTerminalLines(event.deltaY, event.deltaMode, terminal.rows);
  event.preventDefault();
  event.stopPropagation();
  event.stopImmediatePropagation?.();

  if (lines !== 0) {
    terminal.scrollLines(lines);
  }

  return false;
}

export function attachTerminalWheelScroller(terminal: ScrollableTerminal): void {
  terminal.attachCustomWheelEventHandler((event) => handleTerminalWheelScroll(event, terminal));
}
