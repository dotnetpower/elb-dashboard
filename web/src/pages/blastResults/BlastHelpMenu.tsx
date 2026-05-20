import { useEffect, useRef, useState } from "react";
import { BookOpen, ExternalLink, HelpCircle, X } from "lucide-react";

interface BlastHelpMenuProps {
  /** Display program (blastn / blastp / …) — used to tailor the citation list. */
  program: string | null | undefined;
}

/**
 * NCBI Web BLAST's header has a "How to read this report" link plus the
 * Program "Citation" pop-out. This combined menu ports both into the
 * elb-dashboard header so a researcher writing a paper can find the
 * canonical citations without leaving the page, and a new user can find
 * the official NCBI primer.
 *
 * The menu is purely informational — no telemetry, no auth, no data
 * fetched. Links open in a new tab.
 */
export function BlastHelpMenu({ program }: BlastHelpMenuProps) {
  const [open, setOpen] = useState(false);
  const containerRef = useRef<HTMLSpanElement | null>(null);

  // Close on outside click / Esc — standard pop-out behaviour.
  useEffect(() => {
    if (!open) return;
    const onClick = (event: MouseEvent) => {
      const node = containerRef.current;
      if (node && !node.contains(event.target as Node)) setOpen(false);
    };
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") setOpen(false);
    };
    document.addEventListener("mousedown", onClick);
    document.addEventListener("keydown", onKey);
    return () => {
      document.removeEventListener("mousedown", onClick);
      document.removeEventListener("keydown", onKey);
    };
  }, [open]);

  const programLower = (program ?? "").toLowerCase();

  return (
    <span ref={containerRef} style={{ position: "relative" }}>
      <button
        type="button"
        className="glass-button"
        onClick={() => setOpen((value) => !value)}
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 5,
          fontSize: 12,
        }}
        title="How to read this report · BLAST citations"
        aria-haspopup="menu"
        aria-expanded={open}
      >
        <HelpCircle size={14} strokeWidth={1.5} /> Help
      </button>
      {open && (
        <div
          role="menu"
          style={{
            position: "absolute",
            top: "100%",
            right: 0,
            marginTop: 6,
            zIndex: 20,
            width: 360,
            maxWidth: "calc(100vw - 24px)",
            background: "var(--bg-secondary)",
            border: "1px solid var(--glass-border)",
            borderRadius: 8,
            boxShadow: "0 6px 24px rgba(0,0,0,0.30)",
            padding: 12,
          }}
        >
          <div
            style={{
              display: "flex",
              alignItems: "center",
              justifyContent: "space-between",
              marginBottom: 8,
            }}
          >
            <strong style={{ fontSize: 13 }}>BLAST help & citations</strong>
            <button
              type="button"
              onClick={() => setOpen(false)}
              style={{
                background: "transparent",
                border: 0,
                color: "var(--text-muted)",
                cursor: "pointer",
                padding: 2,
              }}
              title="Close"
            >
              <X size={14} />
            </button>
          </div>

          <section style={{ marginBottom: 10 }}>
            <div
              className="muted"
              style={{
                fontSize: 11,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                marginBottom: 4,
              }}
            >
              How to read this report
            </div>
            <ul style={{ margin: 0, padding: 0, listStyle: "none" }}>
              <HelpLink
                href="https://www.ncbi.nlm.nih.gov/books/NBK62051/"
                label="BLAST report description (NCBI Bookshelf)"
              />
              <HelpLink
                href="https://www.ncbi.nlm.nih.gov/books/NBK279690/"
                label="Reading the BLAST output (NCBI handbook)"
              />
              <HelpLink
                href="https://www.youtube.com/playlist?list=PL7dF9e2qSW0Y7w0LFLZl5x_oP_lFc8nKx"
                label="BLAST help videos (NCBI YouTube playlist)"
              />
            </ul>
          </section>

          <section>
            <div
              className="muted"
              style={{
                fontSize: 11,
                textTransform: "uppercase",
                letterSpacing: "0.04em",
                marginBottom: 4,
              }}
            >
              Cite BLAST in your paper
            </div>
            <CitationBlock
              icon={<BookOpen size={12} strokeWidth={1.5} />}
              text="Altschul, S.F. et al. (1990) Basic local alignment search tool. J. Mol. Biol. 215:403–410."
              href="https://doi.org/10.1016/S0022-2836(05)80360-2"
            />
            <CitationBlock
              icon={<BookOpen size={12} strokeWidth={1.5} />}
              text="Altschul, S.F. et al. (1997) Gapped BLAST and PSI-BLAST: a new generation of protein database search programs. Nucleic Acids Res. 25:3389–3402."
              href="https://doi.org/10.1093/nar/25.17.3389"
            />
            {programLower === "blastn" && (
              <CitationBlock
                icon={<BookOpen size={12} strokeWidth={1.5} />}
                text="Zhang, Z. et al. (2000) A greedy algorithm for aligning DNA sequences. J. Comput. Biol. 7:203–214."
                href="https://doi.org/10.1089/10665270050081478"
              />
            )}
            <CitationBlock
              icon={<BookOpen size={12} strokeWidth={1.5} />}
              text="Camacho, C. et al. (2009) BLAST+: architecture and applications. BMC Bioinformatics 10:421."
              href="https://doi.org/10.1186/1471-2105-10-421"
            />
          </section>

          <p
            className="muted"
            style={{
              fontSize: 10,
              marginTop: 10,
              marginBottom: 0,
              lineHeight: 1.5,
            }}
          >
            ElasticBLAST distributes the BLAST+ binaries unchanged across an AKS
            cluster; please cite both BLAST and BLAST+ if you publish results
            produced through this dashboard.
          </p>
        </div>
      )}
    </span>
  );
}

function HelpLink({ href, label }: { href: string; label: string }) {
  return (
    <li style={{ marginBottom: 4 }}>
      <a
        href={href}
        target="_blank"
        rel="noopener noreferrer"
        style={{
          display: "inline-flex",
          alignItems: "center",
          gap: 4,
          fontSize: 12,
          color: "var(--accent)",
        }}
      >
        {label}
        <ExternalLink size={11} strokeWidth={1.5} />
      </a>
    </li>
  );
}

function CitationBlock({
  icon,
  text,
  href,
}: {
  icon: React.ReactNode;
  text: string;
  href: string;
}) {
  return (
    <a
      href={href}
      target="_blank"
      rel="noopener noreferrer"
      style={{
        display: "flex",
        alignItems: "flex-start",
        gap: 6,
        fontSize: 12,
        color: "var(--text-primary)",
        textDecoration: "none",
        padding: "6px 8px",
        borderRadius: 4,
        marginBottom: 4,
        background: "var(--glass-bg)",
        border: "1px solid transparent",
      }}
      onMouseEnter={(event) => {
        event.currentTarget.style.borderColor =
          "color-mix(in srgb, var(--accent) 40%, transparent)";
      }}
      onMouseLeave={(event) => {
        event.currentTarget.style.borderColor = "transparent";
      }}
    >
      <span style={{ color: "var(--text-muted)", marginTop: 2 }}>{icon}</span>
      <span style={{ flex: 1, lineHeight: 1.5 }}>{text}</span>
      <ExternalLink
        size={11}
        strokeWidth={1.5}
        style={{ color: "var(--text-muted)", flexShrink: 0, marginTop: 3 }}
      />
    </a>
  );
}
