import { Link, useLocation } from "react-router-dom";
import { ChevronRight } from "lucide-react";

const ROUTE_LABELS: Record<string, string> = {
  "": "Dashboard",
  terminal: "Terminal",
  blast: "BLAST",
  submit: "New Search",
  jobs: "Recent searches",
  docs: "API Reference",
  databases: "Databases",
  build: "Custom DB",
  tools: "Lab Tools",
  analytics: "Analytics",
};

export function Breadcrumb() {
  const { pathname } = useLocation();
  const parts = pathname.split("/").filter(Boolean);

  // On the dashboard root, still show "Dashboard" as the current crumb so
  // every page in the app has the same header layout (breadcrumb → title
  // → actions). The crumb is non-clickable on root because that *is* the
  // current page.
  if (parts.length === 0) {
    return (
      <nav className="breadcrumb" aria-label="Breadcrumb">
        <span className="breadcrumb__current">Dashboard</span>
      </nav>
    );
  }

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
