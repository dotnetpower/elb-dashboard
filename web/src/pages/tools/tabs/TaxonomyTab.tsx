import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { Loader2, Search } from "lucide-react";

import { taxonomyApi } from "@/api/endpoints";
import { ExamplePicker } from "@/components/ExamplePicker";
import {
  TAXONOMY_EXAMPLES,
  type TaxonomyExampleValues,
} from "@/data/labToolExamples";
import { NotImplementedBanner, SectionHeader } from "@/pages/tools/ToolLayout";
import type { TabMeta } from "@/pages/tools/toolsPageModel";

export function TaxonomyTab({ meta }: { meta: TabMeta }) {
  const [accInput, setAccInput] = useState("");

  const mutation = useMutation({
    mutationFn: () => {
      const accessions = accInput
        .split(/[\s,;]+/)
        .filter(Boolean)
        .slice(0, 50);
      return taxonomyApi.lookup(accessions);
    },
  });

  const annotations = mutation.data?.annotations ?? {};

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        icon={<Search size={16} strokeWidth={1.5} />}
        title={meta.label}
        subtitle={meta.desc}
      />
      <NotImplementedBanner feature="Taxonomy" />

      <ExamplePicker<TaxonomyExampleValues>
        examples={TAXONOMY_EXAMPLES}
        onSelect={(v) => setAccInput(v.accessions)}
      />

      <div className="form-row" style={{ marginBottom: 16 }}>
        <label className="form-label">
          Accessions (space, comma, or newline separated; max 50)
        </label>
        <textarea
          className="form-input blast-textarea"
          rows={3}
          value={accInput}
          onChange={(e) => setAccInput(e.target.value)}
          placeholder="NR_123456.1 NR_789012.1 XP_001234.2"
        />
      </div>

      <button
        className="btn btn--primary"
        onClick={() => mutation.mutate()}
        disabled={mutation.isPending || !accInput.trim()}
      >
        {mutation.isPending ? (
          <Loader2 size={14} className="spin" />
        ) : (
          <Search size={14} />
        )}{" "}
        Look up
      </button>

      {Object.keys(annotations).length > 0 && (
        <div style={{ marginTop: 20, overflowX: "auto" }}>
          <p className="muted" style={{ fontSize: 12, marginBottom: 8 }}>
            Found {mutation.data?.found} of {mutation.data?.requested}
          </p>
          <table className="table" style={{ width: "100%", fontSize: 12 }}>
            <thead>
              <tr>
                <th>Accession</th>
                <th>Organism</th>
                <th>Title</th>
                <th>Tax ID</th>
                <th>Length</th>
              </tr>
            </thead>
            <tbody>
              {Object.values(annotations).map((a) => (
                <tr key={a.accession}>
                  <td>
                    <code className="code-val">{a.accession}</code>
                  </td>
                  <td style={{ fontWeight: 600 }}>{a.organism}</td>
                  <td
                    className="muted"
                    style={{
                      maxWidth: 320,
                      overflow: "hidden",
                      textOverflow: "ellipsis",
                      whiteSpace: "nowrap",
                    }}
                  >
                    {a.title}
                  </td>
                  <td>{a.taxid}</td>
                  <td>{a.seq_length}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
    </section>
  );
}
