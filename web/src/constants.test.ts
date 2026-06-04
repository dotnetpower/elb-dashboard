import { describe, expect, it } from "vitest";

import { phaseLabel, queueReasonText } from "./constants";

describe("phaseLabel", () => {
  it("collapses queued-family phases to 'queued'", () => {
    expect(phaseLabel("waiting_for_submit_slot")).toBe("queued");
    expect(phaseLabel("waiting_for_capacity")).toBe("queued");
    expect(phaseLabel("queued")).toBe("queued");
  });

  it("passes non-queued phases through unchanged", () => {
    expect(phaseLabel("running")).toBe("running");
    expect(phaseLabel("submitted")).toBe("submitted");
    expect(phaseLabel("completed")).toBe("completed");
  });
});

describe("queueReasonText", () => {
  it("explains each queued-family phase", () => {
    expect(queueReasonText("waiting_for_submit_slot")).toBe("Waiting for submit slot");
    expect(queueReasonText("waiting_for_capacity")).toBe("Waiting for cluster capacity");
    expect(queueReasonText("queued")).toBe("Waiting in queue");
  });

  it("returns null for non-queued phases and empty input", () => {
    expect(queueReasonText("running")).toBeNull();
    expect(queueReasonText("completed")).toBeNull();
    expect(queueReasonText(undefined)).toBeNull();
    expect(queueReasonText(null)).toBeNull();
    expect(queueReasonText("")).toBeNull();
  });
});
