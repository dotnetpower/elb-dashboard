import { describe, expect, it } from "vitest";

import {
  decodeTtydOutputFrame,
  encodeInitialTerminalSize,
  encodeTtydCommandFrame,
  normaliseTerminalSize,
} from "./remoteTerminalProtocol";

const decoder = new TextDecoder();

describe("remote terminal ttyd protocol helpers", () => {
  it("clamps invalid or extreme terminal dimensions", () => {
    expect(normaliseTerminalSize(0, Number.NaN)).toEqual({ columns: 2, rows: 2 });
    expect(normaliseTerminalSize(80.9, 24.2)).toEqual({ columns: 80, rows: 24 });
    expect(normaliseTerminalSize(9999, 9999)).toEqual({ columns: 500, rows: 200 });
  });

  it("encodes the initial ttyd size as raw JSON bytes", () => {
    const frame = encodeInitialTerminalSize(80, 24);
    expect(decoder.decode(frame)).toBe('{"columns":80,"rows":24}');
  });

  it("encodes command-prefixed binary frames", () => {
    const frame = encodeTtydCommandFrame("0", "printf ok\n");
    expect(frame[0]).toBe("0".charCodeAt(0));
    expect(decoder.decode(frame.subarray(1))).toBe("printf ok\n");
  });

  it("decodes only output frames", () => {
    expect(decodeTtydOutputFrame("0hello")).toBe("hello");
    expect(decodeTtydOutputFrame("1title")).toBeNull();

    const binary = encodeTtydCommandFrame("0", "hello").buffer;
    const decoded = decodeTtydOutputFrame(binary);
    expect(decoded).toBeInstanceOf(Uint8Array);
    expect(decoder.decode(decoded as Uint8Array)).toBe("hello");
  });
});
