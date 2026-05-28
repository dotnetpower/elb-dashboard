import { describe, expect, it, vi } from "vitest";

import { listWithMiFallback } from "./armWithMiFallback";

describe("listWithMiFallback", () => {
  it("returns the direct result when it is non-empty", async () => {
    const direct = vi.fn().mockResolvedValue([{ id: "a" }]);
    const miProxy = vi.fn().mockResolvedValue([{ id: "b" }]);

    const out = await listWithMiFallback(direct, miProxy);

    expect(out).toEqual([{ id: "a" }]);
    expect(direct).toHaveBeenCalledTimes(1);
    expect(miProxy).not.toHaveBeenCalled();
  });

  it("falls back to the MI proxy when the direct result is empty", async () => {
    const direct = vi.fn().mockResolvedValue([]);
    const miProxy = vi.fn().mockResolvedValue([{ id: "from-mi" }]);

    const out = await listWithMiFallback(direct, miProxy);

    expect(out).toEqual([{ id: "from-mi" }]);
    expect(direct).toHaveBeenCalledTimes(1);
    expect(miProxy).toHaveBeenCalledTimes(1);
  });

  it("falls back to the MI proxy when the direct call throws", async () => {
    const direct = vi.fn().mockRejectedValue(new Error("ARM 401"));
    const miProxy = vi.fn().mockResolvedValue([{ id: "from-mi" }]);

    const out = await listWithMiFallback(direct, miProxy);

    expect(out).toEqual([{ id: "from-mi" }]);
    expect(direct).toHaveBeenCalledTimes(1);
    expect(miProxy).toHaveBeenCalledTimes(1);
  });

  it("returns an empty array (no throw) when both calls fail", async () => {
    const direct = vi.fn().mockRejectedValue(new Error("ARM 401"));
    const miProxy = vi.fn().mockRejectedValue(new Error("backend 503"));

    const out = await listWithMiFallback(direct, miProxy);

    expect(out).toEqual([]);
    expect(direct).toHaveBeenCalledTimes(1);
    expect(miProxy).toHaveBeenCalledTimes(1);
  });

  it("returns an empty array when both calls return empty", async () => {
    const direct = vi.fn().mockResolvedValue([]);
    const miProxy = vi.fn().mockResolvedValue([]);

    const out = await listWithMiFallback(direct, miProxy);

    expect(out).toEqual([]);
    expect(direct).toHaveBeenCalledTimes(1);
    expect(miProxy).toHaveBeenCalledTimes(1);
  });
});
