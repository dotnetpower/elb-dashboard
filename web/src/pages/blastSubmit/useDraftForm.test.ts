import { describe, expect, it } from "vitest";

import {
  DRAFT_KEY,
  DRAFT_SCHEMA_VERSION,
  OUTFMT_PREFERENCE_KEY,
  createInitialForm,
  loadOutfmtPreference,
  restoreDraftForm,
  saveOutfmtPreference,
} from "@/pages/blastSubmit/useDraftForm";

class MemoryStorage {
  private readonly values = new Map<string, string>();

  getItem(key: string): string | null {
    return this.values.get(key) ?? null;
  }

  setItem(key: string, value: string): void {
    this.values.set(key, value);
  }

  removeItem(key: string): void {
    this.values.delete(key);
  }
}

describe("blast submit outfmt preference", () => {
  it("defaults to BLAST XML outfmt 5", () => {
    expect(createInitialForm(null).outfmt).toBe(5);
  });

  it("restores a supported browser-stored outfmt", () => {
    const storage = new MemoryStorage();
    storage.setItem(OUTFMT_PREFERENCE_KEY, "6");

    expect(loadOutfmtPreference(storage)).toBe(6);
    expect(createInitialForm(storage).outfmt).toBe(6);
  });

  it("ignores unsupported browser-stored outfmt values", () => {
    const storage = new MemoryStorage();
    storage.setItem(OUTFMT_PREFERENCE_KEY, "9");

    expect(loadOutfmtPreference(storage)).toBeNull();
    expect(createInitialForm(storage).outfmt).toBe(5);
  });

  it("stores supported user changes in browser storage", () => {
    const storage = new MemoryStorage();

    saveOutfmtPreference(7, storage);

    expect(storage.getItem(OUTFMT_PREFERENCE_KEY)).toBe("7");
  });

  it("migrates old session drafts away from the previous outfmt default", () => {
    const sessionStorage = new MemoryStorage();
    sessionStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({ draft_version: DRAFT_SCHEMA_VERSION - 1, outfmt: 7 }),
    );

    expect(restoreDraftForm(sessionStorage, null).outfmt).toBe(5);
  });

  it("does not let session drafts override the stored outfmt preference", () => {
    const sessionStorage = new MemoryStorage();
    const localStorage = new MemoryStorage();
    sessionStorage.setItem(
      DRAFT_KEY,
      JSON.stringify({ draft_version: DRAFT_SCHEMA_VERSION, outfmt: 7 }),
    );

    expect(restoreDraftForm(sessionStorage, localStorage).outfmt).toBe(5);

    localStorage.setItem(OUTFMT_PREFERENCE_KEY, "6");

    expect(restoreDraftForm(sessionStorage, localStorage).outfmt).toBe(6);
  });
});
