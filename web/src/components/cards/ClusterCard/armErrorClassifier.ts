/**
 * armErrorClassifier — turn raw Azure provisioning errors into a
 * compact, action-oriented summary the modal can render cleanly.
 *
 * The Celery `provision_aks` task surfaces ARM rejection text verbatim
 * via `provError`, which arrives as something like:
 *
 *   "Provisioning task failed: (BadRequest) The VM size of
 *   Standard_E16s_v5,Standard_D2s_v3 is not allowed in your subscription
 *   in location 'koreacentral'. For more details, please visit ...
 *   Code: BadRequest Message: ...repeats..."
 *
 * That string is unusable as a top-line message. This module pattern
 * matches the known categories the dashboard cares about and returns:
 *
 *   - `summary` — one-line, action-shaped headline
 *   - `category` — stable id for the FE (so it can render the right
 *     suggestion / portal link)
 *   - `actions` — list of {label, href, kind} the error card renders
 *
 * Unrecognised errors fall through to `category="unknown"` with the
 * raw message as summary — strictly an improvement over the previous
 * "render the whole thing" UX.
 */
export type ArmErrorCategory =
  | "quota"
  | "sku_blocked"
  | "region"
  | "rg_permission"
  | "auth"
  | "unknown";

export interface ArmErrorAction {
  /** Stable kind so the card can pick the right icon and renderer.
   *  - `portal` / `docs`: opens `href` in a new tab.
   *  - `retry`: callback-driven (renderer ignores `href`).
   *  - `command`: `href` carries a shell command (multi-line allowed) that
   *    the renderer copies to clipboard on click instead of navigating.
   *    Used for the rg_permission CTA so the operator gets the exact
   *    `az role assignment create` invocation — pre-filled with the MI
   *    object id, subscription, and RG parsed out of the ARM error —
   *    rather than a generic Microsoft Learn link. */
  kind: "portal" | "docs" | "retry" | "command";
  label: string;
  href: string;
}

export interface ClassifiedArmError {
  category: ArmErrorCategory;
  /** Human one-liner shown as the card headline. */
  summary: string;
  /** Optional secondary message (e.g. "needs 162 vCPUs, have 100"). */
  details?: string;
  actions: ArmErrorAction[];
}

/** Azure portal deep links per category. Subscription id is folded in
 *  when we know it so the deep link lands directly on the user's
 *  subscription instead of the generic blade. We deliberately use the
 *  `aka.ms/quotas` short link as the primary URL because Azure has
 *  occasionally renamed `QuotaMenuBlade`; aka.ms forwards stay valid
 *  across blade renames. The fully-qualified blade URL is kept as a
 *  secondary docs action so the user can still get there if the
 *  short link is unavailable. */
function portalQuotaUrl(
  subscriptionId?: string,
  region?: string,
): string {
  // Microsoft's stable short link to the My quotas blade. Survives
  // blade renames because the redirect target is owned by the Azure
  // capacity team. Region/subscription parameters are accepted via
  // the same query string Azure exposes on the canonical blade URL.
  if (subscriptionId && region) {
    return (
      "https://aka.ms/quotas/view-quotas" +
      `?subscriptionId=${encodeURIComponent(subscriptionId)}` +
      `&location=${encodeURIComponent(region)}`
    );
  }
  return "https://aka.ms/quotas/view-quotas";
}

function portalRgUrl(subscriptionId?: string, resourceGroup?: string): string {
  if (subscriptionId && resourceGroup) {
    return (
      `https://portal.azure.com/#@/resource/subscriptions/${encodeURIComponent(
        subscriptionId,
      )}/resourceGroups/${encodeURIComponent(resourceGroup)}/overview`
    );
  }
  return "https://portal.azure.com/#blade/HubsExtension/BrowseResourceGroups";
}

/** Pull the requested-vs-free numbers out of an InsufficientVCPUQuota
 *  message. The message shape is:
 *    "Insufficient regional vcpu quota left for location <region>.
 *     left regional vcpu quota <free>, requested quota <needed>."
 *  Returns null when the regex doesn't match — caller falls back to
 *  the generic summary. */
function parseQuotaNumbers(
  raw: string,
): { free: number; needed: number; region?: string } | null {
  const m = raw.match(
    /left regional vcpu quota\s+(\d+),\s+requested quota\s+(\d+)/i,
  );
  if (!m) return null;
  const regionMatch = raw.match(/location\s+(?:'|")?([\w-]+)(?:'|")?/i);
  return {
    free: parseInt(m[1], 10),
    needed: parseInt(m[2], 10),
    region: regionMatch?.[1],
  };
}

/** Pull the SKU name list out of "The VM size of A,B is not allowed". */
function parseBlockedSkus(raw: string): string[] {
  const m = raw.match(/VM size of\s+([\w_,.\s-]+?)\s+is not allowed/i);
  if (!m) return [];
  return m[1]
    .split(",")
    .map((s) => s.trim())
    .filter(Boolean);
}

/** Extract the failing principal's object id + the subscription + resource
 *  group from an AuthorizationFailed message of the canonical shape:
 *
 *    "The client 'X' with object id '<oid>' does not have authorization
 *     to perform action '<action>' over scope '/subscriptions/<sub>/
 *     resourceGroups/<rg>' ..."
 *
 *  Used by the rg_permission branch so we can hand the operator a
 *  concrete `az role assignment create` snippet instead of a generic
 *  docs link. Falls back to `null` for any field that doesn't parse,
 *  in which case the classifier omits the command action. */
function parseAuthFailure(
  raw: string,
): { oid?: string; subscriptionId?: string; resourceGroup?: string } {
  const out: { oid?: string; subscriptionId?: string; resourceGroup?: string } = {};
  const oidMatch = raw.match(/object id ['"]([0-9a-f-]{36})['"]/i);
  if (oidMatch) out.oid = oidMatch[1];
  // Match both `/resourceGroups/<rg>` and `/resourcegroups/<rg>` (Azure
  // mixes the casing across services). The non-greedy [^/'"\s] keeps the
  // RG name from swallowing the closing quote.
  const scopeMatch = raw.match(
    /\/subscriptions\/([0-9a-f-]{36})\/resource[gG]roups\/([^/'"\s]+)/,
  );
  if (scopeMatch) {
    out.subscriptionId = scopeMatch[1];
    out.resourceGroup = scopeMatch[2];
  }
  return out;
}

/** Build the multi-line `az role assignment create` snippet that grants
 *  the dashboard MI Contributor on the cluster RG. The snippet is what
 *  the operator must run from a shell with Owner / User Access
 *  Administrator at subscription scope — it's the exact missing step
 *  that `infra/modules/subscriptionRoles.bicep` deliberately does NOT
 *  perform. Region is left as a `<region>` placeholder because the SPA
 *  doesn't know it at the time the error renders. */
function buildGrantContributorCommand(args: {
  oid: string;
  subscriptionId: string;
  resourceGroup: string;
}): string {
  const { oid, subscriptionId, resourceGroup } = args;
  return [
    `# 1) (optional) create the RG if it does not exist yet`,
    `az group create --subscription ${subscriptionId} \\`,
    `  --name ${resourceGroup} --location <region>`,
    ``,
    `# 2) grant the dashboard MI Contributor on that RG only`,
    `az role assignment create --subscription ${subscriptionId} \\`,
    `  --assignee-object-id ${oid} \\`,
    `  --assignee-principal-type ServicePrincipal \\`,
    `  --role Contributor \\`,
    `  --scope /subscriptions/${subscriptionId}/resourceGroups/${resourceGroup}`,
  ].join("\n");
}

export function classifyArmError(
  raw: string,
  context: {
    subscriptionId?: string;
    region?: string;
    resourceGroup?: string;
  } = {},
): ClassifiedArmError {
  const text = raw ?? "";
  const lower = text.toLowerCase();

  // --- Quota ----------------------------------------------------------
  if (
    /errcode_insufficientvcpuquota|insufficient regional vcpu quota|quotaexceeded.*vcpu/i.test(
      text,
    )
  ) {
    const nums = parseQuotaNumbers(text);
    const region = nums?.region ?? context.region;
    const summary = nums
      ? `Quota too small in ${region ?? "this region"} — needs ${nums.needed} vCPUs, you have ${nums.free} free.`
      : `Compute quota is too small in ${region ?? "this region"} for the requested cluster.`;
    return {
      category: "quota",
      summary,
      details:
        "Either request a quota increase from Azure, or shrink the cluster (fewer nodes / smaller SKU) to fit your current limit.",
      actions: [
        {
          kind: "portal",
          label: "Request quota increase",
          href: portalQuotaUrl(context.subscriptionId, region),
        },
        {
          kind: "docs",
          label: "Learn about Azure quotas",
          href: "https://learn.microsoft.com/azure/quotas/view-quotas",
        },
      ],
    };
  }

  // --- SKU blocked ----------------------------------------------------
  if (
    /vm size of .+ is not allowed/i.test(text) ||
    /skuNotAvailable/i.test(text)
  ) {
    const skus = parseBlockedSkus(text);
    const region = context.region;
    const skuLabel = skus.length > 0 ? skus.join(", ") : "the requested VM size";
    return {
      category: "sku_blocked",
      summary: `${skuLabel} is not available in ${region ?? "this region"} for your subscription.`,
      details:
        "Azure restricts SKUs per subscription per region. Pick an available SKU, switch to a different region, or request the SKU from Azure support.",
      actions: [
        {
          kind: "docs",
          label: "SKU availability in AKS regions",
          href: "https://aka.ms/aks/quotas-skus-regions",
        },
        {
          kind: "docs",
          label: "Request VM SKU availability",
          href: "https://learn.microsoft.com/azure/azure-portal/supportability/per-vm-quota-requests",
        },
      ],
    };
  }

  // --- Region not available ------------------------------------------
  if (
    /location .+ is not available/i.test(text) ||
    /locationnotavailable/i.test(text)
  ) {
    return {
      category: "region",
      summary: `Region ${context.region ?? "(unknown)"} is not available for AKS in your subscription.`,
      details:
        "Pick a different region from the Region dropdown, or check AKS region availability.",
      actions: [
        {
          kind: "docs",
          label: "AKS region availability",
          href: "https://learn.microsoft.com/azure/aks/quotas-skus-regions",
        },
      ],
    };
  }

  // --- RG permission / not found -------------------------------------
  if (
    /resourcegroupnotfound/i.test(text) ||
    /authorizationfailed/i.test(text) ||
    /does not have authorization to perform action/i.test(lower)
  ) {
    // Try to extract MI oid + subscription + RG from the raw ARM error so
    // we can hand the operator a concrete `az role assignment create`
    // snippet. Falls back gracefully to the docs link when any field is
    // missing.
    const parsed = parseAuthFailure(text);
    const oid = parsed.oid;
    const sub = parsed.subscriptionId ?? context.subscriptionId;
    const rg = parsed.resourceGroup ?? context.resourceGroup;
    const actions: ArmErrorAction[] = [
      {
        kind: "portal",
        label: "Open resource group",
        href: portalRgUrl(sub, rg),
      },
    ];
    if (oid && sub && rg) {
      actions.push({
        kind: "command",
        label: "Copy az role assignment command",
        href: buildGrantContributorCommand({
          oid,
          subscriptionId: sub,
          resourceGroup: rg,
        }),
      });
    }
    actions.push({
      kind: "docs",
      label: "Grant Contributor role (docs)",
      href: "https://learn.microsoft.com/azure/role-based-access-control/role-assignments-portal",
    });
    return {
      category: "rg_permission",
      summary: `Could not access resource group ${rg ?? "(unknown)"}.`,
      details:
        "The Container App's managed identity has Reader at subscription scope but " +
        "needs Contributor on this resource group to create or modify it. The dashboard " +
        "deploy intentionally does not grant subscription-scope Contributor — " +
        "pre-create the RG and assign the role at RG scope using the command below.",
      actions,
    };
  }

  // --- Auth ----------------------------------------------------------
  if (
    /invalid_token|unauthorized|401/i.test(text) ||
    /authentication failed/i.test(lower)
  ) {
    return {
      category: "auth",
      summary: "Azure rejected the request as unauthenticated.",
      details:
        "Sign out and back in to refresh your bearer token, or check that the dashboard's managed identity is still valid.",
      actions: [
        {
          kind: "docs",
          label: "Troubleshoot MSAL sign-in",
          href: "https://learn.microsoft.com/azure/active-directory/develop/msal-error-handling-js",
        },
      ],
    };
  }

  // --- Unknown -------------------------------------------------------
  // Strip the leading "Provisioning task failed: " wrapper our own
  // poller adds, then trim duplicated "Code: ... Message: ..." tails
  // Azure repeats verbatim.
  const cleaned = text
    .replace(/^Provisioning task failed:\s*/i, "")
    .replace(/\s+Code:\s+[\w_]+\s+Message:\s+.*$/i, "")
    .trim();
  return {
    category: "unknown",
    summary: cleaned || "Provisioning failed for an unknown reason.",
    actions: [],
  };
}
