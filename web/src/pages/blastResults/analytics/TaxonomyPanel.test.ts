import { describe, expect, it } from "vitest";

import { __internals } from "./TaxonomyPanel";

const { buildLineageTree } = __internals;

// Helper to make a row succinctly.
function row(
  organism: string,
  taxid: string,
  count: number,
  lineageEx?: Array<{ rank: string; taxid: number; scientific_name: string }>,
) {
  return {
    key: `${organism}|${taxid}`.toLowerCase(),
    organism,
    taxid,
    count,
    bestEvalue: 0.0,
    topBitscore: 0.0,
    lineageEx,
  };
}

describe("buildLineageTree", () => {
  it("returns an empty root when no rows are passed", () => {
    const tree = buildLineageTree([]);
    expect(tree.children.size).toBe(0);
  });

  it("groups rows that share an ancestor under a single inner node", () => {
    const monkeypoxLineage = [
      { rank: "superkingdom", taxid: 10239, scientific_name: "Viruses" },
      { rank: "genus", taxid: 10240, scientific_name: "Orthopoxvirus" },
    ];
    const vacciniaLineage = [
      { rank: "superkingdom", taxid: 10239, scientific_name: "Viruses" },
      { rank: "genus", taxid: 10240, scientific_name: "Orthopoxvirus" },
    ];
    const tree = buildLineageTree([
      row("Monkeypox virus", "10244", 99, monkeypoxLineage),
      row("Vaccinia virus", "10245", 1, vacciniaLineage),
    ]);

    // Single top-level "Viruses" node
    const viruses = tree.children.get("10239");
    expect(viruses).toBeDefined();
    expect(viruses!.name).toBe("Viruses");
    expect(viruses!.totalCount).toBe(100); // 99 + 1

    // Single shared genus
    const orthopox = viruses!.children.get("10240");
    expect(orthopox).toBeDefined();
    expect(orthopox!.totalCount).toBe(100);

    // Two distinct leaves under the genus.
    expect(orthopox!.children.size).toBe(2);
    const names = [...orthopox!.children.values()].map((n) => n.name);
    expect(names.sort()).toEqual(["Monkeypox virus", "Vaccinia virus"]);
  });

  it("places rows without lineage_ex in an Unresolved bucket", () => {
    const tree = buildLineageTree([
      row("Monkeypox virus", "10244", 99, undefined),
      row("Unknown organism", "0", 1, []),
    ]);
    const unresolved = tree.children.get("unresolved");
    expect(unresolved).toBeDefined();
    expect(unresolved!.name).toContain("Unresolved");
    expect(unresolved!.totalCount).toBe(100);
    expect(unresolved!.children.size).toBe(2);
  });

  it("tracks leafCount separately from totalCount for inner nodes", () => {
    const tree = buildLineageTree([
      row("Monkeypox virus", "10244", 99, [
        { rank: "genus", taxid: 10240, scientific_name: "Orthopoxvirus" },
      ]),
    ]);
    const orthopox = tree.children.get("10240")!;
    // Inner genus node — its hits live in descendant leaves, not at this rank.
    expect(orthopox.totalCount).toBe(99);
    expect(orthopox.leafCount).toBe(0);
    const leaf = [...orthopox.children.values()][0];
    expect(leaf.leafCount).toBe(99);
  });

  it("accumulates duplicate leaf rows into a single node", () => {
    // Two rows for the same organism (could happen if the rollup is
    // partial / cross-shard merge collides) — the tree should sum
    // counts instead of forking the leaf.
    const lin = [{ rank: "genus", taxid: 10240, scientific_name: "Orthopoxvirus" }];
    const tree = buildLineageTree([
      row("Monkeypox virus", "10244", 60, lin),
      row("Monkeypox virus", "10244", 39, lin),
    ]);
    const orthopox = tree.children.get("10240")!;
    expect(orthopox.totalCount).toBe(99);
    const leaves = [...orthopox.children.values()];
    expect(leaves).toHaveLength(1);
    expect(leaves[0].totalCount).toBe(99);
  });
});
