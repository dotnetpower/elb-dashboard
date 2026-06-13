/**
 * VNet peering — NSG inbound-rule preview/confirm panel.
 *
 * `NsgRuleAction` drives the two-step preview → confirm flow for the
 * deterministic inbound-allow rule (ports 80/443) on the target subnet's
 * NSG, surfacing every skip/error reason the backend reports.
 * `ConflictExistingPanel` renders the colliding operator rule on a
 * name_collision. Pure presentation; the parent owns the API calls.
 */

import { useEffect, useRef, useState } from "react";
import { Copy, Loader2 } from "lucide-react";

import { type VnetPeeringNsgRuleResponse } from "@/api/settings";
import { StatusLine } from "@/components/settings/primitives";

export function NsgRuleAction({
  running,
  disabled,
  onPreview,
  onConfirm,
  onCancel,
  result,
  error,
}: {
  running: boolean;
  disabled: boolean;
  onPreview: () => void;
  onConfirm: () => void;
  onCancel: () => void;
  result: VnetPeeringNsgRuleResponse | null;
  error: string | null;
}) {
  const [copied, setCopied] = useState<"idle" | "ok" | "failed">("idle");
  // Carry the priority the *preview* step quoted so we can flag a shift
  // if another operator took our slot before the operator clicked
  // Confirm. We capture in a ref to keep the effect dependency tight
  // (effect only re-runs when ``result`` changes).
  const previewedPriorityRef = useRef<number | null>(null);
  const [priorityShift, setPriorityShift] = useState<{ from: number; to: number } | null>(null);

  useEffect(() => {
    if (!result) {
      previewedPriorityRef.current = null;
      setPriorityShift(null);
      return;
    }
    if (result.dry_run === true) {
      previewedPriorityRef.current = result.rule?.priority ?? null;
      setPriorityShift(null);
      return;
    }
    if (result.applied === true) {
      const previewed = previewedPriorityRef.current;
      const actual = result.rule?.priority;
      if (
        previewed !== null &&
        typeof actual === "number" &&
        actual !== previewed
      ) {
        setPriorityShift({ from: previewed, to: actual });
      } else {
        setPriorityShift(null);
      }
    }
  }, [result]);

  const copyCli = async () => {
    if (!result?.cli_hint) return;
    if (typeof navigator === "undefined" || !navigator.clipboard) {
      setCopied("failed");
      window.setTimeout(() => setCopied("idle"), 2500);
      return;
    }
    try {
      await navigator.clipboard.writeText(result.cli_hint);
      setCopied("ok");
      window.setTimeout(() => setCopied("idle"), 1500);
    } catch {
      // Clipboard API can refuse without user gesture or under permissions
      // policies; surface a hint so the operator knows to select + Ctrl+C
      // by hand instead of silently doing nothing.
      setCopied("failed");
      window.setTimeout(() => setCopied("idle"), 2500);
    }
  };

  const skipReason = result?.skipped_reason;
  const isAllowedBySkip = !!result && result.applied && skipReason === "already_present";
  const isDryRunPreview = !!result && !result.applied && skipReason === "dry_run";
  const previewRule = result?.rule;
  const previewName = result?.planned_rule_name ?? previewRule?.rule_name ?? "(deterministic)";

  return (
    <div
      style={{
        display: "flex",
        flexDirection: "column",
        gap: 8,
        padding: 12,
        borderRadius: 8,
        background: "var(--surface-2)",
        border: "1px solid var(--border-subtle)",
      }}
    >
      <div style={{ display: "flex", flexWrap: "wrap", alignItems: "center", gap: 8 }}>
        {!isDryRunPreview && (
          <button
            type="button"
            className="btn btn-secondary"
            onClick={onPreview}
            disabled={running || disabled}
            style={{ minWidth: 180 }}
          >
            {running ? (
              <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                <Loader2 size={12} className="spin" /> Checking NSG...
              </span>
            ) : (
              "Preview NSG rule (80, 443)"
            )}
          </button>
        )}
        {isDryRunPreview && (
          <>
            <button
              type="button"
              className="btn btn-primary"
              onClick={onConfirm}
              disabled={running || disabled}
              style={{ minWidth: 180 }}
            >
              {running ? (
                <span style={{ display: "inline-flex", alignItems: "center", gap: 6 }}>
                  <Loader2 size={12} className="spin" /> Applying NSG rule...
                </span>
              ) : (
                "Confirm & apply"
              )}
            </button>
            <button
              type="button"
              className="btn btn-ghost"
              onClick={onCancel}
              disabled={running}
            >
              Cancel
            </button>
          </>
        )}
        <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
          {isDryRunPreview
            ? "Review the planned rule below, then confirm to write it to ARM."
            : "Previews an inbound-allow rule on the target subnet's NSG (source = AKS VNet CIDR, destination = target_ip/32, ports = 80,443). No ARM write happens until you confirm."}{" "}
          Requires{" "}
          <code>Microsoft.Network/networkSecurityGroups/securityRules/write</code>.
        </span>
      </div>
      {isDryRunPreview && (
        <div
          style={{
            display: "grid",
            gridTemplateColumns: "max-content 1fr",
            columnGap: 12,
            rowGap: 4,
            padding: "8px 12px",
            borderRadius: 6,
            background: "var(--surface-3)",
            border: "1px solid var(--border-subtle)",
            fontSize: 12,
          }}
        >
          <span style={{ color: "var(--text-muted)" }}>Planned name</span>
          <code>{previewName}</code>
          <span style={{ color: "var(--text-muted)" }}>Planned priority</span>
          <code>{previewRule?.priority ?? "(first free in 4000-4096)"}</code>
          <span style={{ color: "var(--text-muted)" }}>Source CIDRs</span>
          <code>{(previewRule?.source_prefixes ?? []).join(", ") || "(none)"}</code>
          <span style={{ color: "var(--text-muted)" }}>Destination</span>
          <code>{previewRule?.destination_ip}/32</code>
          <span style={{ color: "var(--text-muted)" }}>Ports</span>
          <code>{(previewRule?.ports ?? []).join(", ")}</code>
          <span style={{ color: "var(--text-muted)" }}>NSG</span>
          <code style={{ wordBreak: "break-all" }}>
            {result?.nsg_context?.nsg_name ?? previewRule?.nsg_id}
          </code>
        </div>
      )}
      {result?.applied && (
        <StatusLine kind="success">
          {isAllowedBySkip
            ? `Existing rule already covers this probe (${result.rule?.rule_name}, priority ${result.rule?.priority ?? "?"}).`
            : `Rule applied: ${result.rule?.rule_name} (priority ${result.rule?.priority}, ports ${(result.rule?.ports ?? []).join(", ")}). Re-running probe...`}
        </StatusLine>
      )}
      {priorityShift && (
        <StatusLine kind="info">
          Priority changed between preview and confirm:{" "}
          <code>{priorityShift.from}</code> -&gt;{" "}
          <code>{priorityShift.to}</code>. Another operator took your
          slot; the rule still applied at the next free priority in
          4000-4096.
        </StatusLine>
      )}
      {result && !result.applied && skipReason === "no_nsg_attached" && (
        <StatusLine kind="info">
          The target subnet has no NSG attached, so nothing to update. If the
          probe still fails, check the AKS subnet&apos;s NSG, Azure Firewall,
          or User Defined Routes.
        </StatusLine>
      )}
      {result && !result.applied && skipReason === "target_ip_not_in_any_subnet" && (
        <StatusLine kind="error">
          {result.target_ip} is not inside any subnet of the selected target VNet.
        </StatusLine>
      )}
      {result && !result.applied && skipReason === "permission_denied" && (
        <>
          <StatusLine kind="info">
            Your identity does not have NSG write permission. Run this with a
            privileged identity instead:
          </StatusLine>
          {result.cli_hint && (
            <div
              style={{
                display: "flex",
                flexDirection: "column",
                gap: 6,
                fontFamily: "var(--font-mono)",
                fontSize: 11,
              }}
            >
              <code style={{ whiteSpace: "pre-wrap", wordBreak: "break-all" }}>
                {result.cli_hint}
              </code>
              <div style={{ display: "flex", alignItems: "center", gap: 8 }}>
                <button
                  type="button"
                  className="btn btn-ghost"
                  onClick={copyCli}
                  style={{ alignSelf: "flex-start" }}
                >
                  <Copy size={12} />{" "}
                  {copied === "ok"
                    ? "Copied!"
                    : copied === "failed"
                      ? "Copy failed"
                      : "Copy CLI"}
                </button>
                {copied === "failed" && (
                  <span style={{ fontSize: 11, color: "var(--text-muted)" }}>
                    Clipboard blocked — select the snippet and press Ctrl+C
                    (or &#8984;C) instead.
                  </span>
                )}
              </div>
            </div>
          )}
        </>
      )}
      {result && !result.applied && skipReason === "name_collision" && (
        <>
          <StatusLine kind="error">
            A rule with the same name already exists but its scope differs.
            The dashboard will not overwrite operator rules — review the
            existing rule below in Azure Portal before retrying.
          </StatusLine>
          {result.rule?.conflict_existing && (
            <ConflictExistingPanel
              conflict={result.rule.conflict_existing as Record<string, unknown>}
            />
          )}
        </>
      )}
      {result && !result.applied && skipReason === "no_free_priority" && (
        <StatusLine kind="error">
          No free priority in the 4000-4096 reserved range. Free one in
          Azure Portal and retry.
        </StatusLine>
      )}
      {error && <StatusLine kind="error">{error}</StatusLine>}
    </div>
  );
}

/**
 * Renders the existing NSG rule that collides with the deterministic
 * dashboard rule name. Shape mirrors
 * `api.tasks.azure.peering_nsg._summarise_rule` — every field is
 * optional because the SDK can return either snake_case or
 * SDK-attribute spellings via the helper.
 */
function ConflictExistingPanel({
  conflict,
}: {
  conflict: Record<string, unknown>;
}) {
  const asString = (key: string): string => {
    const v = conflict[key];
    return v === null || v === undefined ? "" : String(v);
  };
  const asList = (key: string): string[] => {
    const v = conflict[key];
    return Array.isArray(v) ? v.map((item) => String(item)) : [];
  };

  const name = asString("name");
  const priority = asString("priority");
  const protocol = asString("protocol");
  const access = asString("access");
  const direction = asString("direction");
  const sourcePrefixes = asList("source_address_prefixes").filter(Boolean);
  const sourcePorts = asList("source_port_ranges").filter(Boolean);
  const destPrefix = asString("destination_address_prefix");
  // Azure NSG can return either `destination_address_prefix` (singular
  // string) or `destination_address_prefixes` (list). The backend
  // summariser surfaces both shapes; merge them so the panel renders
  // the full destination set whichever form the existing rule used.
  // `"*"` is Azure's explicit wildcard sentinel — render it as "Any"
  // so operators don't read it as a literal CIDR.
  const destPrefixesList = asList("destination_address_prefixes").filter(Boolean);
  const renderWildcard = (raw: string): string => (raw === "*" ? "Any" : raw);
  const destDisplay =
    destPrefixesList.length > 0
      ? destPrefixesList.map(renderWildcard).join(", ")
      : destPrefix
        ? renderWildcard(destPrefix)
        : "(any)";
  const destPorts = asList("destination_port_ranges").filter(Boolean);
  const description = asString("description");

  return (
    <div
      style={{
        display: "grid",
        gridTemplateColumns: "max-content 1fr",
        columnGap: 12,
        rowGap: 4,
        padding: "8px 12px",
        borderRadius: 6,
        background: "var(--surface-3)",
        border: "1px solid var(--border-subtle)",
        fontSize: 12,
      }}
    >
      <span style={{ color: "var(--text-muted)" }}>Existing name</span>
      <code style={{ wordBreak: "break-all" }}>{name || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Priority</span>
      <code>{priority || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Direction</span>
      <code>{direction || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Access</span>
      <code>{access || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Protocol</span>
      <code>{protocol || "(unknown)"}</code>
      <span style={{ color: "var(--text-muted)" }}>Source CIDRs</span>
      <code style={{ wordBreak: "break-all" }}>
        {sourcePrefixes.length
          ? sourcePrefixes.map(renderWildcard).join(", ")
          : "(any)"}
      </code>
      {sourcePorts.length > 0 && (
        <>
          <span style={{ color: "var(--text-muted)" }}>Source ports</span>
          <code>{sourcePorts.map(renderWildcard).join(", ")}</code>
        </>
      )}
      <span style={{ color: "var(--text-muted)" }}>Destination</span>
      <code style={{ wordBreak: "break-all" }}>{destDisplay}</code>
      <span style={{ color: "var(--text-muted)" }}>Destination ports</span>
      <code>{destPorts.length ? destPorts.map(renderWildcard).join(", ") : "(any)"}</code>
      {description && (
        <>
          <span style={{ color: "var(--text-muted)" }}>Description</span>
          <code style={{ wordBreak: "break-all" }}>{description}</code>
        </>
      )}
    </div>
  );
}
