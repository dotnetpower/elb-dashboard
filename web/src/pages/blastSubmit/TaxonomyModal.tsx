import {
  type KeyboardEvent,
  type MouseEvent,
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
} from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  CheckCircle2,
  Loader2,
  Microscope,
  Search,
  Sparkles,
  Trash2,
  X,
} from "lucide-react";

import { blastApi } from "@/api/endpoints";
import type {
  TaxonomyDetail,
  TaxonomyImageResponse,
  TaxonomyLineageNode,
  TaxonomySearchResult,
} from "@/api/blast";
import { formatApiError } from "@/api/client";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { parsePositiveTaxid } from "@/pages/blastSubmitModel";

import {
  RECENT_TAXONOMY_MAX_ENTRIES,
  type RecentTaxonomyEntry,
  useRecentTaxonomy,
} from "@/pages/blastSubmit/useRecentTaxonomy";
import {
  filterCommonTaxa,
  getCommonTaxon,
  topCommonTaxa,
  type CommonTaxon,
} from "@/pages/blastSubmit/taxonomyCommon";
import { TaxonomyDefaultIcon } from "@/pages/blastSubmit/TaxonomyDefaultIcon";
import { LineageTree } from "@/pages/blastSubmit/LineageTree";

const TAXONOMY_RESULT_LIMIT = 8;
const MAX_TAXONOMY_QUERY_CHARS = 120;
const TAXONOMY_SEARCH_DEBOUNCE_MS = 400;
const DETAIL_STALE_MS = 24 * 60 * 60 * 1000;

export interface TaxonomyModalValue {
  taxid: string;
  taxid_label: string;
  taxid_rank: string;
  is_inclusive: boolean;
}

interface TaxonomyModalProps {
  open: boolean;
  initial: TaxonomyModalValue;
  onApply: (value: TaxonomyModalValue) => void;
  onClose: () => void;
}

interface FocusedCandidate {
  taxid: number;
  scientific_name: string;
  common_name?: string | null;
  rank?: string | null;
  matched_name?: string | null;
}

function normaliseSearchText(value: string): string {
  return value.trim().replace(/\s+/g, " ");
}

function canSearchTaxonomy(value: string): boolean {
  if (!value || value.length > MAX_TAXONOMY_QUERY_CHARS) return false;
  if (/^\d+$/.test(value)) return parsePositiveTaxid(value) !== null;
  return value.length >= 2;
}

function candidateFromResult(row: TaxonomySearchResult): FocusedCandidate {
  return {
    taxid: row.taxid,
    scientific_name: row.scientific_name,
    common_name: row.common_name ?? null,
    rank: row.rank,
    matched_name: row.matched_name,
  };
}

function candidateFromCommon(row: CommonTaxon): FocusedCandidate {
  return {
    taxid: row.taxid,
    scientific_name: row.scientific_name,
    common_name: row.common_name,
    rank: row.rank,
    matched_name: null,
  };
}

function candidateFromRecent(row: RecentTaxonomyEntry): FocusedCandidate {
  return {
    taxid: row.taxid,
    scientific_name: row.scientific_name,
    common_name: row.common_name ?? null,
    rank: row.rank ?? null,
    matched_name: null,
  };
}

function candidateFromInitial(value: TaxonomyModalValue): FocusedCandidate | null {
  const taxid = parsePositiveTaxid(value.taxid);
  if (!taxid) return null;
  return {
    taxid,
    scientific_name: value.taxid_label || `taxid ${taxid}`,
    common_name: null,
    rank: value.taxid_rank || null,
    matched_name: null,
  };
}

function formatLineage(detail: TaxonomyDetail | undefined): string {
  if (!detail) return "";
  if (detail.lineage_ex.length > 0) {
    return detail.lineage_ex.map((n) => n.scientific_name).join(" › ");
  }
  return detail.lineage || "";
}

function previewCommand(value: TaxonomyModalValue): string {
  const taxid = parsePositiveTaxid(value.taxid);
  if (!taxid) return "(no filter)";
  return value.is_inclusive ? `-taxids ${taxid}` : `-negative_taxids ${taxid}`;
}

export function TaxonomyModal({ open, initial, onApply, onClose }: TaxonomyModalProps) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);
  const searchInputRef = useRef<HTMLInputElement>(null);
  const recent = useRecentTaxonomy();

  const [searchText, setSearchText] = useState("");
  const [submittedQuery, setSubmittedQuery] = useState("");
  const [focused, setFocused] = useState<FocusedCandidate | null>(() =>
    candidateFromInitial(initial),
  );

  // Reset modal-local state whenever it opens with a possibly-new selection.
  // Depend on primitive fields of `initial` (not the object reference) so
  // unrelated parent re-renders don't blow away the user's in-modal focus.
  useEffect(() => {
    if (!open) return;
    setSearchText("");
    setSubmittedQuery("");
    setFocused(candidateFromInitial(initial));
    // Focus the search box after the modal mounts.
    queueMicrotask(() => searchInputRef.current?.focus());
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [
    open,
    initial.taxid,
    initial.taxid_label,
    initial.taxid_rank,
    initial.is_inclusive,
  ]);

  // Escape closes (cancels) the modal.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: globalThis.KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const normalisedSearchText = normaliseSearchText(searchText);
  const query = submittedQuery.trim();
  const searchTooLong = normalisedSearchText.length > MAX_TAXONOMY_QUERY_CHARS;
  const searchTaxidInvalid =
    /^\d+$/.test(normalisedSearchText) &&
    parsePositiveTaxid(normalisedSearchText) === null;
  const searchReady = canSearchTaxonomy(normalisedSearchText);

  // Debounced submit of the search text.
  useEffect(() => {
    if (!searchReady) return;
    const handle = window.setTimeout(() => {
      setSubmittedQuery(normalisedSearchText);
    }, TAXONOMY_SEARCH_DEBOUNCE_MS);
    return () => window.clearTimeout(handle);
  }, [normalisedSearchText, searchReady]);

  const searchQuery = useQuery({
    queryKey: ["blast-taxonomy-search", query, TAXONOMY_RESULT_LIMIT],
    queryFn: () => blastApi.searchTaxonomy(query, TAXONOMY_RESULT_LIMIT),
    enabled: open && query.length > 0,
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  const results = useMemo<TaxonomySearchResult[]>(
    () => searchQuery.data?.results ?? [],
    [searchQuery.data],
  );

  // Instant client-side autocomplete from the curated catalogue. Computed on
  // every keystroke (no debounce) so the user sees suggestions immediately
  // while the live E-utilities search is still in flight.
  const localMatches = useMemo<CommonTaxon[]>(() => {
    if (!normalisedSearchText) return [];
    return filterCommonTaxa(normalisedSearchText, 6);
  }, [normalisedSearchText]);
  const popularTaxa = useMemo(() => topCommonTaxa(8), []);

  // Merge curated + live results, de-dupe by taxid (curated wins for ordering
  // when both contain the same row). Used for keyboard navigation + display.
  type MergedRow =
    | { kind: "common"; taxon: CommonTaxon }
    | { kind: "result"; row: TaxonomySearchResult };

  const mergedResults = useMemo<MergedRow[]>(() => {
    const seen = new Set<number>();
    const out: MergedRow[] = [];
    for (const taxon of localMatches) {
      if (seen.has(taxon.taxid)) continue;
      seen.add(taxon.taxid);
      out.push({ kind: "common", taxon });
    }
    for (const row of results) {
      if (seen.has(row.taxid)) continue;
      seen.add(row.taxid);
      out.push({ kind: "result", row });
    }
    return out;
  }, [localMatches, results]);
  const searchHasNoMatch =
    normalisedSearchText.length > 0 &&
    searchReady &&
    !searchQuery.isFetching &&
    searchQuery.isFetched &&
    mergedResults.length === 0;

  // Auto-focus the first merged result once a fresh search lands. Prefer the
  // top curated match when present (it appears first anyway and is the
  // higher-confidence suggestion for popular organisms).
  useEffect(() => {
    if (mergedResults.length === 0) return;
    setFocused((prev) => {
      if (
        prev &&
        mergedResults.some(
          (r) => (r.kind === "common" ? r.taxon.taxid : r.row.taxid) === prev.taxid,
        )
      ) {
        return prev;
      }
      const first = mergedResults[0];
      return first.kind === "common"
        ? candidateFromCommon(first.taxon)
        : candidateFromResult(first.row);
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [mergedResults.length, searchQuery.data]);

  const detailQuery = useQuery({
    queryKey: ["blast-taxonomy-detail", focused?.taxid ?? 0],
    queryFn: () => blastApi.getTaxonomyDetail(focused!.taxid),
    enabled: open && focused !== null,
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  const imageQuery = useQuery({
    queryKey: ["blast-taxonomy-image", focused?.scientific_name ?? ""],
    queryFn: () => blastApi.getTaxonomyImage(focused!.scientific_name),
    enabled: open && focused !== null && Boolean(focused.scientific_name),
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  const treeQuery = useQuery({
    queryKey: ["blast-taxonomy-tree", focused?.taxid ?? 0],
    queryFn: () => blastApi.getTaxonomyTree(focused!.taxid, 3),
    enabled: open && focused !== null,
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  const focusableTaxids = useMemo<number[]>(() => {
    const ids: number[] = [];
    const seen = new Set<number>();
    for (const row of mergedResults) {
      const taxid = row.kind === "common" ? row.taxon.taxid : row.row.taxid;
      if (!seen.has(taxid)) {
        ids.push(taxid);
        seen.add(taxid);
      }
    }
    if (ids.length === 0) {
      for (const row of recent.entries) {
        if (!seen.has(row.taxid)) {
          ids.push(row.taxid);
          seen.add(row.taxid);
        }
      }
    }
    return ids;
  }, [mergedResults, recent.entries]);

  const moveFocus = useCallback(
    (delta: 1 | -1) => {
      if (focusableTaxids.length === 0) return;
      const currentIdx = focused ? focusableTaxids.indexOf(focused.taxid) : -1;
      const nextIdx =
        currentIdx === -1
          ? 0
          : (currentIdx + delta + focusableTaxids.length) % focusableTaxids.length;
      const nextTaxid = focusableTaxids[nextIdx];
      const fromMerged = mergedResults.find(
        (r) => (r.kind === "common" ? r.taxon.taxid : r.row.taxid) === nextTaxid,
      );
      if (fromMerged) {
        setFocused(
          fromMerged.kind === "common"
            ? candidateFromCommon(fromMerged.taxon)
            : candidateFromResult(fromMerged.row),
        );
        return;
      }
      const fromRecent = recent.entries.find((r) => r.taxid === nextTaxid);
      if (fromRecent) setFocused(candidateFromRecent(fromRecent));
    },
    [focusableTaxids, focused, mergedResults, recent.entries],
  );

  const applyCandidate = useCallback(
    (candidate: FocusedCandidate) => {
      const value: TaxonomyModalValue = {
        taxid: String(candidate.taxid),
        taxid_label: candidate.scientific_name,
        taxid_rank: candidate.rank ?? "",
        is_inclusive: initial.is_inclusive,
      };
      recent.push({
        taxid: candidate.taxid,
        scientific_name: candidate.scientific_name,
        common_name: candidate.common_name ?? null,
        rank: candidate.rank ?? null,
        is_inclusive: initial.is_inclusive,
      });
      onApply(value);
      onClose();
    },
    [initial.is_inclusive, onApply, onClose, recent],
  );

  const apply = useCallback(() => {
    if (!focused) return;
    applyCandidate(focused);
  }, [applyCandidate, focused]);

  const clearAndClose = useCallback(() => {
    onApply({ taxid: "", taxid_label: "", taxid_rank: "", is_inclusive: true });
    onClose();
  }, [onApply, onClose]);

  const handleBackdropClick = useCallback(
    (event: MouseEvent<HTMLDivElement>) => {
      if (event.target === event.currentTarget) onClose();
    },
    [onClose],
  );

  const handleSearchKey = useCallback(
    (event: KeyboardEvent<HTMLInputElement>) => {
      if (event.key === "ArrowDown") {
        event.preventDefault();
        moveFocus(1);
      } else if (event.key === "ArrowUp") {
        event.preventDefault();
        moveFocus(-1);
      } else if (event.key === "Enter") {
        if (searchReady) setSubmittedQuery(normalisedSearchText);
        if (focused) apply();
      }
    },
    [apply, focused, moveFocus, normalisedSearchText, searchReady],
  );

  if (!open) return null;

  const detail = detailQuery.data;
  const lineageText = formatLineage(detail);
  const synonyms = (detail?.synonyms ?? []).slice(0, 6);
  const equivalentNames = (detail?.equivalent_names ?? []).slice(0, 4);
  const detailValue: TaxonomyModalValue = focused
    ? {
        taxid: String(focused.taxid),
        taxid_label: focused.scientific_name,
        taxid_rank: focused.rank ?? "",
        is_inclusive: initial.is_inclusive,
      }
    : { taxid: "", taxid_label: "", taxid_rank: "", is_inclusive: initial.is_inclusive };

  return (
    <div
      className="glass-dialog-backdrop taxonomy-modal__backdrop"
      onClick={handleBackdropClick}
      role="dialog"
      aria-modal="true"
      aria-label="Choose taxonomy filter"
      ref={trapRef}
    >
      <div
        className="glass-card glass-card--strong taxonomy-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="taxonomy-modal__head">
          <div className="taxonomy-modal__title">
            <Microscope size={14} strokeWidth={1.6} />
            <span>Choose Taxonomy Filter</span>
          </div>
          <div className="taxonomy-modal__head-right">
            <kbd className="taxonomy-modal__esc">Esc</kbd>
            <button
              type="button"
              className="taxonomy-modal__close"
              onClick={onClose}
              aria-label="Close taxonomy filter"
            >
              <X size={14} strokeWidth={1.6} />
            </button>
          </div>
        </header>

        <div className="taxonomy-modal__body">
          <div className="taxonomy-modal__split">
            {/* ── Column 1: search + recent + results ── */}
            <div className="taxonomy-modal__left">
              <div className="taxonomy-modal__search">
                <Search
                  size={13}
                  strokeWidth={1.6}
                  className="taxonomy-modal__search-icon"
                  aria-hidden="true"
                />
                <input
                  ref={searchInputRef}
                  type="text"
                  className="glass-input taxonomy-modal__search-input"
                  value={searchText}
                  onChange={(event) => {
                    setSearchText(event.target.value);
                    setFocused(null);
                  }}
                  onKeyDown={handleSearchKey}
                  placeholder="Search NCBI Taxonomy (e.g. Homo sapiens or 9606)"
                  spellCheck={false}
                  aria-label="Taxonomy search"
                />
                {searchQuery.isFetching && (
                  <Loader2
                    size={13}
                    className="spin taxonomy-modal__search-spinner"
                    aria-hidden="true"
                  />
                )}
              </div>

              {searchTooLong && (
                <div className="taxonomy-modal__warning">
                  Search must be {MAX_TAXONOMY_QUERY_CHARS} characters or fewer.
                </div>
              )}
              {searchTaxidInvalid && (
                <div className="taxonomy-modal__warning">
                  Numeric search must be a positive integer.
                </div>
              )}

              {recent.entries.length > 0 && !normalisedSearchText && (
                <div className="taxonomy-modal__section">
                  <div className="taxonomy-modal__section-head">
                    <span>Recent</span>
                    <button
                      type="button"
                      className="taxonomy-modal__link"
                      onClick={recent.clear}
                      aria-label="Clear recent taxonomy list"
                    >
                      <Trash2 size={11} strokeWidth={1.6} /> Clear
                    </button>
                  </div>
                  <div className="taxonomy-modal__chips">
                    {recent.entries.map((row) => {
                      const isFocused = focused?.taxid === row.taxid;
                      const candidate = candidateFromRecent(row);
                      return (
                        <button
                          key={row.taxid}
                          type="button"
                          className={`taxonomy-modal__chip${
                            isFocused ? " taxonomy-modal__chip--active" : ""
                          }`}
                          onClick={() => setFocused(candidate)}
                          onDoubleClick={() => {
                            setFocused(candidate);
                            applyCandidate(candidate);
                          }}
                          title={`${row.scientific_name} · taxid ${row.taxid}`}
                        >
                          <span className="taxonomy-modal__chip-name">
                            {row.scientific_name}
                          </span>
                          <span className="taxonomy-modal__chip-taxid">{row.taxid}</span>
                        </button>
                      );
                    })}
                  </div>
                </div>
              )}

              {searchQuery.isError && (
                <div className="taxonomy-modal__error" role="alert">
                  <AlertTriangle size={13} strokeWidth={1.6} />
                  Search failed: {formatApiError(searchQuery.error, "blast")}
                </div>
              )}

              {normalisedSearchText && mergedResults.length > 0 && (
                <div className="taxonomy-modal__section">
                  <div className="taxonomy-modal__section-head">
                    <span>
                      Suggestions · {mergedResults.length}
                      {localMatches.length > 0 && (
                        <span className="taxonomy-modal__section-badge">
                          {localMatches.length} curated
                        </span>
                      )}
                    </span>
                    <span className="taxonomy-modal__hint">
                      <kbd>↑</kbd>
                      <kbd>↓</kbd>
                      <kbd>↵</kbd>
                    </span>
                  </div>
                  <ul
                    className="taxonomy-modal__results"
                    role="listbox"
                    aria-label="Taxonomy suggestions"
                  >
                    {mergedResults.map((row) => {
                      const taxid =
                        row.kind === "common" ? row.taxon.taxid : row.row.taxid;
                      const name =
                        row.kind === "common"
                          ? row.taxon.scientific_name
                          : row.row.scientific_name;
                      const commonName =
                        row.kind === "common"
                          ? row.taxon.common_name
                          : row.row.common_name;
                      const rank = row.kind === "common" ? row.taxon.rank : row.row.rank;
                      const meta =
                        row.kind === "common"
                          ? rank
                          : `${rank}${row.row.division ? ` · ${row.row.division}` : ""}`;
                      const isCommon =
                        row.kind === "common" || getCommonTaxon(row.row.taxid) !== null;
                      const isFocused = focused?.taxid === taxid;
                      const candidate =
                        row.kind === "common"
                          ? candidateFromCommon(row.taxon)
                          : candidateFromResult(row.row);
                      const pick = () => setFocused(candidate);
                      return (
                        <li key={`${row.kind}-${taxid}`}>
                          <button
                            type="button"
                            role="option"
                            aria-selected={isFocused}
                            className={`taxonomy-modal__result${
                              isFocused ? " taxonomy-modal__result--focused" : ""
                            }`}
                            onClick={pick}
                            onDoubleClick={() => {
                              pick();
                              applyCandidate(candidate);
                            }}
                          >
                            <span
                              className={`taxonomy-modal__result-dot${
                                isCommon ? " taxonomy-modal__result-dot--curated" : ""
                              }`}
                              aria-hidden="true"
                            >
                              {isCommon && <Sparkles size={9} strokeWidth={2} />}
                            </span>
                            <span className="taxonomy-modal__result-main">
                              <span className="taxonomy-modal__result-title">
                                {name}
                                {commonName && <small> {commonName}</small>}
                              </span>
                              <span className="taxonomy-modal__result-meta">
                                {meta}
                                {isCommon && (
                                  <span className="taxonomy-modal__result-badge">
                                    curated
                                  </span>
                                )}
                              </span>
                            </span>
                            <span className="taxonomy-modal__result-taxid">{taxid}</span>
                          </button>
                        </li>
                      );
                    })}
                  </ul>
                </div>
              )}

              {searchHasNoMatch && (
                <div className="taxonomy-modal__error" role="alert">
                  <AlertTriangle size={13} strokeWidth={1.6} />
                  No valid taxonomy match found for &ldquo;{normalisedSearchText}&rdquo;.
                </div>
              )}

              {!normalisedSearchText && recent.entries.length === 0 && (
                <div className="taxonomy-modal__section taxonomy-modal__popular">
                  <div className="taxonomy-modal__section-head">
                    <span>Popular</span>
                    <span className="taxonomy-modal__footer-recent">Quick pick</span>
                  </div>
                  <div className="taxonomy-modal__chips">
                    {popularTaxa.map((taxon) => {
                      const isFocused = focused?.taxid === taxon.taxid;
                      const candidate = candidateFromCommon(taxon);
                      const pick = () => setFocused(candidate);
                      return (
                        <button
                          key={taxon.taxid}
                          type="button"
                          className={`taxonomy-modal__chip${
                            isFocused ? " taxonomy-modal__chip--active" : ""
                          }`}
                          onClick={pick}
                          onDoubleClick={() => {
                            pick();
                            applyCandidate(candidate);
                          }}
                          title={`${taxon.scientific_name} · taxid ${taxon.taxid}`}
                        >
                          <span className="taxonomy-modal__chip-name">
                            {taxon.common_name ?? taxon.scientific_name}
                          </span>
                          <span className="taxonomy-modal__chip-taxid">
                            {taxon.taxid}
                          </span>
                        </button>
                      );
                    })}
                  </div>
                  <div className="taxonomy-modal__muted">
                    Search an organism name or NCBI taxid to find more taxa.
                  </div>
                </div>
              )}
            </div>

            {/* ── Column 2: text detail ── */}
            <div className="taxonomy-modal__right">
              <TaxonomyDetailPanel
                focused={focused}
                detail={detail}
                detailLoading={detailQuery.isLoading}
                detailError={detailQuery.error}
                lineageText={lineageText}
                synonyms={synonyms}
                equivalentNames={equivalentNames}
                siblings={treeQuery.data?.siblings}
                siblingsLoading={treeQuery.isLoading}
              />
            </div>

            {/* ── Column 3: image preview ── */}
            <div className="taxonomy-modal__image-col">
              <div className="taxonomy-modal__section-head">
                <span>Preview</span>
              </div>
              <TaxonomyImagePanel
                focused={focused}
                image={imageQuery.data}
                imageLoading={imageQuery.isLoading}
              />
            </div>
          </div>
        </div>

        <footer className="taxonomy-modal__footer">
          <div className="taxonomy-modal__footer-meta">
            Preview:{" "}
            <span className="taxonomy-modal__command">{previewCommand(detailValue)}</span>
            <span className="taxonomy-modal__footer-recent">
              {recent.entries.length}/{RECENT_TAXONOMY_MAX_ENTRIES} recent
            </span>
          </div>
          <div className="taxonomy-modal__footer-actions">
            <button
              type="button"
              className="glass-button glass-button--ghost"
              onClick={clearAndClose}
              aria-label="Clear taxonomy filter"
            >
              Clear filter
            </button>
            <button type="button" className="glass-button" onClick={onClose}>
              Cancel
            </button>
            <button
              type="button"
              className="glass-button glass-button--primary"
              onClick={apply}
              disabled={!focused}
            >
              <CheckCircle2 size={13} strokeWidth={1.6} />
              Apply
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

interface ImagePanelProps {
  focused: FocusedCandidate | null;
  image: TaxonomyImageResponse | undefined;
  imageLoading: boolean;
}

function TaxonomyImagePanel({ focused, image, imageLoading }: ImagePanelProps) {
  if (!focused) {
    return (
      <div className="taxonomy-modal__image taxonomy-modal__image--empty">
        <TaxonomyDefaultIcon
          className="taxonomy-modal__image-default"
          ariaLabel="No taxon selected"
        />
        <span>Select a taxon to see its preview.</span>
      </div>
    );
  }

  if (imageLoading) {
    return (
      <div className="taxonomy-modal__image taxonomy-modal__image--loading">
        <Loader2 size={20} className="spin" aria-hidden="true" />
        <span>Loading image…</span>
      </div>
    );
  }

  if (image?.image_url) {
    return (
      <figure className="taxonomy-modal__image">
        <img
          src={image.image_url}
          alt={`${focused.scientific_name} (Wikipedia)`}
          loading="lazy"
          referrerPolicy="no-referrer"
        />
        <figcaption className="taxonomy-modal__image-caption">
          <span>Wikipedia thumbnail</span>
          {image.page_url && (
            <a
              href={image.page_url}
              target="_blank"
              rel="noopener noreferrer"
              className="taxonomy-modal__image-link"
            >
              Open ↗
            </a>
          )}
        </figcaption>
      </figure>
    );
  }

  // No Wikipedia image → show the curated SVG fallback so the column still
  // feels intentional rather than empty/broken.
  return (
    <div className="taxonomy-modal__image taxonomy-modal__image--fallback">
      <TaxonomyDefaultIcon
        className="taxonomy-modal__image-default"
        ariaLabel={`No image for ${focused.scientific_name}`}
      />
      <span>No reference image available</span>
    </div>
  );
}

interface DetailPanelProps {
  focused: FocusedCandidate | null;
  detail: TaxonomyDetail | undefined;
  detailLoading: boolean;
  detailError: unknown;
  lineageText: string;
  synonyms: string[];
  equivalentNames: string[];
  siblings: Record<string, TaxonomyLineageNode[]> | undefined;
  siblingsLoading: boolean;
}

function TaxonomyDetailPanel({
  focused,
  detail,
  detailLoading,
  detailError,
  lineageText,
  synonyms,
  equivalentNames,
  siblings,
  siblingsLoading,
}: DetailPanelProps) {
  if (!focused) {
    return (
      <div className="taxonomy-modal__detail taxonomy-modal__detail--empty">
        <Microscope size={20} strokeWidth={1.4} />
        <div>Pick a result on the left to preview its lineage.</div>
      </div>
    );
  }

  return (
    <div className="taxonomy-modal__detail">
      <div className="taxonomy-modal__detail-head">
        <div className="taxonomy-modal__detail-name">{focused.scientific_name}</div>
        <div className="taxonomy-modal__detail-sub">
          {focused.common_name ? `Common: ${focused.common_name} · ` : ""}
          taxid {focused.taxid}
          {focused.rank ? ` · ${focused.rank}` : ""}
        </div>
      </div>

      {detailLoading && (
        <div className="taxonomy-modal__muted">
          <Loader2 size={12} className="spin" /> Loading detail…
        </div>
      )}
      {detailError !== null && detailError !== undefined && (
        <div className="taxonomy-modal__error" role="alert">
          <AlertTriangle size={12} strokeWidth={1.6} />
          {formatApiError(detailError, "blast")}
        </div>
      )}

      {detail && (
        <>
          <LineageTree
            nodes={detail.lineage_ex}
            selectedTaxid={focused.taxid}
            lineageText={lineageText}
            siblings={siblings}
            siblingsLoading={siblingsLoading}
          />
          <dl className="taxonomy-modal__detail-grid">
            {detail.parent_taxid !== null && (
              <>
                <dt>Parent</dt>
                <dd className="taxonomy-modal__detail-mono">
                  taxid {detail.parent_taxid}
                </dd>
              </>
            )}
            {detail.authority && (
              <>
                <dt>Authority</dt>
                <dd>{detail.authority}</dd>
              </>
            )}
            {synonyms.length > 0 && (
              <>
                <dt>Synonyms</dt>
                <dd>{synonyms.join(", ")}</dd>
              </>
            )}
            {equivalentNames.length > 0 && (
              <>
                <dt>Also known as</dt>
                <dd>{equivalentNames.join(", ")}</dd>
              </>
            )}
            {detail.genetic_code && (
              <>
                <dt>Genetic code</dt>
                <dd>
                  {detail.genetic_code}
                  {detail.mito_genetic_code ? ` · mito: ${detail.mito_genetic_code}` : ""}
                </dd>
              </>
            )}
            {detail.update_date && (
              <>
                <dt>Updated</dt>
                <dd className="taxonomy-modal__detail-mono">{detail.update_date}</dd>
              </>
            )}
          </dl>
        </>
      )}
    </div>
  );
}
