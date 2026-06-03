import { describe, expect, it } from "vitest";

import {
  DB_CATALOG,
  MOLECULE_PROGRAMS,
  countUnavailableDbs,
  filterDbCatalog,
  formatNcbiVersion,
  ncbiBlastDbFtpUrl,
} from "./storageDbCatalog";

describe("NCBI BLAST DB FTP helpers", () => {
  it("formats snapshot tags and links to a valid DB metadata file", () => {
    const version = "2026-05-20-00-00-00";

    expect(formatNcbiVersion(version)).toBe("2026-05-20 00:00:00");
    expect(ncbiBlastDbFtpUrl("core_nt", "nucl")).toBe(
      "https://ftp.ncbi.nlm.nih.gov/blast/db/v5/core_nt-nucl-metadata.json",
    );
  });

  it("falls back to the v5 FTP listing for unsafe or incomplete inputs", () => {
    expect(ncbiBlastDbFtpUrl("../core_nt", "nucl")).toBe(
      "https://ftp.ncbi.nlm.nih.gov/blast/db/v5/",
    );
    expect(ncbiBlastDbFtpUrl("core_nt", null)).toBe(
      "https://ftp.ncbi.nlm.nih.gov/blast/db/v5/",
    );
  });
});

describe("filterDbCatalog", () => {
  it("returns only nucleotide DBs for the nucl filter", () => {
    const out = filterDbCatalog(DB_CATALOG, "nucl", true);
    expect(out.length).toBeGreaterThan(0);
    expect(out.every((d) => d.type === "nucl")).toBe(true);
  });

  it("returns only protein DBs for the prot filter", () => {
    const out = filterDbCatalog(DB_CATALOG, "prot", true);
    expect(out.length).toBeGreaterThan(0);
    expect(out.every((d) => d.type === "prot")).toBe(true);
  });

  it("hides unsupported DBs by default and reveals them when asked", () => {
    const hidden = filterDbCatalog(DB_CATALOG, "all", false);
    const shown = filterDbCatalog(DB_CATALOG, "all", true);
    expect(hidden.every((d) => !d.unsupported)).toBe(true);
    expect(shown.length).toBeGreaterThanOrEqual(hidden.length);
  });
});

describe("countUnavailableDbs", () => {
  it("counts unsupported DBs for the active molecule filter", () => {
    const all = countUnavailableDbs(DB_CATALOG, "all");
    const nucl = countUnavailableDbs(DB_CATALOG, "nucl");
    const prot = countUnavailableDbs(DB_CATALOG, "prot");
    expect(all).toBe(DB_CATALOG.filter((d) => d.unsupported).length);
    expect(nucl + prot).toBe(all);
  });
});

describe("recommended starter databases", () => {
  it("flags one gettable nucleotide and one gettable protein starter", () => {
    const core = DB_CATALOG.find((d) => d.value === "core_nt");
    const swiss = DB_CATALOG.find((d) => d.value === "swissprot");
    expect(core?.recommended).toBe(true);
    expect(core?.type).toBe("nucl");
    expect(swiss?.recommended).toBe(true);
    expect(swiss?.type).toBe("prot");
  });
});

describe("MOLECULE_PROGRAMS", () => {
  it("maps each molecule type to its BLAST programs", () => {
    expect(MOLECULE_PROGRAMS.nucl).toContain("blastn");
    expect(MOLECULE_PROGRAMS.prot).toContain("blastp");
    expect(MOLECULE_PROGRAMS.prot).toContain("blastx");
  });
});