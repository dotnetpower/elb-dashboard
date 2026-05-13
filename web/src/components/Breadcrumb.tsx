import { Link, useLocation } from "react-router-dom";
import { ChevronRight } from "lucide-react";

const ROUTE_LABELS: Record<string, string> = {
  "": "Dashboard",
  terminal: "Remote Terminal",
  blast: "BLAST",
  submit: "New Search",
  jobs: "Jobs",
  docs: "API Reference",
  databases: "Databases",
  build: "Custom DB",
  tools: "Lab Tools",
  analytics: "Analytics",
};

export function Breadcrumb() {
  const { pathname } = useLocation();
  const parts = pathname.split("/").filter(Boolean);

  if (parts.length === 0) return null;

  const crumbs: { label: string; path: string }[] = [{ label: "Dashboard", path: "/" }];

  let acc = "";
  for (const part of parts) {
    acc += `/${part}`;
    let label = ROUTE_LABELS[part];
    // #28: Show truncated ID for job detail pages instead of full UUID
    if (!label && part.startsWith("job-")) {
      label = part.slice(0, 12) + "…";
    }
    crumbs.push({ label: label ?? part, path: acc });
  }

  return (
    <nav className="breadcrumb" aria-label="Breadcrumb">
      {crumbs.map((c, i) => {
        const isLast = i === crumbs.length - 1;
        return (
          <span key={c.path} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {i > 0 && <ChevronRight size={10} className="breadcrumb__sep" />}
            {isLast ? (
              <span className="breadcrumb__current">{c.label}</span>
            ) : (
              <Link to={c.path}>{c.label}</Link>
            )}
          </span>
        );
      })}
    </nav>
  );
}
