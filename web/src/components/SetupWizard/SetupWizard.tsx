import { useCallback, useEffect, useState } from "react";
import { useMutation, useQuery } from "@tanstack/react-query";

import { armProxyApi, resourceApi } from "@/api/endpoints";
import { listSubscriptions as armListSubs } from "@/api/arm";
import { isAksManagedResourceGroup } from "@/lib/aksManagedRg";
import { listWithMiFallback } from "@/lib/armWithMiFallback";
import { useFocusTrap } from "@/hooks/useFocusTrap";

import { saveConfig } from "./configStorage";
import { Step1Subscription } from "./steps/Step1Subscription";
import { Step2ResourceGroups } from "./steps/Step2ResourceGroups";
import { Step3Resources } from "./steps/Step3Resources";
import { Step4Confirm } from "./steps/Step4Confirm";
import { DEFAULTS, DEV_BYPASS, type ResourceConfig, type Step } from "./types";
import {
  UUID_RE,
  validateStep1,
  validateStep2,
  validateStep3,
  type ValidationErrors,
} from "./validation";
import { WizardFooter } from "./WizardFooter";
import { WizardStepper } from "./WizardStepper";

interface Props {
  onComplete: (config: ResourceConfig) => void;
  onClose?: () => void;
}

interface RgRow {
  name: string;
  location: string;
  tags?: Record<string, string>;
}

function findTaggedWorkspace(resourceGroups: RgRow[]): RgRow | undefined {
  const selectableResourceGroups = resourceGroups.filter(
    (rg) => !isAksManagedResourceGroup(rg),
  );
  return (
    selectableResourceGroups.find(
      (rg) => rg.tags?.["elb-workload-rg"] || rg.tags?.["elb-storage"],
    ) ??
    selectableResourceGroups.find(
      (rg) => rg.tags?.app === "elb-dashboard" && Boolean(rg.tags?.["azd-env-name"]),
    )
  );
}

export function SetupWizard({ onComplete, onClose }: Props) {
  const [step, setStep] = useState<Step>(1);
  const [config, setConfig] = useState<ResourceConfig>(DEFAULTS);
  const [errors, setErrors] = useState<ValidationErrors>({});
  const [attempted, setAttempted] = useState(false);
  const dialogRef = useFocusTrap<HTMLDivElement>(true, onClose);

  // ── Step 1: Subscriptions (direct ARM or backend MI proxy) ──
  const subsQuery = useQuery({
    queryKey: ["wizard-subs"],
    queryFn: async () => {
      if (DEV_BYPASS) return armProxyApi.listSubscriptions();
      // Mirror useWorkspaceDiscovery: fall back to the backend MI proxy
      // on either a thrown ARM error OR an empty list so a collaborator
      // with zero subscription-scope RBAC still gets a usable dropdown.
      return listWithMiFallback(
        async () => {
          const subs = await armListSubs();
          return subs.map((s) => ({
            subscriptionId: s.subscriptionId,
            displayName: s.displayName,
          }));
        },
        () => armProxyApi.listSubscriptions(),
      );
    },
    staleTime: 5 * 60_000,
    retry: 1,
  });
  useEffect(() => {
    if (!config.subscriptionId && subsQuery.data?.length)
      setConfig((c) => ({
        ...c,
        subscriptionId: subsQuery.data[0].subscriptionId,
      }));
  }, [config.subscriptionId, subsQuery.data]);

  // ── Step 2: Resource groups (direct ARM or backend MI proxy) ──
  const rgQuery = useQuery({
    queryKey: ["wizard-rgs", config.subscriptionId],
    queryFn: () => armProxyApi.listResourceGroups(config.subscriptionId),
    enabled: Boolean(config.subscriptionId) && UUID_RE.test(config.subscriptionId),
    staleTime: 30_000,
    retry: 1,
  });

  useEffect(() => {
    const workspace = rgQuery.data?.length
      ? findTaggedWorkspace(rgQuery.data)
      : undefined;
    if (!workspace) return;
    const tags = workspace.tags ?? {};
    setConfig((current) => {
      const next = { ...current };
      let changed = false;
      const workloadRg = tags["elb-workload-rg"] || workspace.name;
      const acrRg = tags["elb-acr-rg"] || workloadRg;
      const region = tags["elb-region"] || workspace.location;

      if (!next.workloadResourceGroup) {
        next.workloadResourceGroup = workloadRg;
        changed = true;
      }
      if (!next.acrResourceGroup) {
        next.acrResourceGroup = acrRg;
        changed = true;
      }
      if (!next.storageAccountName && tags["elb-storage"]) {
        next.storageAccountName = tags["elb-storage"];
        changed = true;
      }
      if (!next.acrName && tags["elb-acr"]) {
        next.acrName = tags["elb-acr"];
        changed = true;
      }
      if (!next.region || next.region === DEFAULTS.region) {
        next.region = region;
        changed = true;
      }

      return changed ? next : current;
    });
  }, [rgQuery.data]);

  // ── Step 3: Discovery (direct ARM or backend MI proxy) ──
  const storageQuery = useQuery({
    queryKey: ["wizard-storage", config.subscriptionId, config.workloadResourceGroup],
    queryFn: () =>
      armProxyApi.listStorageAccounts(
        config.subscriptionId,
        config.workloadResourceGroup,
      ),
    enabled: step >= 3 && Boolean(config.subscriptionId && config.workloadResourceGroup),
    retry: 1,
  });
  const acrQuery = useQuery({
    queryKey: ["wizard-acr", config.subscriptionId, config.acrResourceGroup],
    queryFn: () => armProxyApi.listAcrs(config.subscriptionId, config.acrResourceGroup),
    enabled: step >= 3 && Boolean(config.subscriptionId && config.acrResourceGroup),
    retry: 1,
  });
  // vmQuery removed: there is no Terminal VM in the bundled Container Apps
  // topology; the terminal is the in-process `terminal` sidecar.

  // Auto-fill discovered resources.
  useEffect(() => {
    if (step !== 3) return;
    setConfig((c) => {
      const n = { ...c };
      if (!n.storageAccountName && storageQuery.data?.length)
        n.storageAccountName = storageQuery.data[0].name;
      if (!n.acrName && acrQuery.data?.length) n.acrName = acrQuery.data[0].name;
      return n;
    });
  }, [step, storageQuery.data, acrQuery.data]);

  // ── Mutations for resource creation ──
  const createStorageMut = useMutation({
    mutationFn: () =>
      resourceApi.ensureStorage({
        subscription_id: config.subscriptionId,
        resource_group: config.workloadResourceGroup,
        account_name: config.storageAccountName,
        region: config.region,
      }),
  });
  const createAcrMut = useMutation({
    mutationFn: () =>
      resourceApi.ensureAcr({
        subscription_id: config.subscriptionId,
        resource_group: config.acrResourceGroup,
        registry_name: config.acrName,
        region: config.region,
      }),
  });

  // ── Navigation ──
  const handleNext = useCallback(() => {
    setAttempted(true);
    const v =
      step === 1
        ? validateStep1(config)
        : step === 2
          ? validateStep2(config)
          : step === 3
            ? validateStep3(config)
            : {};
    setErrors(v);
    if (Object.keys(v).length > 0) return;
    setAttempted(false);
    setErrors({});
    setStep((s) => (s + 1) as Step);
  }, [step, config]);

  const handleFinish = useCallback(() => {
    saveConfig(config);
    // Save associated resource config as RG tags (fire-and-forget).
    if (config.subscriptionId && config.workloadResourceGroup) {
      armProxyApi
        .setRgTags(config.subscriptionId, config.workloadResourceGroup, {
          "elb-acr-rg": config.acrResourceGroup || "",
          "elb-acr": config.acrName || "",
          "elb-storage": config.storageAccountName || "",
          "elb-terminal-rg": config.terminalResourceGroup || "",
          "elb-terminal-vm": config.terminalVmName || "",
          "elb-region": config.region || "",
        })
        .catch(() => {}); // Best-effort, don't block.
    }
    onComplete(config);
  }, [config, onComplete]);

  // Live validation.
  useEffect(() => {
    if (!attempted) return;
    const v =
      step === 1
        ? validateStep1(config)
        : step === 2
          ? validateStep2(config)
          : step === 3
            ? validateStep3(config)
            : {};
    setErrors(v);
  }, [config, step, attempted]);

  const currentStepErrors =
    step === 1
      ? validateStep1(config)
      : step === 2
        ? validateStep2(config)
        : step === 3
          ? validateStep3(config)
          : {};
  const canProceed = Object.keys(currentStepErrors).length === 0;

  return (
    <div
      style={{
        position: "fixed",
        inset: 0,
        background: "rgba(0,0,0,0.6)",
        display: "flex",
        alignItems: "center",
        justifyContent: "center",
        zIndex: 100,
        backdropFilter: "blur(4px)",
      }}
    >
      <div
        ref={dialogRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="setup-wizard-title"
        style={{
          background: "var(--bg-primary)",
          border: "1px solid var(--border-medium)",
          borderRadius: 16,
          boxShadow: "0 8px 48px rgba(0,0,0,0.5)",
          width: 720,
          maxHeight: "90vh",
          overflow: "hidden",
          display: "flex",
          flexDirection: "column",
        }}
      >
        {/* Header */}
        <div
          style={{
            padding: "24px 32px 0",
            display: "flex",
            alignItems: "center",
            gap: 12,
          }}
        >
          <div
            style={{
              width: 36,
              height: 36,
              borderRadius: 10,
              background: "linear-gradient(135deg, #6e9fff, #b877d9)",
              boxShadow: "0 2px 12px rgba(110,159,255,0.25)",
            }}
          />
          <div style={{ flex: 1 }}>
            <h1 id="setup-wizard-title" style={{ fontSize: 18, fontWeight: 700, margin: 0 }}>
              Set up your BLAST workspace
            </h1>
            <div
              style={{
                fontSize: 12,
                color: "var(--text-faint)",
                marginTop: 1,
              }}
            >
              Connect to Azure and configure resources for running BLAST searches
            </div>
          </div>
          {onClose && (
            <button
              type="button"
              onClick={onClose}
              aria-label="Close wizard"
              style={{
                width: 32,
                height: 32,
                borderRadius: 8,
                display: "flex",
                alignItems: "center",
                justifyContent: "center",
                color: "var(--text-faint)",
                cursor: "pointer",
                border: "1px solid var(--border-weak)",
                background: "none",
                flexShrink: 0,
                transition: "all 0.15s",
              }}
              onMouseEnter={(e) => {
                e.currentTarget.style.borderColor = "var(--border-medium)";
                e.currentTarget.style.color = "var(--text-muted)";
              }}
              onMouseLeave={(e) => {
                e.currentTarget.style.borderColor = "var(--border-weak)";
                e.currentTarget.style.color = "var(--text-faint)";
              }}
              title="Close wizard"
            >
              ✕
            </button>
          )}
        </div>

        <WizardStepper step={step} />

        {/* Body */}
        <div style={{ padding: "8px 32px 24px", flex: 1, overflowY: "auto" }}>
          {step === 1 && (
            <Step1Subscription
              config={config}
              setConfig={setConfig}
              errors={errors}
              subsQuery={subsQuery}
            />
          )}
          {step === 2 && (
            <Step2ResourceGroups
              config={config}
              setConfig={setConfig}
              errors={errors}
              rgQuery={rgQuery}
            />
          )}
          {step === 3 && (
            <Step3Resources
              config={config}
              setConfig={setConfig}
              errors={errors}
              storageQuery={storageQuery}
              acrQuery={acrQuery}
              createStorageMut={createStorageMut}
              createAcrMut={createAcrMut}
            />
          )}
          {step === 4 && <Step4Confirm config={config} />}
        </div>

        <WizardFooter
          step={step}
          canProceed={canProceed}
          onBack={() => {
            setStep((s) => (s - 1) as Step);
            setAttempted(false);
            setErrors({});
          }}
          onNext={handleNext}
          onFinish={handleFinish}
        />
      </div>
    </div>
  );
}
