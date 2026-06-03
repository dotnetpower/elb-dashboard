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

// Accumulated paths that actually resolve to a navigable route (see the
// ``<Route>`` table in ``App.tsx``). Section prefixes such as ``/sequence``,
// ``/blast``, ``/blast/databases``, ``/monitor`` and ``/mockups`` have NO index
// route, so linking to them falls through the ``*`` catch-all and bounces the
// user back to the dashboard. Such crumbs must render as plain (non-clickable)
// text instead of a broken link.
const NAVIGABLE_PATHS = new Set<string>([
  "/",
  "/terminal",
  "/tools",
  "/docs",
  "/upgrade",
  "/monitor/live-wall",
  "/blast/submit",
  "/blast/jobs",
  "/blast/databases/build",
]);

// A crumb is navigable when its accumulated path is a known static route or a
// dynamic ``/blast/jobs/:jobId`` job-detail path (3 segments, not the
// ``analytics`` sub-page). Everything else is a non-linkable section prefix.
function isNavigablePath(accPath: string): boolean {
  if (NAVIGABLE_PATHS.has(accPath)) return true;
  const parts = accPath.split("/").filter(Boolean);
  if (parts.length === 3 && parts[0] === "blast" && parts[1] === "jobs") {
    return true;
  }
  return false;
}

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
        const navigable = !isLast && isNavigablePath(c.path);
        return (
          <span key={c.path} style={{ display: "flex", alignItems: "center", gap: 6 }}>
            {i > 0 && <ChevronRight size={10} className="breadcrumb__sep" />}
            {navigable ? (
              <Link to={c.path}>{c.label}</Link>
            ) : (
              <span className={isLast ? "breadcrumb__current" : "breadcrumb__section"}>
                {c.label}
              </span>
            )}
          </span>
        );
      })}
    </nav>
  );
}
