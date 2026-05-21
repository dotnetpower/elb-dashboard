/**
 * Read-only NCBI Taxonomy detail modal.
 *
 * Mounted next to the Descriptions table (BlastHitsTable) and opened when
 * the user clicks a Scientific Name cell. Resolves taxid via
 * `searchTaxonomy(name, 1)` when the row didn't carry one, then fetches
 * `getTaxonomyDetail`, `getTaxonomyImage`, and (optionally) the lineage
 * tree in parallel. All endpoints have 24 h server-side caches plus a
 * React Query staleTime of 24 h, so re-opening the same taxon costs no
 * additional eutils calls.
 *
 * Design decisions (per agent recommendation, 2026-05-22):
 *  - Centred modal layout (consistent with TaxonomyModal).
 *  - LineageTree nodes link to the NCBI Taxonomy Browser in a new tab
 *    (no in-modal stack, no back button).
 *  - Wikipedia thumbnail when available; TaxonomyDefaultIcon fallback
 *    otherwise so the image slot never collapses to an empty hole.
 *  - "Full record" link is a small icon next to the taxid (no large
 *    footer CTA).
 */

import { useEffect, useMemo, useRef } from "react";
import { useQuery } from "@tanstack/react-query";
import {
  AlertTriangle,
  ExternalLink,
  Loader2,
  Microscope,
  X,
} from "lucide-react";

import type {
  TaxonomyDetail,
  TaxonomyImageResponse,
  TaxonomySearchResponse,
  TaxonomyTreeResponse,
} from "@/api/blast";
import { blastApi } from "@/api/endpoints";
import { formatApiError } from "@/api/client";
import { useFocusTrap } from "@/hooks/useFocusTrap";
import { LineageTree } from "@/pages/blastSubmit/LineageTree";
import { TaxonomyDefaultIcon } from "@/pages/blastSubmit/TaxonomyDefaultIcon";

const DETAIL_STALE_MS = 24 * 60 * 60 * 1000;

export interface TaxonomyDetailModalProps {
  open: boolean;
  /** Display label used for the header + Wikipedia lookup. */
  scientificName: string;
  /** Pre-resolved NCBI taxid, when the caller already has one. */
  taxid?: number | null;
  /**
   * Hint about how the calling row obtained the scientific name. When
   * `"stitle"` (i.e. we parsed it heuristically from the BLAST stitle),
   * the modal warns the user that the resolution may be approximate.
   */
  organismSource?: "sscinames" | "stitle" | string | null;
  onClose: () => void;
}

function ncbiTaxonomyBrowserUrl(taxid: number): string {
  return `https://www.ncbi.nlm.nih.gov/Taxonomy/Browser/wwwtax.cgi?id=${taxid}`;
}

function displayRank(rank: string | null | undefined): string | null {
  const value = rank?.trim();
  if (!value || value.toLowerCase() === "no rank") return null;
  return value;
}

function formatNcbiDate(value: string | null | undefined): string {
  if (!value) return "";
  return value.split(/\s+/)[0] ?? value;
}

export function TaxonomyDetailModal({
  open,
  scientificName,
  taxid,
  organismSource,
  onClose,
}: TaxonomyDetailModalProps) {
  const trapRef = useFocusTrap<HTMLDivElement>(open);
  const closeButtonRef = useRef<HTMLButtonElement | null>(null);
  const trimmedName = scientificName.trim();

  // Resolve taxid by name when the caller didn't provide one.
  const searchQuery = useQuery<TaxonomySearchResponse>({
    queryKey: ["taxon-detail-modal-search", trimmedName],
    queryFn: () => blastApi.searchTaxonomy(trimmedName, 1),
    enabled: open && !taxid && trimmedName.length > 0,
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  const resolvedTaxid: number | null = useMemo(() => {
    if (taxid && taxid > 0) return taxid;
    const first = searchQuery.data?.results?.[0];
    return first ? first.taxid : null;
  }, [taxid, searchQuery.data]);

  const detailQuery = useQuery<TaxonomyDetail>({
    queryKey: ["taxon-detail-modal-detail", resolvedTaxid ?? 0],
    queryFn: () => blastApi.getTaxonomyDetail(resolvedTaxid!),
    enabled: open && resolvedTaxid !== null,
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  const imageQuery = useQuery<TaxonomyImageResponse>({
    queryKey: ["taxon-detail-modal-image", trimmedName],
    queryFn: () => blastApi.getTaxonomyImage(trimmedName),
    enabled: open && trimmedName.length > 0,
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  const treeQuery = useQuery<TaxonomyTreeResponse>({
    queryKey: ["taxon-detail-modal-tree", resolvedTaxid ?? 0],
    queryFn: () => blastApi.getTaxonomyTree(resolvedTaxid!, 3),
    enabled: open && resolvedTaxid !== null,
    retry: false,
    staleTime: DETAIL_STALE_MS,
  });

  // Escape closes; focus the close button on open so screen-reader users
  // land on a known control rather than the first lineage link.
  useEffect(() => {
    if (!open) return;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("keydown", onKey);
    queueMicrotask(() => closeButtonRef.current?.focus());
    return () => window.removeEventListener("keydown", onKey);
  }, [open, onClose]);

  const detail = detailQuery.data;
  const rank = displayRank(detail?.rank);
  const updatedDate = formatNcbiDate(detail?.update_date);
  const lineageText = useMemo(() => {
    if (!detail) return "";
    if (detail.lineage_ex.length > 0) {
      return detail.lineage_ex.map((n) => n.scientific_name).join(" › ");
    }
    return detail.lineage || "";
  }, [detail]);

  if (!open) return null;

  const synonyms = (detail?.synonyms ?? []).slice(0, 5);
  const synonymsOverflow = (detail?.synonyms.length ?? 0) - synonyms.length;

  const titleId = "taxon-detail-modal-title";
  const resolving = !taxid && searchQuery.isFetching;
  const notFound =
    !taxid &&
    searchQuery.isFetched &&
    !searchQuery.isFetching &&
    (searchQuery.data?.results?.length ?? 0) === 0;
  const headingFallback = trimmedName || "(unnamed organism)";

  return (
    <div
      className="glass-dialog-backdrop taxonomy-modal__backdrop"
      onClick={(event) => {
        if (event.target === event.currentTarget) onClose();
      }}
      role="dialog"
      aria-modal="true"
      aria-labelledby={titleId}
      ref={trapRef}
    >
      <div
        className="glass-card glass-card--strong taxon-detail-modal"
        onClick={(event) => event.stopPropagation()}
      >
        <header className="taxonomy-modal__head">
          <div className="taxonomy-modal__title" id={titleId}>
            <Microscope size={14} strokeWidth={1.6} />
            <span>{detail?.scientific_name || headingFallback}</span>
            {rank && (
              <span className="taxonomy-modal__section-badge">{rank}</span>
            )}
          </div>
          <div className="taxonomy-modal__head-right">
            <kbd className="taxonomy-modal__esc">Esc</kbd>
            <button
              ref={closeButtonRef}
              type="button"
              className="taxonomy-modal__close"
              onClick={onClose}
              aria-label="Close taxonomy details"
            >
              <X size={14} strokeWidth={1.6} />
            </button>
          </div>
        </header>

        <div className="taxonomy-modal__body taxon-detail-modal__body">
          <div className="taxon-detail-modal__summary">
            {/* Image (Wikipedia or default icon) */}
            <ImagePanel
              scientificName={trimmedName}
              image={imageQuery.data}
              imageLoading={imageQuery.isLoading}
            />

            {/* Key facts (taxid, rank, division, …) */}
            <div className="taxon-detail-modal__summary-main">
              {detail?.common_name && (
                <div className="taxon-detail-modal__common-name">
                  {detail.common_name}
                </div>
              )}

              {resolving && (
                <div className="taxonomy-modal__muted">
                  <Loader2 size={12} className="spin" /> Looking up NCBI taxid…
                </div>
              )}
              {notFound && (
                <div className="taxonomy-modal__error" role="alert">
                  <AlertTriangle size={12} strokeWidth={1.6} />
                  <span>
                    No NCBI Taxonomy match for{" "}
                    <strong>{headingFallback}</strong>.
                    {organismSource === "stitle" && (
                      <>
                        {" "}
                        The name was parsed from the alignment title and may be
                        approximate.
                      </>
                    )}
                  </span>
                </div>
              )}
              {detailQuery.isLoading && resolvedTaxid !== null && (
                <div className="taxonomy-modal__muted">
                  <Loader2 size={12} className="spin" /> Loading taxonomy
                  detail…
                </div>
              )}
              {detailQuery.error && (
                <div className="taxonomy-modal__error" role="alert">
                  <AlertTriangle size={12} strokeWidth={1.6} />
                  {formatApiError(detailQuery.error, "blast")}
                </div>
              )}

              {detail && (
                <div className="taxon-detail-modal__facts" aria-label="Taxonomy facts">
                  <Fact label="Taxid" mono>
                    <span>{detail.taxid}</span>
                    <a
                      href={ncbiTaxonomyBrowserUrl(detail.taxid)}
                      target="_blank"
                      rel="noopener noreferrer"
                      className="taxonomy-modal__link taxon-detail-modal__inline-link"
                      title="Open this taxon on NCBI Taxonomy Browser"
                      aria-label={`Open taxid ${detail.taxid} on NCBI Taxonomy Browser`}
                    >
                      <ExternalLink size={11} strokeWidth={1.6} />
                    </a>
                  </Fact>

                  {detail.division && (
                    <Fact label="Division">{detail.division}</Fact>
                  )}

                  {detail.parent_taxid !== null && (
                    <Fact label="Parent" mono>
                      <span>taxid {detail.parent_taxid}</span>
                        <a
                          href={ncbiTaxonomyBrowserUrl(detail.parent_taxid)}
                          target="_blank"
                          rel="noopener noreferrer"
                          className="taxonomy-modal__link taxon-detail-modal__inline-link"
                          title="Open parent taxon on NCBI"
                          aria-label={`Open parent taxid ${detail.parent_taxid} on NCBI`}
                        >
                          <ExternalLink size={11} strokeWidth={1.6} />
                        </a>
                    </Fact>
                  )}

                  {synonyms.length > 0 && (
                    <Fact label="Synonyms" wide>
                        {synonyms.join(", ")}
                        {synonymsOverflow > 0 && (
                          <span className="muted">
                            {" "}
                            +{synonymsOverflow} more
                          </span>
                        )}
                    </Fact>
                  )}

                  {detail.update_date && (
                    <Fact label="Updated" mono>{updatedDate}</Fact>
                  )}
                </div>
              )}
            </div>
          </div>

          {detail && detail.lineage_ex.length > 0 && (
            <div className="taxon-detail-modal__lineage">
              <LineageTree
                nodes={detail.lineage_ex}
                selectedTaxid={detail.taxid}
                lineageText={lineageText}
                siblings={treeQuery.data?.siblings}
                siblingsLoading={treeQuery.isLoading}
                nodeHref={ncbiTaxonomyBrowserUrl}
                defaultZoom={1}
                minSvgWidth={600}
              />
            </div>
          )}
        </div>

        <footer className="taxonomy-modal__footer taxon-detail-modal__footer">
          <div className="taxonomy-modal__footer-meta">
            {detail ? (
              <>
                Source: NCBI eutils
                {detail.cached ? " · cached" : ""}
                {updatedDate ? ` · updated ${updatedDate}` : ""}
              </>
            ) : (
              <span className="muted">Source: NCBI eutils</span>
            )}
          </div>
          <div className="taxonomy-modal__footer-actions">
            <button
              type="button"
              className="glass-button"
              onClick={onClose}
            >
              Close
            </button>
          </div>
        </footer>
      </div>
    </div>
  );
}

interface FactProps {
  label: string;
  children: React.ReactNode;
  mono?: boolean;
  wide?: boolean;
}

function Fact({ label, children, mono = false, wide = false }: FactProps) {
  return (
    <div className={wide ? "taxon-detail-modal__fact taxon-detail-modal__fact--wide" : "taxon-detail-modal__fact"}>
      <div className="taxon-detail-modal__fact-label">{label}</div>
      <div
        className={
          mono
            ? "taxon-detail-modal__fact-value taxon-detail-modal__fact-value--mono"
            : "taxon-detail-modal__fact-value"
        }
      >
        {children}
      </div>
    </div>
  );
}

interface ImagePanelProps {
  scientificName: string;
  image: TaxonomyImageResponse | undefined;
  imageLoading: boolean;
}

function ImagePanel({ scientificName, image, imageLoading }: ImagePanelProps) {
  if (imageLoading) {
    return (
      <div className="taxon-detail-modal__image taxon-detail-modal__image--loading">
        <Loader2 size={20} className="spin" aria-hidden="true" />
        <span>Loading image…</span>
      </div>
    );
  }

  if (image?.image_url) {
    return (
      <figure className="taxon-detail-modal__image">
        <img
          src={image.image_url}
          alt={`${scientificName || "Taxon"} (Wikipedia)`}
          loading="lazy"
          referrerPolicy="no-referrer"
        />
        <figcaption className="taxon-detail-modal__image-caption">
          <span>Wikipedia</span>
          {image.page_url && (
            <a
              href={image.page_url}
              target="_blank"
              rel="noopener noreferrer"
              className="taxon-detail-modal__image-link"
            >
              Open ↗
            </a>
          )}
        </figcaption>
      </figure>
    );
  }

  return (
    <div className="taxon-detail-modal__image taxon-detail-modal__image--fallback">
      <TaxonomyDefaultIcon
        className="taxon-detail-modal__image-default"
        ariaLabel={
          scientificName
            ? `No image available for ${scientificName}`
            : "No taxonomy image"
        }
      />
      <span>No reference image</span>
    </div>
  );
}

