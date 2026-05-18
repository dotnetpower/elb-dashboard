/**
 * A1 — API Reference left sidebar.
 *
 * Provides a sticky index with:
 *  - a free-text search across endpoint path + summary,
 *  - HTTP method filter chips,
 *  - a tag list (jumps to <section id="tag-…">), and
 *  - a flat endpoint list (jumps to <article id="ep-…">), filtered by both
 *    search and method chips.
 *
 * The sidebar does not own any of the spec parsing — it consumes the same
 * `grouped` shape ApiReference already computes. Navigation uses anchor
 * `<a href="#…">` so the existing EndpointCard hashchange handler (A2)
 * auto-expands the target card.
 */
import { useId, useMemo, useState } from "react";
import { Filter, Hash, Search, X } from "lucide-react";

import { MethodBadge } from "@/pages/apiReference/MethodBadge";
import type { SpecEndpoint } from "@/pages/apiReference/types";

const METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE"] as const;

export interface ApiReferenceSidebarGroup {
  tag: { name: string; description?: string };
  endpoints: SpecEndpoint[];
}

export function ApiReferenceSidebar({ groups }: { groups: ApiReferenceSidebarGroup[] }) {
  const inputId = useId();
  const [search, setSearch] = useState("");
  const [methodFilter, setMethodFilter] = useState<Set<string>>(() => new Set());

  const toggleMethod = (m: string) => {
    setMethodFilter((prev) => {
      const next = new Set(prev);
      if (next.has(m)) next.delete(m);
      else next.add(m);
      return next;
    });
  };

  // Pre-build a flat list once per `groups` change so search filtering is O(n).
  const flat = useMemo(() => {
    const rows: { tagName: string; ep: SpecEndpoint; id: string }[] = [];
    for (const { tag, endpoints } of groups) {
      for (const ep of endpoints) {
        rows.push({
          tagName: tag.name,
          ep,
          id: `ep-${ep.method}-${ep.path.replace(/\//g, "-")}`,
        });
      }
    }
    return rows;
  }, [groups]);

  const filtered = useMemo(() => {
    const needle = search.trim().toLowerCase();
    return flat.filter(({ ep }) => {
      if (methodFilter.size > 0 && !methodFilter.has(ep.method.toUpperCase())) {
        return false;
      }
      if (!needle) return true;
      const hay = `${ep.path} ${ep.summary ?? ""} ${ep.tags.join(" ")}`.toLowerCase();
      return hay.includes(needle);
    });
  }, [flat, methodFilter, search]);

  const totalCount = flat.length;
  const matchCount = filtered.length;

  return (
    <aside
      className="api-reference-sidebar glass-card"
      aria-label="API endpoint navigation"
      style={{
        position: "sticky",
        top: 16,
        alignSelf: "flex-start",
        maxHeight: "calc(100vh - 32px)",
        overflowY: "auto",
        padding: 12,
        display: "flex",
        flexDirection: "column",
        gap: 10,
        minWidth: 240,
        maxWidth: 280,
      }}
    >
      <div style={{ display: "flex", alignItems: "center", gap: 6 }}>
        <Hash size={13} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
        <strong style={{ fontSize: 12, color: "var(--text-muted)" }}>Endpoints</strong>
        <span style={{ marginLeft: "auto", fontSize: 11, color: "var(--text-faint)" }}>
          {matchCount}/{totalCount}
        </span>
      </div>

      {/* Search box */}
      <label
        htmlFor={inputId}
        style={{
          display: "flex",
          alignItems: "center",
          gap: 6,
          padding: "6px 8px",
          background: "var(--bg-secondary)",
          border: "1px solid var(--border-weak)",
          borderRadius: 8,
        }}
      >
        <Search size={12} strokeWidth={1.5} style={{ color: "var(--text-faint)" }} />
        <input
          id={inputId}
          type="search"
          placeholder="Search path or summary…"
          value={search}
          onChange={(e) => setSearch(e.target.value)}
          style={{
            border: "none",
            background: "transparent",
            color: "var(--text-primary)",
            fontSize: 12,
            outline: "none",
            flex: 1,
            minWidth: 0,
          }}
        />
        {search && (
          <button
            type="button"
            onClick={() => setSearch("")}
            aria-label="Clear search"
            style={{
              background: "none",
              border: "none",
              padding: 0,
              cursor: "pointer",
              color: "var(--text-faint)",
              display: "inline-flex",
            }}
          >
            <X size={12} strokeWidth={1.5} />
          </button>
        )}
      </label>

      {/* Method filter chips */}
      <div style={{ display: "flex", gap: 4, flexWrap: "wrap" }}>
        <Filter size={11} strokeWidth={1.5} style={{ color: "var(--text-faint)", alignSelf: "center" }} />
        {METHODS.map((m) => {
          const active = methodFilter.has(m);
          return (
            <button
              key={m}
              type="button"
              onClick={() => toggleMethod(m)}
              aria-pressed={active}
              style={{
                padding: "2px 6px",
                borderRadius: 6,
                fontSize: 10,
                fontWeight: 600,
                letterSpacing: 0.3,
                border: `1px solid ${active ? "var(--accent)" : "var(--border-weak)"}`,
                background: active ? "rgba(122,167,255,0.18)" : "transparent",
                color: active ? "var(--accent)" : "var(--text-muted)",
                cursor: "pointer",
              }}
            >
              {m}
            </button>
          );
        })}
      </div>

      {/* Tag list (always shown when no search) */}
      {!search && methodFilter.size === 0 && (
        <nav aria-label="Tags" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
          <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 4, marginBottom: 2 }}>
            BY TAG
          </div>
          {groups.map(({ tag, endpoints }) => (
            <a
              key={tag.name}
              href={`#tag-${tag.name}`}
              style={{
                display: "flex",
                justifyContent: "space-between",
                gap: 6,
                padding: "4px 6px",
                borderRadius: 6,
                fontSize: 12,
                color: "var(--text-primary)",
                textDecoration: "none",
              }}
            >
              <span style={{ overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>
                {tag.name}
              </span>
              <span style={{ color: "var(--text-faint)", fontSize: 10 }}>{endpoints.length}</span>
            </a>
          ))}
        </nav>
      )}

      {/* Flat endpoint list (always shown; filtered) */}
      <nav aria-label="Endpoints" style={{ display: "flex", flexDirection: "column", gap: 2 }}>
        <div style={{ fontSize: 10, color: "var(--text-faint)", marginTop: 4, marginBottom: 2 }}>
          ENDPOINTS
        </div>
        {filtered.length === 0 && (
          <div style={{ fontSize: 11, color: "var(--text-faint)", padding: "4px 6px" }}>
            No matches.
          </div>
        )}
        {filtered.map(({ ep, id }) => (
          <a
            key={id}
            href={`#${id}`}
            title={ep.summary ?? ep.path}
            style={{
              display: "flex",
              alignItems: "center",
              gap: 6,
              padding: "3px 6px",
              borderRadius: 6,
              fontSize: 11,
              color: "var(--text-muted)",
              textDecoration: "none",
              minWidth: 0,
            }}
          >
            <MethodBadge method={ep.method} size="sm" />
            <span
              style={{
                overflow: "hidden",
                textOverflow: "ellipsis",
                whiteSpace: "nowrap",
                fontFamily: "var(--font-mono, monospace)",
                fontSize: 10.5,
              }}
            >
              {ep.path}
            </span>
          </a>
        ))}
      </nav>
    </aside>
  );
}
