export interface TerminalSize {
  columns: number;
  rows: number;
}

export type TtydCommand = "0" | "1" | "2" | "3";
export type TtydOutputPayload = string | Uint8Array;

const encoder = new TextEncoder();

const MIN_COLUMNS = 2;
const MIN_ROWS = 2;
const MAX_COLUMNS = 500;
const MAX_ROWS = 200;

function clampInteger(value: number, min: number, max: number): number {
  if (!Number.isFinite(value)) return min;
  return Math.min(max, Math.max(min, Math.floor(value)));
}

export function normaliseTerminalSize(columns: number, rows: number): TerminalSize {
  return {
    columns: clampInteger(columns, MIN_COLUMNS, MAX_COLUMNS),
    rows: clampInteger(rows, MIN_ROWS, MAX_ROWS),
  };
}

export function encodeInitialTerminalSize(columns: number, rows: number): Uint8Array {
  return encoder.encode(JSON.stringify(normaliseTerminalSize(columns, rows)));
}

export function encodeTtydCommandFrame(command: TtydCommand, payload = ""): Uint8Array {
  const encoded = encoder.encode(payload);
  const frame = new Uint8Array(encoded.length + 1);
  frame[0] = command.charCodeAt(0);
  frame.set(encoded, 1);
  return frame;
}

export function decodeTtydOutputFrame(data: string | ArrayBufferLike): TtydOutputPayload | null {
  if (typeof data === "string") {
    return data[0] === "0" ? data.slice(1) : null;
  }

  const view = new Uint8Array(data);
  if (view.length === 0 || view[0] !== 48 /* ASCII "0" */) return null;
  return view.subarray(1);
}
