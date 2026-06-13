import { useEffect, useMemo, useRef, useState } from "react";
import { useNavigate, useSearchParams } from "react-router-dom";

import { loadSavedConfig } from "@/components/SetupWizard";
import { useToast } from "@/components/Toast";
import type { BlastProgram } from "@/api/endpoints";
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
import {
  deriveSubmitValidation,
  type MissingItem,
} from "@/pages/blastSubmit/submitValidation";
import {
  deriveFullDbMemoryFit,
  fullDbMemoryWarmingInProgress,
  fullDbMemoryWarmupRemediation,
} from "@/pages/blastSubmit/memoryFit";
import {
  decideProgramSwitch,
  deriveDbAvailabilityByType,
} from "@/pages/blastSubmit/helpers";
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
import { permissionDeniedTooltip } from "@/components/PermissionGate";
import { usePermissions } from "@/hooks/usePermissions";
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
  const [searchParams, setSearchParams] = useSearchParams();
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

  // One-shot prefill from `/sequence/:accession` → "Use in BLAST" or
  // "BLAST range" buttons. We consume the params once and strip them from
  // the URL so a manual edit + reload does not re-apply stale handoff data.
  useEffect(() => {
    const accession = (searchParams.get("accession") || "").trim();
    const from = (searchParams.get("from") || "").trim();
    const to = (searchParams.get("to") || "").trim();
    if (!accession && !from && !to) return;

    // Preserve an in-progress FASTA draft. The backend already rejects
    // `query_accession` + `query_data` with 422 `conflicting_query_sources`,
    // so we MUST NOT silently wipe the inline FASTA on handoff — the
    // researcher might have spent time pasting it. Read the draft from the
    // outer-scope `form` (this effect runs once on mount, so `form` is the
    // initial state hydrated by `useDraftForm`).
    const hasExistingDraft = (form.query_data || "").trim().length > 0;
    const applyAccession = accession && !hasExistingDraft;
    // `from`/`to` are coordinates against the handed-off accession. Applying
    // them to a researcher's existing inline FASTA draft would silently mis-
    // range the submission, so we gate the range on the same flag as the
    // accession itself. Standalone `from`/`to` (no accession) is still
    // honoured for the manual sub-range entry path.
    const applyRange = applyAccession || !accession;

    setForm((current) => ({
      ...current,
      ...(applyAccession
        ? {
            query_accession: accession,
            query_data: "",
            // Give the handed-off accession a descriptive default title so the
            // summary rail does not read as an empty job. The researcher can
            // still override it; an empty title falls back to the auto title.
            ...(current.job_title.trim() ? null : { job_title: `BLAST ${accession}` }),
          }
        : null),
      ...(applyRange && from ? { query_from: from } : null),
      ...(applyRange && to ? { query_to: to } : null),
    }));
    if (applyAccession) {
      toast(`Loaded NCBI accession ${accession} into the query field.`, "info");
    } else if (accession && hasExistingDraft) {
      toast(
        `Kept your existing FASTA draft; ignoring accession handoff for ${accession}.` +
          " Clear the query box first if you want to use the accession instead.",
        "info",
      );
    }
    const next = new URLSearchParams(searchParams);
    next.delete("accession");
    next.delete("from");
    next.delete("to");
    setSearchParams(next, { replace: true });
    // Mount-only; we consume the URL handoff once.
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
    form,
    setForm,
  });

  const {
    warmupQuery,
    warmDbs,
    selectedDbShortName,
    isDbAlreadyWarm,
    isWarmupStatusResolved,
    warmDbInfo,
    isDbWarming,
    warmupProgressPct,
  } =
    useWarmupStatus({
      subId,
      // The k8s warmup-status endpoint resolves the cluster via
      // `list_aks_clusters(cred, sub, rg)`. The cluster picker is
      // subscription-wide, so we must send the cluster's *actual* RG —
      // not the workspace anchor RG — otherwise the lookup fails when
      // the user picks a tier living outside the anchor.
      workloadRg: selectedCluster?.resource_group || workloadRg,
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

  // Which molecule types have a ready DB downloaded — gates the program tabs.
  const dbAvailableByType = useMemo(
    () => deriveDbAvailabilityByType(dbQuery.data?.databases),
    [dbQuery.data?.databases],
  );

  // Selecting a program reconciles the database: keep it when compatible,
  // overwrite it with a ready DB of the right molecule type, or block the
  // change and tell the user to prepare one (consistent with the step-gating
  // UX). Same-program clicks are a no-op.
  const handleProgramSelect = (next: BlastProgram) => {
    if (next === form.program) return;
    const meta = PROGRAMS.find((program) => program.value === next) ?? PROGRAMS[0];
    const decision = decideProgramSwitch(meta, form.db, dbQuery.data?.databases);
    if (decision.kind === "blocked") {
      const molecule = decision.molecule === "nucl" ? "nucleotide" : "protein";
      toast(
        `No ${molecule} database is downloaded. Prepare one from the Dashboard before choosing ${next}.`,
        "info",
      );
      return;
    }
    set("program", next);
    if (decision.kind === "switch-db") set("db", decision.db);
  };

  const runtimeDataLoading = Boolean(
    clusterQuery.isLoading || dbQuery.isLoading || warmupStatusLoading,
  );

  const shardingAvailability = useMemo(
    () =>
      deriveShardingAvailability({
        cluster: selectedCluster,
        database: selectedDbInfo,
        isDbAlreadyWarm,
        isWarmupStatusResolved,
        outfmt: form.outfmt,
      }),
    [selectedCluster, selectedDbInfo, isDbAlreadyWarm, isWarmupStatusResolved, form.outfmt],
  );
  const shardingBlockedReason = !shardingAvailability.options[form.sharding_mode].enabled
    ? shardingAvailability.options[form.sharding_mode].reason
    : null;
  const effectiveShardingMode = shardingAvailability.options[form.sharding_mode].enabled
    ? form.sharding_mode
    : shardingAvailability.preferredMode;
  const effectiveShardingEnabled = effectiveShardingMode !== "off";

  // Block a full-database (non-sharded) submit that cannot fit the cluster
  // node's RAM before ElasticBLAST rejects it at submit pre-flight. Uses the
  // *effective* sharding mode so an auto-promoted sharded run is never blocked,
  // and never blocks when the requirement is unknown.
  const fullDbMemoryFit = deriveFullDbMemoryFit({
    database: selectedDbInfo,
    cluster: selectedCluster,
    shardingMode: effectiveShardingMode,
  });

  // Resolve the user-facing blocker for a full-DB run that does not fit node
  // RAM, in priority order:
  //   1. A warmup is ALREADY running for this DB on the selected cluster — tell
  //      the user to wait for it to finish (it unlocks the Sharded profile),
  //      not to "warm the database" they are already warming. Includes the live
  //      progress percentage when known.
  //   2. The DB is confirmed cold and warming WOULD unlock sharding (and is
  //      feasible) — steer to warming instead of the greyed-out Sharded control
  //      (the original catch-22 fix).
  //   3. Otherwise — the default message steering to the Sharded profile or a
  //      larger machine type.
  const fullDbMemoryBlockedReason =
    fullDbMemoryFit.fits === false && isDbWarming
      ? (fullDbMemoryWarmingInProgress(
          fullDbMemoryFit,
          selectedDbInfo?.name ?? "",
          warmupProgressPct,
        ) ?? fullDbMemoryFit.blockedReason)
      : fullDbMemoryFit.fits === false &&
          shardingAvailability.canUnlockShardingByWarming &&
          !warmupBlocked
        ? (fullDbMemoryWarmupRemediation(fullDbMemoryFit, selectedDbInfo?.name ?? "") ??
          fullDbMemoryFit.blockedReason)
        : fullDbMemoryFit.blockedReason;

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
      // The cluster picker is subscription-wide, so the selected cluster
      // may live outside the workspace anchor RG. Backend preflight uses
      // this value to locate the cluster (`list_aks_clusters(cred, sub,
      // rg)`); falling back to the anchor RG only when no cluster is
      // selected keeps the empty-form behaviour stable.
      resource_group: selectedCluster?.resource_group || workloadRg,
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
    fullDbMemoryBlockedReason,
    dataLoading: runtimeDataLoading,
    submitPending: submitMutation.isPending,
  });

  // Critique #6: gate the Submit button behind the caller's BLAST-submit
  // capability at the selected cluster's scope. Degrade-open while the
  // permission probe is loading so there is no flash-of-disabled state;
  // ARM still enforces real authorization at submit time.
  const { permissions: submitPermissions } = usePermissions(
    subId,
    selectedCluster?.resource_group || workloadRg,
    selectedCluster?.name,
  );
  const submitPermissionDenied =
    !submitPermissions.can_submit_blast && !submitPermissions.degraded;
  const submitPermissionTooltip = submitPermissionDenied
    ? permissionDeniedTooltip("can_submit_blast", submitPermissions)
    : undefined;
  const effectiveCanSubmit = validation.canSubmit && !submitPermissionDenied;

  // The Run button is disabled when `effectiveCanSubmit` is false, but a
  // permission denial only lives in the button's hover `title` — which leaves
  // the button silently greyed out with no visible reason. Surface it as a
  // first-class checklist entry so the disabled state always has an on-screen
  // explanation (the footer / rail render `missing` as the "Required before
  // submitting" list).
  const submitMissing: MissingItem[] = submitPermissionDenied
    ? [
        ...validation.missing,
        {
          text:
            submitPermissionTooltip ??
            "You do not have permission to submit BLAST jobs at this cluster scope.",
        },
      ]
    : validation.missing;

  const handleSubmit = () => {
    if (!selectedCluster) return;
    if (submitPermissionDenied) {
      toast(submitPermissionTooltip ?? "Insufficient permission to submit", "error");
      return;
    }
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
          // Selected cluster's actual RG — see preflight payload comment
          // above. The workspace anchor RG is only used as a fallback.
          workloadRg: selectedCluster.resource_group || workloadRg,
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
            <ProgramSection
              form={form}
              programMeta={programMeta}
              onSelectProgram={handleProgramSelect}
              dbAvailableByType={dbAvailableByType}
            />
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
              dbNotReady={validation.dbNotReady}
              dbNotReadyReason={validation.dbNotReadyReason}
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
              missing={submitMissing}
              searchSummary={validation.searchSummary}
              canSubmit={effectiveCanSubmit}
              submitPending={submitMutation.isPending}
              submitError={submitMutation.isError ? submitMutation.error : null}
              preFlightResult={preFlightResult}
              preFlightPending={preFlightMutation.isPending}
              effectiveSearchSpace={selectedDbInfo?.web_blast_searchsp}
              lastSavedAt={lastSavedAt}
              permissionTooltip={submitPermissionTooltip}
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
          missing={submitMissing}
          searchSummary={validation.searchSummary}
          paramsSummary={validation.paramsSummary}
          canSubmit={effectiveCanSubmit}
          submitPending={submitMutation.isPending}
          preFlightResult={preFlightResult}
          preFlightPending={preFlightMutation.isPending}
          effectiveSearchSpace={selectedDbInfo?.web_blast_searchsp}
          lastSavedAt={lastSavedAt}
          effectiveShardingMode={effectiveShardingMode}
          isDbAlreadyWarm={isDbAlreadyWarm}
          permissionTooltip={submitPermissionTooltip}
          set={set}
          onPreFlight={() => preFlightMutation.mutate()}
          onSubmit={handleSubmit}
          now={now}
        />
      </div>
    </div>
  );
}
