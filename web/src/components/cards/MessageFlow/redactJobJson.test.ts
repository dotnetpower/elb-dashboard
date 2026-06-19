import { describe, expect, it } from "vitest";

import { redactJobJson, scrubSasValue } from "./redactJobJson";

describe("scrubSasValue", () => {
  it("redacts the sig= signature but keeps the blob path", () => {
    const url =
      "https://acct.blob.core.windows.net/results/job-1/out.xml?sv=2024-11-04&se=2026-06-20&sig=AbCd%2FxyZ123";
    const out = scrubSasValue(url);
    expect(out).toContain("results/job-1/out.xml");
    expect(out).toContain("sig=<redacted>");
    expect(out).not.toContain("AbCd");
  });

  it("leaves a non-SAS string untouched", () => {
    expect(scrubSasValue("blast-db/core_nt/core_nt")).toBe("blast-db/core_nt/core_nt");
  });
});

describe("redactJobJson", () => {
  it("drops sensitive keys at any nesting depth", () => {
    const state = {
      job_id: "abc",
      owner_oid: "11111111-1111-1111-1111-111111111111",
      subscription_id: "22222222-2222-2222-2222-222222222222",
      metadata: {
        tenant_id: "33333333-3333-3333-3333-333333333333",
        owner_upn: "user@contoso.com",
        sas_token: "se=...&sig=secret",
      },
    };
    const out = redactJobJson(state) as Record<string, unknown>;
    expect(out.job_id).toBe("abc");
    expect(out.owner_oid).toBeUndefined();
    expect(out.subscription_id).toBeUndefined();
    const meta = out.metadata as Record<string, unknown>;
    expect(meta.tenant_id).toBeUndefined();
    expect(meta.sas_token).toBeUndefined();
    // The submitter alias is shown intentionally elsewhere — never redacted.
    expect(meta.owner_upn).toBe("user@contoso.com");
  });

  it("recurses through arrays and scrubs SAS URLs in string values", () => {
    const state = {
      results: [
        { url: "https://a.blob.core.windows.net/c/x?sv=1&sig=ZZZ" },
        "plain-string",
      ],
    };
    const out = redactJobJson(state) as { results: unknown[] };
    const first = out.results[0] as Record<string, string>;
    expect(first.url).toContain("sig=<redacted>");
    expect(first.url).not.toContain("ZZZ");
    expect(out.results[1]).toBe("plain-string");
  });

  it("passes primitives and null through unchanged", () => {
    expect(redactJobJson(null)).toBeNull();
    expect(redactJobJson(42)).toBe(42);
    expect(redactJobJson(true)).toBe(true);
  });
});
