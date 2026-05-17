import { useState } from "react";
import { AlertTriangle, Filter, History, Pencil, Star, ToggleLeft, X } from "lucide-react";

import {
  hasStructuredTaxidOptionConflict,
  parsePositiveTaxid,
} from "@/pages/blastSubmitModel";
import type { TaxonomyFilterSectionProps } from "@/pages/blastSubmit/types";
import { SectionHeader, Tip } from "@/pages/blastSubmit/ui";
import {
  TaxonomyModal,
  type TaxonomyModalValue,
} from "@/pages/blastSubmit/TaxonomyModal";
import { useRecentTaxonomy } from "@/pages/blastSubmit/useRecentTaxonomy";
import {
  topCommonTaxa,
  type CommonTaxon,
} from "@/pages/blastSubmit/taxonomyCommon";

const QUICK_PICK_LIMIT = 5;

export function TaxonomyFilterSection({ form, set }: TaxonomyFilterSectionProps) {
  const [modalOpen, setModalOpen] = useState(false);
  const recent = useRecentTaxonomy();

  const taxidValue = parsePositiveTaxid(form.taxid);
  const taxidInvalid = form.taxid.trim().length > 0 && taxidValue === null;
  const hasConflict =
    form.taxid.trim().length > 0 &&
    hasStructuredTaxidOptionConflict(form.additional_options);

  const selectedLabel = form.taxid_label || (taxidValue ? `taxid ${taxidValue}` : "");

  const initial: TaxonomyModalValue = {
    taxid: form.taxid,
    taxid_label: form.taxid_label,
    taxid_rank: form.taxid_rank,
    is_inclusive: form.is_inclusive,
  };

  const applyValue = (value: TaxonomyModalValue) => {
    set("taxid", value.taxid);
    set("taxid_label", value.taxid_label);
    set("taxid_rank", value.taxid_rank);
    set("is_inclusive", value.is_inclusive);
  };

  const clearSelection = () => {
    set("taxid", "");
    set("taxid_label", "");
    set("taxid_rank", "");
  };

  // One-click apply from a recent chip — bypasses the modal entirely and
  // preserves the row's stored include/exclude mode.
  const pickRecent = (taxid: number) => {
    const row = recent.entries.find((r) => r.taxid === taxid);
    if (!row) return;
    applyValue({
      taxid: String(row.taxid),
      taxid_label: row.scientific_name,
      taxid_rank: row.rank ?? "",
      is_inclusive: row.is_inclusive,
    });
    recent.push({
      taxid: row.taxid,
      scientific_name: row.scientific_name,
      common_name: row.common_name ?? null,
      rank: row.rank ?? null,
      is_inclusive: row.is_inclusive,
    });
  };

  // One-click apply from a curated "popular" chip. Defaults to include-mode
  // which matches researcher intent ~95% of the time; user can flip via the
  // modal afterwards.
  const pickCommon = (taxon: CommonTaxon) => {
    applyValue({
      taxid: String(taxon.taxid),
      taxid_label: taxon.scientific_name,
      taxid_rank: taxon.rank,
      is_inclusive: true,
    });
    recent.push({
      taxid: taxon.taxid,
      scientific_name: taxon.scientific_name,
      common_name: taxon.common_name,
      rank: taxon.rank,
      is_inclusive: true,
    });
  };

  const recentChips = recent.entries.slice(0, QUICK_PICK_LIMIT);
  const commonChips = topCommonTaxa(QUICK_PICK_LIMIT);

  return (
    <section className="glass-card blast-section">
      <SectionHeader
        step={4}
        icon={<Filter size={16} strokeWidth={1.5} />}
        title="Taxonomy Filter"
        subtitle="Limit the search to one NCBI taxon"
      />

      <div className="taxonomy-filter-launcher">
        <div className="taxonomy-filter-launcher__copy">
          <span className="glass-label">
            Filter scope <Tip text="Open a modal to search NCBI Taxonomy and preview the candidate's lineage before applying." />
          </span>
          {taxidValue ? (
            <div className="taxonomy-filter-launcher__selected">
              <div className="taxonomy-filter-launcher__selected-main">
                <strong>{selectedLabel}</strong>
                <span>taxid {taxidValue}</span>
                {form.taxid_rank && <span>{form.taxid_rank}</span>}
                <span
                  className={`taxonomy-filter-launcher__mode taxonomy-filter-launcher__mode--${
                    form.is_inclusive ? "include" : "exclude"
                  }`}
                >
                  {form.is_inclusive ? "include only" : "exclude"}
                </span>
              </div>
              <button
                type="button"
                className="taxonomy-filter-launcher__clear"
                onClick={clearSelection}
                aria-label="Clear taxonomy filter"
                title="Clear taxonomy filter"
              >
                <X size={12} strokeWidth={1.6} />
              </button>
            </div>
          ) : (
            <div className="taxonomy-filter-launcher__empty">
              No taxonomy filter set. BLAST will search every taxon in the database.
            </div>
          )}
        </div>
        <button
          type="button"
          className="glass-button taxonomy-filter-launcher__button"
          onClick={() => setModalOpen(true)}
        >
          {taxidValue ? (
            <>
              <Pencil size={13} strokeWidth={1.6} />
              Change taxon
            </>
          ) : (
            <>
              <Filter size={13} strokeWidth={1.6} />
              Choose taxon
            </>
          )}
        </button>
      </div>

      {recentChips.length > 0 && (
        <div className="taxonomy-filter-quickpick">
          <div className="taxonomy-filter-quickpick__head">
            <History size={11} strokeWidth={1.6} />
            <span>Recent</span>
          </div>
          <div className="taxonomy-filter-quickpick__chips">
            {recentChips.map((row) => {
              const isActive = taxidValue === row.taxid;
              return (
                <button
                  key={`recent-${row.taxid}`}
                  type="button"
                  className={`taxonomy-filter-quickpick__chip${
                    isActive ? " taxonomy-filter-quickpick__chip--active" : ""
                  }`}
                  onClick={() => pickRecent(row.taxid)}
                  title={`${row.scientific_name} · taxid ${row.taxid}${
                    row.is_inclusive ? " · include" : " · exclude"
                  }`}
                >
                  <span className="taxonomy-filter-quickpick__chip-name">
                    {row.scientific_name}
                  </span>
                  <span className="taxonomy-filter-quickpick__chip-taxid">
                    {row.taxid}
                  </span>
                  {!row.is_inclusive && (
                    <span className="taxonomy-filter-quickpick__chip-flag">
                      exclude
                    </span>
                  )}
                </button>
              );
            })}
          </div>
        </div>
      )}

      {recentChips.length === 0 && (
        <div className="taxonomy-filter-quickpick">
          <div className="taxonomy-filter-quickpick__head">
            <Star size={11} strokeWidth={1.6} />
            <span>Popular</span>
          </div>
          <div className="taxonomy-filter-quickpick__chips">
            {commonChips.map((taxon) => {
              const isActive = taxidValue === taxon.taxid;
              return (
                <button
                  key={`common-${taxon.taxid}`}
                  type="button"
                  className={`taxonomy-filter-quickpick__chip${
                    isActive ? " taxonomy-filter-quickpick__chip--active" : ""
                  }`}
                  onClick={() => pickCommon(taxon)}
                  title={`${taxon.scientific_name} · taxid ${taxon.taxid}`}
                >
                  <span className="taxonomy-filter-quickpick__chip-name">
                    {taxon.scientific_name}
                  </span>
                  <span className="taxonomy-filter-quickpick__chip-taxid">
                    {taxon.taxid}
                  </span>
                </button>
              );
            })}
          </div>
        </div>
      )}

      {/* Inline filter mode — always visible so researchers don't need to
          open the modal just to toggle include ↔ exclude. */}
      <div className="taxonomy-filter-mode">
        <div className="taxonomy-filter-mode__head">
          <ToggleLeft size={11} strokeWidth={1.6} />
          <span>Filter mode</span>
        </div>
        <div
          className="taxonomy-filter-mode__segmented"
          role="group"
          aria-label="Taxonomy filter mode"
        >
          <button
            type="button"
            className={`taxonomy-filter-mode__seg${form.is_inclusive ? " taxonomy-filter-mode__seg--active" : ""}`}
            onClick={() => set("is_inclusive", true)}
            aria-pressed={form.is_inclusive}
          >
            Include only
          </button>
          <button
            type="button"
            className={`taxonomy-filter-mode__seg${!form.is_inclusive ? " taxonomy-filter-mode__seg--active taxonomy-filter-mode__seg--exclude" : ""}`}
            onClick={() => set("is_inclusive", false)}
            aria-pressed={!form.is_inclusive}
          >
            Exclude
          </button>
        </div>
      </div>

      {taxidInvalid && (
        <div className="blast-warning-box">
          <AlertTriangle size={14} />
          Taxonomy taxid must be a positive integer.
        </div>
      )}

      <div className="blast-taxonomy-note">
        <AlertTriangle size={13} />
        Taxonomy filtering requires the selected BLAST database to include taxonomy metadata.
      </div>

      {hasConflict && (
        <div className="blast-warning-box">
          <AlertTriangle size={14} />
          Remove -taxids or -negative_taxids from Additional options before using this structured filter.
        </div>
      )}

      <TaxonomyModal
        open={modalOpen}
        initial={initial}
        onApply={applyValue}
        onClose={() => setModalOpen(false)}
      />
    </section>
  );
}

              