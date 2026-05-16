import { describe, expect, it, vi } from "vitest";

import {
  attachTerminalWheelScroller,
  handleTerminalWheelScroll,
  WHEEL_DELTA_LINE,
  WHEEL_DELTA_PAGE,
  WHEEL_DELTA_PIXEL,
  wheelDeltaToTerminalLines,
} from "./wheelScroll";

describe("terminal wheel scrolling", () => {
  it("converts pixel wheels into terminal scroll lines", () => {
    expect(wheelDeltaToTerminalLines(-48, WHEEL_DELTA_PIXEL, 24)).toBe(-2);
    expect(wheelDeltaToTerminalLines(72, WHEEL_DELTA_PIXEL, 24)).toBe(3);
    expect(wheelDeltaToTerminalLines(1, WHEEL_DELTA_PIXEL, 24)).toBe(1);
  });

  it("converts line and page wheels using terminal dimensions", () => {
    expect(wheelDeltaToTerminalLines(-3, WHEEL_DELTA_LINE, 24)).toBe(-3);
    expect(wheelDeltaToTerminalLines(1, WHEEL_DELTA_PAGE, 24)).toBe(24);
  });

  it("consumes wheel events before they can reach ttyd input", () => {
    const preventDefault = vi.fn();
    const stopPropagation = vi.fn();
    const stopImmediatePropagation = vi.fn();
    const scrollLines = vi.fn();

    const shouldContinue = handleTerminalWheelScroll(
      {
        deltaMode: WHEEL_DELTA_PIXEL,
        deltaY: -48,
        preventDefault,
        stopPropagation,
        stopImmediatePropagation,
      },
      { rows: 24, scrollLines, attachCustomWheelEventHandler: vi.fn() },
    );

    expect(shouldContinue).toBe(false);
    expect(preventDefault).toHaveBeenCalledOnce();
    expect(stopPropagation).toHaveBeenCalledOnce();
    expect(stopImmediatePropagation).toHaveBeenCalledOnce();
    expect(scrollLines).toHaveBeenCalledWith(-2);
  });

  it("registers an xterm custom wheel handler", () => {
    const attachCustomWheelEventHandler = vi.fn();

    attachTerminalWheelScroller({ rows: 24, scrollLines: vi.fn(), attachCustomWheelEventHandler });

    expect(attachCustomWheelEventHandler).toHaveBeenCalledOnce();
    expect(attachCustomWheelEventHandler.mock.calls[0]?.[0]).toBeTypeOf("function");
  });
});
