/**
 * "Identity and Security" diagnostic detail — extracted from
 * `DiagnosticsSection.tsx` (issue #24).
 *
 * Reproduces the Azure portal IAM "View my access" view per resource group
 * plus the signed-in account / dashboard managed identity. The user's
 * effective role assignments (direct + Entra-group-inherited) are listed for
 * each RG the dashboard depends on, with an inheritance flag, so permission
 * gaps in a freshly-onboarded tenant are obvious.
 *
 * Read-only diagnostic — backed by `GET /api/me/access-review`, which (unlike
 * `/me/permissions`) does NOT degrade open: an enumeration failure is surfaced
 * as a finding, not hidden. Re-exported from `DiagnosticsSection.tsx` so the
 * existing `@/components/settings/sections/DiagnosticsSection` import path keeps
 * working alongside the dedicated diagnostics page.
 */

import { useCallback, useEffect, useMemo, useState } from "react";
import {
  AlertCircle,
  CheckCircle2,
  ChevronLeft,
  Loader2,
  RefreshCw,
  ShieldCheck,
  User,
} from "lucide-react";

import { formatApiError } from "@/api/client";
import {
  meApi,
  type AccessReviewGroup,
  type AccessReviewPrincipal,
  type AccessReviewRow,
  type AccessReviewTarget,
} from "@/api/me";
import { type AksClusterSummary, monitoringApi } from "@/api/monitoring";
import { msalInstance } from "@/auth/msal";
import type { ResourceConfig } from "@/components/SetupWizard";
import { Group, Section, Segmented } from "@/components/settings/primitives";

interface RgTarget {
  rg: string;
  labels: string[];
}

interface SignedInIdentity {
  name: string;
  username: string;
  tenantId: string;
  objectId: string;
}

const SCOPE_LEVEL_LABEL: Record<AccessReviewRow["scope_level"], string> = {
  subscription: "Subscription",
  management_group: "Management group",
  resource_group: "Resource group",
  resource: "Resource",
  other: "Scope",
};

export function IdentitySecurityDetail({
  config,
  onBack,
}: {
  config: ResourceConfig | null;
  onBack: () => void;
}) {
  const [clusterRgs, setClusterRgs] = useState<string[]>([]);
  const [groups, setGroups] = useState<AccessReviewGroup[]>([]);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [identity, setIdentity] = useState<SignedInIdentity | null>(null);
  const [target, setTarget] = useState<AccessReviewTarget>("me");
  const [principal, setPrincipal] = useState<AccessReviewPrincipal | null>(null);

  const subscriptionId = config?.subscriptionId ?? "";

  // Resolve the signed-in user's account info. MSAL's active account is the
  // immediate source (name / username / tenant / object id); the backend
  // `/api/me` confirms the validated token's claims (upn / tenant / oid) and
  // wins when present so the displayed identity matches what the API enforces.
  useEffect(() => {
    let cancelled = false;
    const account = msalInstance.getActiveAccount() ?? msalInstance.getAllAccounts()[0];
    const base: SignedInIdentity = {
      name: account?.name ?? "",
      username: account?.username ?? "",
      tenantId: account?.tenantId ?? "",
      objectId: account?.localAccountId ?? "",
    };
    setIdentity(base);
    void (async () => {
      try {
        const me = await meApi.get();
        if (cancelled) return;
        setIdentity({
          name: base.name,
          username: (me.upn ?? base.username ?? "").trim(),
          tenantId: (me.tenant_id ?? base.tenantId ?? "").trim(),
          objectId: (me.object_id ?? base.objectId ?? "").trim(),
        });
      } catch {
        // Keep the MSAL-derived identity on failure.
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);


  // Collect the resource groups the dashboard touches. Config carries the
  // workload / ACR / terminal RGs; AKS cluster RGs are discovered sub-wide
  // (a workload cluster commonly lives outside the dashboard anchor RG).
  const targets = useMemo<RgTarget[]>(() => {
    const byRg = new Map<string, RgTarget>();
    const add = (rg: string | undefined, label: string) => {
      const name = (rg ?? "").trim();
      if (!name) return;
      const key = name.toLowerCase();
      const existing = byRg.get(key);
      if (existing) {
        if (!existing.labels.includes(label)) existing.labels.push(label);
      } else {
        byRg.set(key, { rg: name, labels: [label] });
      }
    };
    add(config?.workloadResourceGroup, "Workload / dashboard");
    add(config?.acrResourceGroup, "Container registry");
    add(config?.terminalResourceGroup, "Terminal");
    for (const rg of clusterRgs) add(rg, "AKS cluster");
    return [...byRg.values()];
  }, [config?.workloadResourceGroup, config?.acrResourceGroup, config?.terminalResourceGroup, clusterRgs]);

  // Discover cluster RGs once a subscription is known.
  useEffect(() => {
    if (!subscriptionId) return;
    let cancelled = false;
    void (async () => {
      try {
        const response = await monitoringApi.aks(subscriptionId);
        if (cancelled) return;
        const rgs = (response.clusters ?? [])
          .map((c: AksClusterSummary) => c.resource_group)
          .filter((rg): rg is string => Boolean(rg));
        setClusterRgs(rgs);
      } catch {
        // Cluster discovery is best-effort; the config RGs are still reviewed.
        if (!cancelled) setClusterRgs([]);
      }
    })();
    return () => {
      cancelled = true;
    };
  }, [subscriptionId]);

  const targetKey = useMemo(
    () => targets.map((t) => t.rg.toLowerCase()).sort().join(","),
    [targets],
  );

  const review = useCallback(async () => {
    if (!subscriptionId || targets.length === 0) {
      setGroups([]);
      setPrincipal(null);
      return;
    }
    setLoading(true);
    setError(null);
    try {
      const response = await meApi.accessReview(
        subscriptionId,
        targets.map((t) => t.rg),
        target,
      );
      setGroups(response.groups ?? []);
      setPrincipal(response.principal ?? null);
    } catch (err) {
      setError(formatApiError(err, "me"));
    } finally {
      setLoading(false);
    }
  }, [subscriptionId, targets, targetKey, target]); // eslint-disable-line react-hooks/exhaustive-deps

  useEffect(() => {
    void review();
  }, [review]);

  // Clear the previous principal's rows the instant the target toggles, so a
  // diagnostic whose whole point is "whose access" never renders the user's
  // RBAC rows under the "Dashboard identity" header (or vice-versa) during the
  // in-flight refetch. A same-target Refresh does not hit this.
  useEffect(() => {
    setGroups([]);
    setPrincipal(null);
  }, [target]);

  const labelFor = useCallback(
    (rg: string) =>
      targets.find((t) => t.rg.toLowerCase() === rg.toLowerCase())?.labels.join(" · ") ?? "",
    [targets],
  );

  if (!subscriptionId) {
    return (
      <Section heading="Identity and Security">
        <DiagnosticDetailHeader onBack={onBack} title="Identity and Security" />
        <SignedInAccountCard identity={identity} />
        <Group>
          <div style={{ padding: "16px 0", fontSize: 12, color: "var(--text-faint)" }}>
            Select a subscription in the Setup Wizard first — access review needs a
            subscription to enumerate role assignments.
          </div>
        </Group>
      </Section>
    );
  }

  const dashboardUnavailable =
    target === "dashboard" && principal !== null && principal.available === false;

  return (
    <Section heading="Identity and Security">
      <DiagnosticDetailHeader onBack={onBack} title="Identity and Security" />

      <div style={{ marginBottom: 12 }}>
        <Segmented
          ariaLabel="Whose access to review"
          value={target}
          onChange={setTarget}
          options={[
            { value: "me", label: "My access" },
            { value: "dashboard", label: "Dashboard identity" },
          ]}
        />
      </div>

      {target === "me" ? (
        <SignedInAccountCard identity={identity} />
      ) : (
        <DashboardIdentityCard principal={principal} />
      )}

      <div style={{ fontSize: 12, color: "var(--text-faint)", lineHeight: 1.6, margin: "-4px 0 12px" }}>
        {target === "me" ? (
          <>
            Your effective Azure RBAC role assignments per resource group — the same
            view as the portal's IAM <strong>“View my access”</strong>. Includes roles
            inherited from the subscription, management groups, and your Entra groups.
            Use this to spot a missing role when onboarding a new tenant.
          </>
        ) : (
          <>
            The shared <strong>managed identity</strong> the dashboard runs as — this is
            the principal that actually performs ARM and Storage writes. A missing role
            here is the usual root cause of a tenant onboarding failure even when{" "}
            <em>your</em> access looks fine.
          </>
        )}
      </div>

      <div style={{ display: "flex", justifyContent: "flex-end", marginBottom: 10 }}>
        <button className="glass-button" onClick={() => void review()} disabled={loading}>
          {loading ? <Loader2 size={12} strokeWidth={1.5} className="spin" /> : <RefreshCw size={12} strokeWidth={1.5} />}
          {loading ? "Checking…" : "Refresh"}
        </button>
      </div>

      {error && (
        <div
          role="alert"
          style={{
            display: "flex",
            gap: 8,
            alignItems: "flex-start",
            padding: "10px 12px",
            marginBottom: 12,
            borderRadius: 8,
            border: "1px solid var(--border-weak)",
            background: "var(--bg-secondary)",
            fontSize: 12,
            color: "var(--text-muted)",
          }}
        >
          <AlertCircle size={14} strokeWidth={1.5} style={{ marginTop: 1, flexShrink: 0 }} />
          <span style={{ wordBreak: "break-word" }}>{error}</span>
        </div>
      )}

      {dashboardUnavailable ? (
        <Group>
          <div style={{ padding: "16px 0", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
            The dashboard managed identity's principal id is not available in this
            environment (no <code style={{ fontSize: 11 }}>SHARED_IDENTITY_PRINCIPAL_ID</code>).
            This is expected in local development — the identity only exists in the
            deployed Container App.
          </div>
        </Group>
      ) : (
        <>
          {groups.length === 0 && !loading && !error && (
            <Group>
              <div style={{ padding: "16px 0", fontSize: 12, color: "var(--text-faint)" }}>
                No resource groups configured to review yet.
              </div>
            </Group>
          )}

          {groups.map((grp) => (
            <RgAccessCard key={grp.resource_group} group={grp} label={labelFor(grp.resource_group)} />
          ))}
        </>
      )}
    </Section>
  );
}

function DiagnosticDetailHeader({ onBack, title }: { onBack: () => void; title: string }) {
  return (
    <button
      type="button"
      onClick={onBack}
      style={{
        display: "inline-flex",
        alignItems: "center",
        gap: 4,
        background: "none",
        border: "none",
        padding: "0 0 12px",
        margin: 0,
        cursor: "pointer",
        color: "var(--text-faint)",
        fontSize: 12,
      }}
      aria-label={`Back to diagnostics — leave ${title}`}
    >
      <ChevronLeft size={14} strokeWidth={1.5} /> Diagnose &amp; solve problems
    </button>
  );
}

function DashboardIdentityCard({ principal }: { principal: AccessReviewPrincipal | null }) {
  const objectId = principal?.object_id || "";
  const rows: Array<[string, string]> = [
    ["Identity", "Shared user-assigned managed identity"],
    ["Object ID", objectId || "—"],
  ];
  return (
    <Group>
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "14px 0 10px" }}>
        <div
          aria-hidden
          style={{
            width: 36,
            height: 36,
            borderRadius: "50%",
            background: "var(--bg-tertiary)",
            border: "1px solid var(--border-weak)",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            color: "var(--text-muted)",
            flexShrink: 0,
          }}
        >
          <ShieldCheck size={16} strokeWidth={1.5} />
        </div>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)" }}>
            Dashboard managed identity
          </div>
          <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
            The principal the control plane uses for ARM &amp; Storage
          </div>
        </div>
      </div>
      <div style={{ paddingBottom: 12 }}>
        {rows.map(([k, v]) => (
          <div
            key={k}
            style={{
              display: "grid",
              gridTemplateColumns: "92px 1fr",
              gap: 12,
              padding: "5px 0",
              fontSize: 12,
              borderTop: "1px solid var(--border-weak)",
            }}
          >
            <span style={{ color: "var(--text-faint)" }}>{k}</span>
            <span
              style={{
                color: v === "—" ? "var(--text-faint)" : "var(--text-muted)",
                wordBreak: "break-all",
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {v}
            </span>
          </div>
        ))}
      </div>
    </Group>
  );
}

function SignedInAccountCard({ identity }: { identity: SignedInIdentity | null }) {
  const initials = (identity?.name || identity?.username || "U")
    .split(/[\s@.]+/)
    .filter(Boolean)
    .map((w) => w[0])
    .join("")
    .slice(0, 2)
    .toUpperCase();

  const rows: Array<[string, string]> = [
    ["Account", identity?.username || "—"],
    ["Tenant ID", identity?.tenantId || "—"],
    ["Object ID", identity?.objectId || "—"],
  ];

  return (
    <Group>
      <div style={{ display: "flex", alignItems: "center", gap: 12, padding: "14px 0 10px" }}>
        <div
          aria-hidden
          style={{
            width: 36,
            height: 36,
            borderRadius: "50%",
            background: "var(--bg-tertiary)",
            border: "1px solid var(--border-weak)",
            display: "inline-flex",
            alignItems: "center",
            justifyContent: "center",
            fontSize: 13,
            fontWeight: 600,
            color: "var(--text-muted)",
            flexShrink: 0,
          }}
        >
          {initials || <User size={16} strokeWidth={1.5} />}
        </div>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", wordBreak: "break-word" }}>
            {identity?.name || identity?.username || "Signed-in user"}
          </div>
          <div style={{ fontSize: 11, color: "var(--text-faint)" }}>Signed in to the dashboard</div>
        </div>
      </div>
      <div style={{ paddingBottom: 12 }}>
        {rows.map(([k, v]) => (
          <div
            key={k}
            style={{
              display: "grid",
              gridTemplateColumns: "92px 1fr",
              gap: 12,
              padding: "5px 0",
              fontSize: 12,
              borderTop: "1px solid var(--border-weak)",
            }}
          >
            <span style={{ color: "var(--text-faint)" }}>{k}</span>
            <span
              style={{
                color: v === "—" ? "var(--text-faint)" : "var(--text-muted)",
                wordBreak: "break-all",
                fontVariantNumeric: "tabular-nums",
              }}
            >
              {v}
            </span>
          </div>
        ))}
      </div>
    </Group>
  );
}

function RgAccessCard({ group, label }: { group: AccessReviewGroup; label: string }) {
  return (
    <Group>
      <div style={{ padding: "12px 0 8px", display: "flex", alignItems: "center", justifyContent: "space-between", gap: 12 }}>
        <div style={{ minWidth: 0 }}>
          <div style={{ fontSize: 13, fontWeight: 600, color: "var(--text-primary)", wordBreak: "break-all" }}>
            {group.resource_group}
          </div>
          {label && <div style={{ fontSize: 11, color: "var(--text-faint)" }}>{label}</div>}
        </div>
        {group.degraded ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--warning, #d08a3a)", flexShrink: 0 }}>
            <AlertCircle size={13} strokeWidth={1.5} /> Cannot read
          </span>
        ) : group.assignments.length > 0 ? (
          <span style={{ display: "inline-flex", alignItems: "center", gap: 5, fontSize: 11, color: "var(--text-faint)", flexShrink: 0 }}>
            <CheckCircle2 size={13} strokeWidth={1.5} /> {group.assignments.length} role
            {group.assignments.length === 1 ? "" : "s"}
          </span>
        ) : (
          <span style={{ fontSize: 11, color: "var(--text-faint)", flexShrink: 0 }}>No access</span>
        )}
      </div>

      {group.degraded ? (
        <div style={{ padding: "4px 0 14px", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
          Could not enumerate role assignments. You likely lack{" "}
          <code style={{ fontSize: 11 }}>Microsoft.Authorization/roleAssignments/read</code> on this
          scope — which is itself why management actions here may fail. Ask a subscription Owner /
          User Access Administrator to grant at least Reader.
          <div style={{ marginTop: 6, color: "var(--text-faint)", fontSize: 11, wordBreak: "break-word" }}>
            {group.reason}
          </div>
        </div>
      ) : group.assignments.length === 0 ? (
        <div style={{ padding: "4px 0 14px", fontSize: 12, color: "var(--text-muted)", lineHeight: 1.5 }}>
          You have no Azure RBAC role at this scope. Dashboard actions that touch this resource group
          will fail until a role (Reader to browse, Contributor to manage) is assigned.
        </div>
      ) : (
        <div style={{ paddingBottom: 8 }}>
          {group.assignments.map((row) => (
            <div
              key={`${row.role_guid}:${row.assignment_scope}`}
              style={{
                display: "grid",
                gridTemplateColumns: "1fr auto",
                alignItems: "center",
                gap: 12,
                padding: "8px 0",
                borderTop: "1px solid var(--border-weak)",
              }}
            >
              <div style={{ minWidth: 0 }}>
                <div style={{ fontSize: 12, color: "var(--text-primary)", wordBreak: "break-word" }}>
                  {row.role_name}
                </div>
                <div style={{ fontSize: 11, color: "var(--text-faint)" }}>
                  {SCOPE_LEVEL_LABEL[row.scope_level]}
                </div>
              </div>
              <span
                style={{
                  fontSize: 10,
                  padding: "2px 8px",
                  borderRadius: 999,
                  border: "1px solid var(--border-weak)",
                  color: "var(--text-faint)",
                  whiteSpace: "nowrap",
                  flexShrink: 0,
                }}
              >
                {row.inherited ? "Inherited" : "Direct"}
              </span>
            </div>
          ))}
        </div>
      )}
    </Group>
  );
}
