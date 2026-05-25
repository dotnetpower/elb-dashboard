import { describe, expect, it } from "vitest";

import { formatNcbiVersion, ncbiBlastDbFtpUrl } from "./storageDbCatalog";

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