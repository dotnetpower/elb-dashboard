import { useCallback, useMemo, useState } from "react";
import { Sparkles, Check, AlertTriangle } from "lucide-react";

import type { BlastDatabase } from "@/api/endpoints";
import {
  blastApi,
  type BlastDbRecommendation,
  type BlastDbSuggestion,
  type BlastRecommendGoal,
} from "@/api/blast";
import { buildDatabasePath } from "@/pages/blastSubmit/helpers";
import { isBlastDbReady } from "@/utils/blastDbReady";

// Friendly labels for the backend's SUPPORTED_GOALS. The value is the exact
// enum the oracle accepts; the label is researcher-facing copy. Adding a goal
// means appending here AND in the backend rule table — keep them in sync.
export const GOAL_OPTIONS: Array<{ value: BlastRecommendGoal; label: string }> = [
  { value: "identify", label: "Identify an unknown sequence" },
  { value: "highly_similar", label: "Find near-identical matches" },
  { value: "transcripts", label: "Match mRNA / transcripts" },
  { value: "genomes", label: "Match against genomes" },
  { value: "well_characterized", label: "Prefer curated / annotated records" },
  { value: "comprehensive", label: "Maximum coverage (most sensitive)" },
];

interface DatabaseRecommendPanelProps {
  /** Selected BLAST program — drives the molecule the oracle recommends for. */
  program: string;
  /** Downloaded databases, used to decide whether a suggestion is selectable. */
  databases?: BlastDatabase[];
  /** Optional taxon hint prefilled from the taxonomy filter step. */
  taxonHint?: string;
  /** Apply a database path to the form (only called for downloaded + ready DBs). */
  onSelect: (path: string) => void;
}

/**
 * Find a downloaded, ready-to-search database whose base name matches a
 * suggestion (e.g. "core_nt"). Returns its storage path, or null when the DB
 * is not downloaded yet so the panel can surface a "get it from the Dashboard"
 * hint instead of selecting a path that would block Submit.
 */
export function readyPathForSuggestion(
  databases: BlastDatabase[] | undefined,
  dbName: string,
): string | null {
  const match = databases?.find((db) => db.name === dbName && isBlastDbReady(db));
  return match ? buildDatabasePath(match) : null;
}

function SuggestionRow({
  kind,
  suggestion,
  path,
  onSelect,
}: {
  kind: "recommended" | "alternative";
  suggestion: BlastDbSuggestion;
  path: string | null;
  onSelect: (path: string) => void;
}) {
  return (
    <div className={`blast-db-reco__row blast-db-reco__row--${kind}`}>
      <div className="blast-db-reco__row-head">
        <span className="blast-db-reco__badge">
          {kind === "recommended" ? "Recommended" : "Alternative"}
        </span>
        <span className="blast-db-reco__db">{suggestion.label}</span>
      </div>
      <p className="blast-db-reco__rationale">{suggestion.rationale}</p>
      {path ? (
        <button
          type="button"
          className="blast-db-chip blast-db-reco__use"
          onClick={() => onSelect(path)}
        >
          <Check size={11} />
          <span>Use {suggestion.db}</span>
        </button>
      ) : (
        <span className="blast-db-reco__missing">
          <AlertTriangle size={11} />
          {suggestion.db} is not downloaded yet — get it from the Dashboard.
        </span>
      )}
    </div>
  );
}

/**
 * "Help me choose" panel (R8 database selection oracle). Collapsed by default
 * so it never crowds the database list. On expand the researcher picks a search
 * goal (and optional taxon), and the backend oracle returns one recommended
 * database plus an alternative, each with a plain-language rationale. Selecting
 * a suggestion only writes the form when that database is already downloaded.
 */
export function DatabaseRecommendPanel({
  program,
  databases,
  taxonHint,
  onSelect,
}: DatabaseRecommendPanelProps) {
  const [open, setOpen] = useState(false);
  const [goal, setGoal] = useState<BlastRecommendGoal>("identify");
  const [taxon, setTaxon] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [result, setResult] = useState<BlastDbRecommendation | null>(null);

  const effectiveTaxon = taxon.trim() || (taxonHint ?? "").trim();

  const fetchRecommendation = useCallback(async () => {
    setLoading(true);
    setError(null);
    try {
      const reco = await blastApi.getDatabaseRecommendation({
        program,
        goal,
        taxon: effectiveTaxon || undefined,
      });
      setResult(reco);
    } catch {
      setError("Could not load a recommendation. Try again.");
      setResult(null);
    } finally {
      setLoading(false);
    }
  }, [program, goal, effectiveTaxon]);

  const recommendedPath = useMemo(
    () => (result ? readyPathForSuggestion(databases, result.recommended.db) : null),
    [databases, result],
  );
  const alternativePath = useMemo(
    () => (result ? readyPathForSuggestion(databases, result.alternative.db) : null),
    [databases, result],
  );

  return (
    <div className="blast-db-reco">
      <button
        type="button"
        className="blast-db-reco__toggle"
        aria-expanded={open}
        onClick={() => setOpen((prev) => !prev)}
      >
        <Sparkles size={13} strokeWidth={1.5} />
        <span>Help me choose a database</span>
      </button>
      {open && (
        <div className="blast-db-reco__body">
          <div className="blast-db-reco__controls">
            <label className="blast-db-reco__control">
              <span className="muted">Goal</span>
              <select
                className="glass-input"
                value={goal}
                onChange={(event) => setGoal(event.target.value as BlastRecommendGoal)}
              >
                {GOAL_OPTIONS.map((option) => (
                  <option key={option.value} value={option.value}>
                    {option.label}
                  </option>
                ))}
              </select>
            </label>
            <label className="blast-db-reco__control">
              <span className="muted">Taxon (optional)</span>
              <input
                className="glass-input"
                value={taxon}
                onChange={(event) => setTaxon(event.target.value)}
                placeholder={taxonHint || "e.g. Homo sapiens"}
                spellCheck={false}
              />
            </label>
            <button
              type="button"
              className="blast-db-chip blast-db-reco__run"
              onClick={fetchRecommendation}
              disabled={loading}
            >
              {loading ? "…" : "Recommend"}
            </button>
          </div>
          {error && (
            <div className="blast-warning-box">
              <AlertTriangle size={14} />
              {error}
            </div>
          )}
          {result && !error && (
            <div className="blast-db-reco__results">
              <SuggestionRow
                kind="recommended"
                suggestion={result.recommended}
                path={recommendedPath}
                onSelect={onSelect}
              />
              <SuggestionRow
                kind="alternative"
                suggestion={result.alternative}
                path={alternativePath}
                onSelect={onSelect}
              />
              {result.notes.length > 0 && (
                <ul className="blast-db-reco__notes">
                  {result.notes.map((note, index) => (
                    <li key={index}>{note}</li>
                  ))}
                </ul>
              )}
              <span className="blast-db-reco__version">
                Ruleset {result.ruleset_version}
              </span>
            </div>
          )}
        </div>
      )}
    </div>
  );
}
