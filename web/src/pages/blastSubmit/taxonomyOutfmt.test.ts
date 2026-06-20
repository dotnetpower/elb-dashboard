import { describe, expect, it } from "vitest";

import { INITIAL } from "@/pages/blastSubmitModel";
import { buildEffectiveAdditionalOptions } from "@/pages/blastSubmit/useSubmitMutation";

describe("buildEffectiveAdditionalOptions taxonomy columns (#29)", () => {
  it("emits no -outfmt when taxonomy columns are off", () => {
    const opts = buildEffectiveAdditionalOptions({ ...INITIAL });
    expect(opts ?? "").not.toContain("-outfmt");
  });

  it("emits the canonical UNQUOTED multi-token specifier when taxonomy is on", () => {
    const opts = buildEffectiveAdditionalOptions({
      ...INITIAL,
      outfmt_taxonomy_columns: true,
    });
    expect(opts).toContain("-outfmt 7 std staxids sscinames stitle qcovs");
    // Never quote the specifier — quotes break the generated Job YAML.
    expect(opts).not.toContain('"7 std staxids');
  });

  it("does not double the -outfmt flag when the user already supplied one", () => {
    const opts = buildEffectiveAdditionalOptions({
      ...INITIAL,
      outfmt_taxonomy_columns: true,
      additional_options: "-outfmt 7 std staxids sstrand",
    });
    const occurrences = (opts ?? "").match(/-outfmt/g) ?? [];
    expect(occurrences).toHaveLength(1);
    // The user's explicit specifier wins.
    expect(opts).toContain("-outfmt 7 std staxids sstrand");
  });
});
