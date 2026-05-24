import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate } from "react-router-dom";

import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import { AlgorithmParametersSection } from "@/pages/blastSubmit/AlgorithmParametersSection";
import { BlastSubmitFooter } from "@/pages/blastSubmit/BlastSubmitFooter";
import { BlastSubmitHeader } from "@/pages/blastSubmit/BlastSubmitHeader";
import { ComputeSection } from "@/pages/blastSubmit/ComputeSection";
import { DatabaseSection } from "@/pages/blastSubmit/DatabaseSection";
import { OptimizeSection } from "@/pages/blastSubmit/OptimizeSection";
import { ProgramSection } from "@/pages/blastSubmit/ProgramSection";
import { QuerySection } from "@/pages/blastSubmit/QuerySection";
import { SubmitStepper } from "@/pages/blastSubmit/SubmitStepper";
import { SubmitSummaryRail } from "@/pages/blastSubmit/SubmitSummaryRail";
import { TaxonomyFilterSection } from "@/pages/blastSubmit/TaxonomyFilterSection";
import {
  PENDING_DUPLICATE_KEY,
  type ExportableFormFields,
} from "@/pages/blastSubmit/configSerializer";
import { deriveSubmitValidation } from "@/pages/blastSubmit/submitValidation";
import {
  deriveShardingAvailability,
  reconcileShardingSelection,
} from "@/pages/blastSubmit/shardingAvailability";
import { useClusterSelection } from "@/pages/blastSubmit/useClusterSelection";
import { useDbWithWarmupPlan } from "@/pages/blastSubmit/useDbWithWarmupPlan";
import { useDraftForm } from "@/pages/blastSubmit/useDraftForm";
import { usePreFlight } from "@/pages/blastSubmit/usePreFlight";
import {
  buildEffectiveAdditionalOptions,
  buildSubmitRequest,
  useSubmitMutation,
} from "@/pages/blastSubmit/useSubmitMutation";
import { useWarmupStatus } from "@/pages/blastSubmit/useWarmupStatus";
import { parsePositiveTaxid, PROGRAMS } from "@/pages/blastSubmitModel";
import { isAksWorkloadReady } from "@/utils/aksStatus";
import {
  AUTO_WARMUP_PREFS_EVENT,
  readAutoWarmupDbs,
} from "@/components/cards/storage/autoWarmupPrefs";

export function BlastSubmit() {
  const fileInputRef = useRef<HTMLInputElement>(null);
  const { form, setForm, set, reset, clearDraft, lastSavedAt } = useDraftForm();
  const [showParams, setShowParams] = useState(false);
  const [autoWarmupDbs, setAutoWarmupDbs] = useState<Set<string>>(() =>
    readAutoWarmupDbs(),
  );
  const [activeStep, setActiveStep] = useState(2); // default focus on Query
  const [now, setNow] = useState(() => Date.now());
  const sectionRefs = useRef<Record<number, HTMLDivElement | null>>({});
  const navigate = useNavigate();
  const { toast } = useToast();

  // One-shot hydration from a "Duplicate / Re-run" handoff. The
  // BlastJobHeader stashes a snapshot under PENDING_DUPLICATE_KEY before
  // navigating here; we consume it once on mount and clear the slot so a
  // page reload doesn't re-apply stale values.
  useEffect(() => {
    let raw: string | null = null;
    try {
      raw = window.sessionStorage.getItem(PENDING_DUPLICATE_KEY);
    } catch {
      // Storage disabled (private mode etc.) — nothing to do.
      return;
    }
    if (!raw) return;
    try {
      window.sessionStorage.removeItem(PENDING_DUPLICATE_KEY);
    } catch {
      /* ignore */
    }
    try {
      const parsed = JSON.parse(raw) as {
        source?: { jobId?: string; jobTitle?: string };
        form?: ExportableFormFields;
      };
      if (parsed && parsed.form && typeof parsed.form === "object") {
        setForm((current) => ({ ...current, ...parsed.form }));
        const label = parsed.source?.jobTitle || parsed.source?.jobId || "previous job";
        toast(`Loaded configuration from ${label}.`, "success");
      }
    } catch (err) {
      toast(
        `Could not load duplicated config: ${
          err instanceof Error ? err.message : "invalid handoff"
        }`,
        "error",
      );
    }
    // Mount-only — re-applying on every render would defeat the one-shot
    // semantics and trample the researcher's edits.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  const [config] = useState(() => loadSavedConfig());
  const subId = config?.subscriptionId ?? "";
  const workloadRg = config?.workloadResourceGroup ?? "";
  const storageAccount = config?.storageAccountName ?? "";
  const acrRg = config?.acrResourceGroup ?? "";
  const acrName = config?.acrName ?? "";
  const region = config?.region ?? "koreacentral";

  const programMeta = PROGRAMS.find((p) => p.value === form.program) ?? PROGRAMS[0];
  const selectedTaxid = parsePositiveTaxid(form.taxid);

  const { clusterQuery, clusters, selectedCluster } = useClusterSelection({
    subId,
    workloadRg,
    form,
    setForm,
  });

  const { warmupQuery, warmDbs, selectedDbShortName, isDbAlreadyWarm, warmDbInfo } =
    useWarmupStatus({
      subId,
      workloadRg,
      selectedCluster,
      formDb: form.db,
    });
  const autoWarmupSelected = autoWarmupDbs.has(selectedDbShortName);

  useEffect(() => {
    const refreshAutoWarmupDbs = () => setAutoWarmupDbs(readAutoWarmupDbs());
    window.addEventListener(AUTO_WARMUP_PREFS_EVENT, refreshAutoWarmupDbs);
    window.addEventListener("storage", refreshAutoWarmupDbs);
    return () => {
      window.removeEventListener(AUTO_WARMUP_PREFS_EVENT, refreshAutoWarmupDbs);
      window.removeEventListener("storage", refreshAutoWarmupDbs);
    };
  }, []);

  // Database listing + warmup-feasibility plan, scoped to the selected
  // cluster's topology. See `useDbWithWarmupPlan` for the contract.
  const { dbQuery, selectedDbInfo, selectedDbPlan, warmupBlocked } = useDbWithWarmupPlan({
    subId,
    storageAccount,
    workloadRg,
    selectedCluster,
    selectedDbShortName,
    warmupRequested: form.enable_warmup && !isDbAlreadyWarm,
  });
  const dbShardSets = selectedDbInfo?.shard_sets ?? [];
  const warmupStatusLoading = Boolean(
    selectedCluster && isAksWorkloadReady(selectedCluster) && warmupQuery.isLoading,
  );
  const runtimeDataLoading = Boolean(
    clusterQuery.isLoading || dbQuery.isLoading || warmupStatusLoading,
  );

  const shardingAvailability = useMemo(
    () =>
      deriveShardingAvailability({
        cluster: selectedCluster,
        database: selectedDbInfo,
        isDbAlreadyWarm,
        outfmt: form.outfmt,
      }),
    [selectedCluster, selectedDbInfo, isDbAlreadyWarm, form.outfmt],
  );
  const shardingBlockedReason = !shardingAvailability.options[form.sharding_mode].enabled
    ? shardingAvailability.options[form.sharding_mode].reason
    : null;
  const effectiveShardingMode = shardingAvailability.options[form.sharding_mode].enabled
    ? form.sharding_mode
    : shardingAvailability.preferredMode;
  const effectiveShardingEnabled = effectiveShardingMode !== "off";

  useEffect(() => {
    if (runtimeDataLoading) return;
    setForm((current) =>
      reconcileShardingSelection({
        form: current,
        availability: shardingAvailability,
        isDbAlreadyWarm,
        autoWarmupSelected,
      }),
    );
  }, [
    autoWarmupSelected,
    isDbAlreadyWarm,
    runtimeDataLoading,
    setForm,
    shardingAvailability,
  ]);

  const submitMutation = useSubmitMutation({ navigate, toast, clearDraft });

  const { preFlightResult, preFlightMutation } = usePreFlight({
    toast,
    payload: () => ({
      subscription_id: subId,
      resource_group: workloadRg,
      acr_resource_group: acrRg || undefined,
      acr_name: acrName || undefined,
      storage_account: storageAccount,
      aks_cluster_name: selectedCluster?.name || "",
      db: form.db,
      query_data: form.query_data || undefined,
      additional_options: buildEffectiveAdditionalOptions(form),
      taxid: selectedTaxid ?? undefined,
      is_inclusive: selectedTaxid ? form.is_inclusive : undefined,
      allow_approximate_sharding: effectiveShardingMode === "approximate" || undefined,
      db_auto_partition: effectiveShardingEnabled,
      db_total_bytes: selectedDbInfo?.total_bytes,
      db_total_letters: selectedDbInfo?.total_letters,
      db_effective_search_space: selectedDbInfo?.web_blast_searchsp,
      disable_sharding: !effectiveShardingEnabled,
      enable_warmup: form.enable_warmup,
      evalue: form.evalue,
      max_target_seqs: form.max_target_seqs,
      outfmt: form.outfmt,
      low_complexity_filter: form.low_complexity_filter,
      shard_sets:
        effectiveShardingEnabled && dbShardSets.length > 0 ? dbShardSets : undefined,
      sharding_mode: effectiveShardingMode,
      word_size: form.word_size ? parseInt(form.word_size, 10) : undefined,
    }),
  });

  const validation = deriveSubmitValidation({
    form,
    programMeta,
    subId,
    workloadRg,
    storageAccount,
    selectedCluster,
    dbQueryData: dbQuery.data,
    dbQueryIsSuccess: dbQuery.isSuccess,
    warmupBlocked,
    selectedDbPlan,
    shardingBlockedReason,
    dataLoading: runtimeDataLoading,
    submitPending: submitMutation.isPending,
  });

  const handleSubmit = () => {
    if (!selectedCluster) return;
    if (!validation.canSubmit) {
      toast(
        validation.missing[0]?.text ?? "Complete the required BLAST submit fields first.",
        "error",
      );
      return;
    }
    if (warmupBlocked) {
      // Defence in depth — the Run BLAST button is already disabled when
      // the planner says no, but a keyboard / programmatic activation
      // could slip through. Surface the planner verdict immediately.
      toast(
        `Warmup blocked by feasibility planner: ${selectedDbPlan?.message ?? "infeasible"}`,
        "error",
      );
      return;
    }
    try {
      submitMutation.mutate(
        buildSubmitRequest({
          form,
          selectedCluster,
          subId,
          workloadRg,
          storageAccount,
          acrRg,
          acrName,
          region,
          dbTotalLetters: selectedDbInfo?.total_letters,
          dbTotalBytes: selectedDbInfo?.total_bytes,
          dbEffectiveSearchSpace: selectedDbInfo?.web_blast_searchsp,
          dbShardSets,
        }),
      );
    } catch (error) {
      toast(
        error instanceof Error ? error.message : "BLAST submit form is invalid.",
        "error",
      );
    }
  };

  // Ctrl/Cmd + Enter submits from anywhere in the form. Researchers move
  // between the query textarea and the options sidebar a lot; the shortcut
  // saves a precise mouse trip to the "Run BLAST" button at the bottom.
  useEffect(() => {
    const onKeyDown = (event: KeyboardEvent) => {
      if ((event.ctrlKey || event.metaKey) && event.key === "Enter") {
        event.preventDefault();
        handleSubmit();
      }
    };
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
    // handleSubmit closes over fresh form/validation state on every render,
    // so the listener naturally tracks the latest values.
  });

  // Re-render the "Saved Ns ago" label every 15s.
  useEffect(() => {
    const tick = () => {
      if (!document.hidden) setNow(Date.now());
    };
    const t = window.setInterval(tick, 15_000);
    document.addEventListener("visibilitychange", tick);
    return () => {
      window.clearInterval(t);
      document.removeEventListener("visibilitychange", tick);
    };
  }, []);

  const scrollToStep = (step: number) => {
    setActiveStep(step);
    sectionRefs.current[step]?.scrollIntoView({ behavior: "smooth", block: "start" });
  };

  return (
    <div className="blast-page">
      {/* B layout header — spans full width above the grid */}
      <BlastSubmitHeader
        programMeta={programMeta}
        readySteps={validation.readySteps}
        readyCount={validation.readyCount}
        onReset={reset}
      />

      <div className="bsl-grid">
        {/* ── Left: grouped stepper ────────────────────────────── */}
        <SubmitStepper
          readySteps={validation.readySteps}
          activeStep={activeStep}
          onStepClick={scrollToStep}
        />

        {/* ── Center: form sections with group dividers ────────── */}
        <main className="bsl-center">
          {/* ─ Input group (steps 1–4) ──────────────────────── */}
          <div className="bsl-divider">
            <span className="bsl-divider__label--input">
              ① Input · What are you searching?
            </span>
            <hr />
          </div>

          <div
            ref={(el) => {
              sectionRefs.current[1] = el;
            }}
            onFocus={() => setActiveStep(1)}
          >
            <ProgramSection form={form} set={set} programMeta={programMeta} />
          </div>

          <div
            ref={(el) => {
              sectionRefs.current[2] = el;
            }}
            onFocus={() => setActiveStep(2)}
          >
            <DatabaseSection
              form={form}
              set={set}
              programMeta={programMeta}
              databases={dbQuery.data?.databases}
              dbLoading={dbQuery.isLoading}
              warmDbs={warmDbs}
              warmupKnown={warmupQuery.isSuccess}
              dbWarning={validation.dbWarning}
              dbMissingFromStorage={validation.dbMissingFromStorage}
              dbBaseName={validation.dbBaseName}
            />
          </div>

          <div
            ref={(el) => {
              sectionRefs.current[3] = el;
            }}
            onFocus={() => setActiveStep(3)}
          >
            <QuerySection
              form={form}
              set={set}
              fileInputRef={fileInputRef}
              toast={toast}
              isFasta={validation.seqStats.isFasta}
              seqCount={validation.seqStats.seqCount}
              charCount={validation.seqStats.charCount}
            />
          </div>

          <div
            ref={(el) => {
              sectionRefs.current[4] = el;
            }}
            onFocus={() => setActiveStep(4)}
          >
            <TaxonomyFilterSection form={form} set={set} />
          </div>

          {/* ─ Runtime group (steps 5–7) ────────────────────── */}
          <div className="bsl-divider">
            <span className="bsl-divider__label--runtime">
              ② Runtime · How should it run?
            </span>
            <hr />
          </div>

          <div
            ref={(el) => {
              sectionRefs.current[5] = el;
            }}
            onFocus={() => setActiveStep(5)}
          >
            <OptimizeSection form={form} set={set} programMeta={programMeta} />
          </div>

          <div
            ref={(el) => {
              sectionRefs.current[6] = el;
            }}
            onFocus={() => setActiveStep(6)}
          >
            <ComputeSection
              subId={subId}
              workloadRg={workloadRg}
              clusters={clusters}
              selectedCluster={selectedCluster}
              clusterLoading={clusterQuery.isLoading}
              runtimeLoading={runtimeDataLoading}
              form={form}
              set={set}
              isDbAlreadyWarm={isDbAlreadyWarm}
              warmDbInfo={warmDbInfo}
              selectedDbShortName={selectedDbShortName}
              dbShardSets={dbShardSets}
              warmupPlan={selectedDbPlan}
              shardingAvailability={shardingAvailability}
            />
          </div>

          <div
            ref={(el) => {
              sectionRefs.current[7] = el;
            }}
            onFocus={() => setActiveStep(7)}
          >
            <AlgorithmParametersSection
              form={form}
              set={set}
              showParams={showParams}
              setShowParams={setShowParams}
              paramsSummary={validation.paramsSummary}
              programMeta={programMeta}
              webBlastSearchsp={selectedDbInfo?.web_blast_searchsp}
              webBlastSearchspScope={selectedDbInfo?.web_blast_searchsp_scope}
            />
          </div>

          {/* Mobile-only footer (hidden by grid on desktop) */}
          <div className="bsl-mobile-footer">
            <BlastSubmitFooter
              form={form}
              set={set}
              programMeta={programMeta}
              toast={toast}
              missing={validation.missing}
              searchSummary={validation.searchSummary}
              canSubmit={validation.canSubmit}
              submitPending={submitMutation.isPending}
              submitError={submitMutation.isError ? submitMutation.error : null}
              preFlightResult={preFlightResult}
              preFlightPending={preFlightMutation.isPending}
              effectiveSearchSpace={selectedDbInfo?.web_blast_searchsp}
              lastSavedAt={lastSavedAt}
              onPreFlight={() => preFlightMutation.mutate()}
              onSubmit={handleSubmit}
            />
          </div>
        </main>

        {/* ── Right: summary rail ──────────────────────────────── */}
        <SubmitSummaryRail
          form={form}
          programMeta={programMeta}
          toast={toast}
          readySteps={validation.readySteps}
          readyCount={validation.readyCount}
          missing={validation.missing}
          searchSummary={validation.searchSummary}
          paramsSummary={validation.paramsSummary}
          canSubmit={validation.canSubmit}
          submitPending={submitMutation.isPending}
          preFlightResult={preFlightResult}
          preFlightPending={preFlightMutation.isPending}
          effectiveSearchSpace={selectedDbInfo?.web_blast_searchsp}
          lastSavedAt={lastSavedAt}
          set={set}
          onPreFlight={() => preFlightMutation.mutate()}
          onSubmit={handleSubmit}
          now={now}
        />
      </div>
    </div>
  );
}
