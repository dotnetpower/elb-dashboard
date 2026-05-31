import { api } from "@/api/client";
import type { OrchestrationStatus } from "@/api/shared";
import type { AutoWarmupPreference } from "@/api/monitoring";

export interface AksProvisionRequest {
  subscription_id: string;
  resource_group: string;
  region: string;
  cluster_name: string;
  node_sku?: string;
  node_count?: number;
  /** AKS system pool VM size. Mirrors sibling repo
   *  constants.py::ELB_DFLT_AZURE_SYSTEM_VM_SIZE (Standard_D2s_v3). */
  system_vm_size?: string;
  /** Node count for the system pool (default 1, capped at 3). */
  system_node_count?: number;
  acr_resource_group?: string;
  acr_name?: string;
  storage_resource_group?: string;
  storage_account?: string;
  /** Free-form cluster classification ("heavy" / "light" / "general" / "").
   *  Written to ARM as the `elb-tier` tag so the dashboard can group
   *  multi-cluster deployments. Empty string = do not write the tag.
   *  Picking a non-empty tier in the SPA also pre-fills the workload
   *  pool's `node_sku` / `node_count` from `CLUSTER_TIER_PRESETS`. */
  tier?: string;
}

export interface AksProvisionResponse {
  /** Celery task id — poll /api/tasks/{task_id} for live status. The api
   *  route already returns this; if it's missing the FE falls back to
   *  cluster-list polling only and can't detect task failures. */
  task_id?: string;
  /** Mirror of task_id kept for backwards compat with the Functions-era
   *  Durable Functions response shape. */
  id?: string;
  job_id?: string;
  instance_id?: string;
  statusQueryGetUri?: string;
  cluster_name: string;
  resource_group: string;
  region: string;
  node_sku: string;
  node_count: number;
  system_vm_size?: string;
  system_node_count?: number;
  status: string;
  message: string;
  roles_assigned?: string[];
}

export interface AksSku {
  name: string;
  vCPUs: number;
  memoryGiB: number;
  category: string;
  series: string;
  hourlyUsd: number;
  /** "system" — only suitable for the systempool;
   *  "blast"  — only suitable for the workload pool;
   *  "both"   — fits either. */
  role: "system" | "blast" | "both";
  /** Stable group id used for <optgroup> rendering. */
  group: string;
}

export interface AksSkuListResponse {
  skus: AksSku[];
  default: string;
  default_sku: string;
  /** Default SKU for the small AKS system pool. */
  default_system_sku: string;
  /** Default node count for the AKS system pool. */
  default_system_node_count?: number;
  /** Map of group id → human-friendly label (used as `<optgroup label>`). */
  group_labels?: Record<string, string>;
  /** Stable display order for the group ids in the dropdown. */
  group_order?: string[];
  degraded?: boolean;
  degraded_reason?: string;
}

export interface OpenApiTokenStatus {
  configured: boolean;
  token: string;
  masked_token: string;
  header_name: string;
  env_name: string;
  source: string;
  updated_at?: string | null;
  generated?: boolean;
  rotated?: boolean;
  /**
   * Populated only when GET /aks/openapi/token tried to self-heal a
   * legacy deployment (missing ELB_OPENAPI_API_TOKEN env entry) and the
   * patch failed. The panel renders this as a red banner so the operator
   * immediately sees the actionable failure code+message instead of the
   * silent "No API token generated" placeholder. `null` on the happy
   * path — both "token already configured" and "self-heal succeeded".
   */
  self_heal_error?: { code: string; message: string } | null;
}

export interface OpenApiDeploymentStatus {
  configured: boolean;
  deployment_name: string;
  container_name: string;
  namespace: string;
  image: string;
  image_repository: string;
  image_tag: string;
}

export interface OpenApiPublicHttpsStatus {
  enabled: boolean;
  fqdn?: string;
  public_base_url?: string;
  dns_label?: string;
  region?: string;
  ingress_lb_ip?: string;
  cert_issuer?: string;
  cert_expires_at?: string;
  updated_at?: string;
}

/**
 * Live Private Link Service (PLS) annotation state for the ``elb-openapi``
 * Service. Returned by ``GET /aks/openapi/pls``.
 *
 * ``available=false`` means the dashboard could not probe the live state
 * (RBAC missing, K8s API unreachable, deploy never ran). The SPA renders
 * those as an "unknown" cell instead of a hard error.
 *
 * ``transition_pending=true`` means the operator has enabled PLS via env
 * (``OPENAPI_PLS_ENABLED=1``) but the Service does not yet carry the
 * ``azure-pls-create`` annotation — the next deploy must re-create the
 * Service (the AKS LB controller silently ignores in-place PLS annotation
 * updates) and the operator needs to acknowledge that explicitly.
 */
export interface OpenApiPlsStatus {
  available: boolean;
  pls_enabled_env: boolean;
  pls_name: string;
  service_exists: boolean | null;
  service_has_pls_annotation: boolean | null;
  transition_pending: boolean;
  confirm_recreate_required: boolean;
  reason?: string | null;
}

export interface AksAvailableSkusResponse {
  region: string;
  /** SKU names that the subscription can actually deploy in this region.
   *  The dropdown should treat anything missing from this list as
   *  ineligible regardless of whether it appears in the allow-list. */
  available: string[];
  /** Per-SKU reason rows for everything in the allow-list that is
   *  *not* deployable here. Used by the dropdown to render a tooltip
   *  with the Azure restriction code (e.g. NotAvailableForSubscription). */
  unavailable: {
    name: string;
    available: false;
    reason: string | null;
    location_restricted?: boolean;
  }[];
  /** True when the Azure listing failed entirely — the SPA should not
   *  filter the dropdown in that case (better to allow + hit the live
   *  BadRequest than to silently hide everything). */
  degraded: boolean;
}

export interface AksPreflightRequest {
  subscription_id: string;
  resource_group: string;
  region: string;
  cluster_name: string;
  node_sku: string;
  node_count: number;
  system_vm_size: string;
  system_node_count: number;
  /** Optional ACR / Storage targets so the backend can also report the
   *  `rbac_runtime` row (User Access Administrator on the runtime RBAC
   *  scopes). Leaving them empty makes that row a no-op `ok`. */
  acr_resource_group?: string;
  acr_name?: string;
  storage_resource_group?: string;
  storage_account?: string;
}

export interface AksPreflightCheck {
  /** Stable id — `region` | `skus` | `quota` | `resource_group`. */
  name: string;
  /** `ok` | `warn` | `fail`. The modal renders these with green / amber /
   *  red icons respectively. `fail` blocks submit; `warn` does not. */
  status: "ok" | "warn" | "fail";
  message: string;
  details?: Record<string, unknown>;
}

export interface AksPreflightResponse {
  /** False whenever at least one `checks[]` row has `status="fail"`. */
  ok: boolean;
  checks: AksPreflightCheck[];
  /** Azure portal deep link to the cluster's overview blade. Surfaced
   *  in the banner once provisioning starts and the resource is
   *  visible. */
  portal_url: string | null;
}

export interface AksLifecycleResponse {
  cluster_name?: string;
  task_id?: string;
  status: string;
}

export interface AksCancelProvisionResponse {
  task_id: string;
  job_id: string | null;
  /** Celery status the task was in when the request arrived. */
  previous_status: string;
  /** True when the task was still running (PENDING/RECEIVED/STARTED/RETRY)
   *  and the revoke signal was actually sent. False on a noop cancel of
   *  an already-terminal task. */
  was_running: boolean;
  cancelled: boolean;
  /** Approximate wait before the worker honors SIGTERM and the dashboard
   *  banner clears. Surfaced in the toast so the user understands the
   *  brief delay. */
  settle_after_seconds: number;
}

export interface AksRecentFailedProvision {
  job_id: string;
  task_id: string | null;
  status: string;
  phase: string | null;
  /** Raw Azure / Celery error string, truncated to 500 chars by the
   *  task. Feeds the dashboard's `armErrorClassifier` so the banner
   *  can render a friendly headline + portal link. */
  error_code: string | null;
  cluster_name: string | null;
  region: string | null;
  resource_group: string | null;
  subscription_id: string | null;
  created_at: string | null;
  updated_at: string | null;
}

export interface AksRecentFailedProvisionsResponse {
  jobs: AksRecentFailedProvision[];
  /** True when the state-repo query failed entirely. The FE should
   *  fall back to the localStorage source rather than treat an empty
   *  list as "no failures". */
  degraded: boolean;
}

/** Result of POST /api/aks/peer-with-platform — mirrors
 *  `ensure_vnet_peering_with_cluster`'s return shape. Both directions are
 *  reported; `error` populated when one direction failed (the other may
 *  still be Connected). `skipped` set when there is nothing to peer
 *  (BYO-VNet mode or env not resolved). */
export interface AksPeerWithPlatformResponse {
  dashboard_vnet?: string;
  aks_vnet?: string;
  node_resource_group?: string;
  peerings?: Array<{ direction: string; name: string; state: string }>;
  recovery_command?: string;
  skipped?: boolean;
  reason?: string;
  error?: string;
}

export const aksApi = {
  listSkus: () => api.get<AksSkuListResponse>("/aks/skus"),

  availableSkus: (subscriptionId: string, region: string) =>
    api.get<AksAvailableSkusResponse>(
      `/aks/available-skus?subscription_id=${encodeURIComponent(subscriptionId)}&region=${encodeURIComponent(region)}`,
    ),

  preflight: (req: AksPreflightRequest) =>
    api.post<AksPreflightResponse>("/aks/preflight", req),

  provision: (req: AksProvisionRequest) =>
    api.post<AksProvisionResponse>("/aks/provision", req),

  cancelProvision: (taskId: string) =>
    api.post<AksCancelProvisionResponse>(
      `/aks/cancel-provision/${encodeURIComponent(taskId)}`,
      {},
    ),

  recentFailedProvisions: (hours: number = 24, limit: number = 10) =>
    api.get<AksRecentFailedProvisionsResponse>(
      `/aks/recent-failed-provisions?hours=${hours}&limit=${limit}`,
    ),

  delete: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<AksLifecycleResponse>("/aks/delete", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
    }),

  start: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    autoWarmup?: Partial<AutoWarmupPreference>,
  ) =>
    api.post<AksLifecycleResponse>("/aks/start", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
      ...(autoWarmup ? { auto_warmup: autoWarmup } : {}),
    }),

  stop: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<AksLifecycleResponse>("/aks/stop", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
    }),

  assignRoles: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    acrRg: string,
    acrName: string,
    storageRg: string,
    storageAccount: string,
  ) =>
    api.post<{ kubelet_oid: string; roles_assigned: string[] }>(
      `/aks/${encodeURIComponent(clusterName)}/assign-roles`,
      {
        subscription_id: subscriptionId,
        resource_group: rg,
        acr_resource_group: acrRg,
        acr_name: acrName,
        storage_resource_group: storageRg,
        storage_account: storageAccount,
      },
    ),

  deployOpenApi: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    acrName?: string,
    storageAccount?: string,
    storageResourceGroup?: string,
    acrResourceGroup?: string,
    confirmRecreate?: boolean,
  ) =>
    api.post<{ id: string; statusQueryGetUri?: string }>("/aks/openapi/deploy", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
      acr_name: acrName,
      acr_resource_group: acrResourceGroup,
      storage_account: storageAccount,
      storage_resource_group: storageResourceGroup,
      ...(confirmRecreate ? { confirm_recreate: true } : {}),
    }),

  openApiDeployStatus: (instanceId: string) =>
    api.get<
      OrchestrationStatus<{
        cluster_name?: string;
        resource_group?: string;
        status?: string;
        openapi_deploy?: { error?: string };
        workload_identity?: { error?: string };
      }> & {
        /** Additive envelope-root recovery affordance — set when the
         *  failed task looks like an upstream-reach (VNet peering)
         *  problem. The SPA's RepairPeeringButton keys off this. */
        recovery_action?: string;
        recovery_hint?: string;
      }
    >(`/aks/openapi/deploy/${encodeURIComponent(instanceId)}/status`),

  /** Revoke a running ``deploy_openapi_service`` Celery task. Response
   *  shape mirrors {@link AksCancelProvisionResponse} so the SPA can
   *  reuse its cancel-toast UX. Idempotent — already-terminal tasks
   *  return 200 with ``was_running: false``. */
  cancelOpenApiDeploy: (taskId: string) =>
    api.post<AksCancelProvisionResponse>(
      `/aks/openapi/deploy/${encodeURIComponent(taskId)}/cancel`,
      {},
    ),

  proxyOpenApiSpec: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<Record<string, unknown>>(
      `/aks/openapi/spec?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  openApiDeployment: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<OpenApiDeploymentStatus>(
      `/aks/openapi/deployment?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  /** Live Private Link Service annotation state — used by the API
   * Reference page to render a "PLS transition pending" banner when the
   * deploy environment says PLS but the Service does not yet have the
   * annotation set. */
  openApiPls: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<OpenApiPlsStatus>(
      `/aks/openapi/pls?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  openApiToken: (subscriptionId: string, rg: string, clusterName: string) =>
    api.get<OpenApiTokenStatus>(
      `/aks/openapi/token?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  generateOpenApiToken: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    regenerate: boolean,
  ) =>
    api.post<OpenApiTokenStatus>("/aks/openapi/token", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
      regenerate,
    }),

  openApiPublicHttpsStatus: () =>
    api.get<OpenApiPublicHttpsStatus>("/aks/openapi/public-https"),

  /**
   * Fetch the operator-email validator rules (private-use TLDs the
   * backend rejects + canonical regex + max length). Used so the SPA
   * client gate cannot drift from the server rule when a new private
   * TLD is added in `_PRIVATE_USE_TLDS` without touching the SPA.
   */
  openApiOperatorEmailRules: () =>
    api.get<{ private_use_tlds: string[]; email_regex: string; max_length: number }>(
      "/aks/openapi/public-https/operator-email-rules",
    ),

  enableOpenApiPublicHttps: (
    subscriptionId: string,
    rg: string,
    clusterName: string,
    operatorEmail: string,
  ) =>
    api.post<{ id: string; task_id: string; statusQueryGetUri: string; status: string }>(
      "/aks/openapi/public-https",
      {
        subscription_id: subscriptionId,
        resource_group: rg,
        cluster_name: clusterName,
        operator_email: operatorEmail,
      },
    ),

  disableOpenApiPublicHttps: (subscriptionId: string, rg: string, clusterName: string) =>
    api.del<{ id: string; task_id: string; statusQueryGetUri: string; status: string }>(
      `/aks/openapi/public-https?subscription_id=${encodeURIComponent(subscriptionId)}&resource_group=${encodeURIComponent(rg)}&cluster_name=${encodeURIComponent(clusterName)}`,
    ),

  openApiPublicHttpsTaskStatus: (taskId: string) =>
    api.get<
      OrchestrationStatus<{
        status?: string;
        fqdn?: string;
        public_base_url?: string;
        ingress_lb_ip?: string;
        cert_expires_at?: string;
        error?: string;
      }>
    >(`/aks/openapi/public-https/${encodeURIComponent(taskId)}/status`),

  /** Re-create the bidirectional VNet peering between the dashboard
   *  platform VNet and the AKS-auto VNet. Wraps the same idempotent
   *  helper the AKS provision task runs at end-of-create, so re-running
   *  on an already-peered pair is a no-op. Use from the API Reference
   *  page when the OpenAPI spec / proxy / Try-It surfaces report
   *  `recovery_action === "peer_with_platform"`. */
  peerWithPlatform: (subscriptionId: string, rg: string, clusterName: string) =>
    api.post<AksPeerWithPlatformResponse>("/aks/peer-with-platform", {
      subscription_id: subscriptionId,
      resource_group: rg,
      cluster_name: clusterName,
    }),

  /** Idle auto-stop cost saver — opt-in toggle + countdown banner.
   *  See docs/features_change/2026-05/2026-05-29-aks-idle-auto-stop.md. */
  autoStop: {
    get: (subscriptionId: string, rg: string, clusterName: string) =>
      api.get<AutoStopPreferenceResponse>(
        `/aks/autostop?subscription_id=${encodeURIComponent(subscriptionId)}` +
          `&resource_group=${encodeURIComponent(rg)}` +
          `&cluster_name=${encodeURIComponent(clusterName)}`,
      ),
    save: (req: {
      subscription_id: string;
      resource_group: string;
      cluster_name: string;
      enabled: boolean;
      idle_minutes: number;
    }) => api.put<AutoStopPreferenceResponse>("/aks/autostop", req),
    extend: (
      subscriptionId: string,
      rg: string,
      clusterName: string,
      minutes: number = 30,
    ) =>
      api.post<AutoStopPreferenceResponse>("/aks/autostop/extend", {
        subscription_id: subscriptionId,
        resource_group: rg,
        cluster_name: clusterName,
        minutes,
      }),
    status: (subscriptionId: string, rg: string, clusterName: string) =>
      api.get<AutoStopStatusResponse>(
        `/aks/autostop/status?subscription_id=${encodeURIComponent(subscriptionId)}` +
          `&resource_group=${encodeURIComponent(rg)}` +
          `&cluster_name=${encodeURIComponent(clusterName)}`,
      ),
  },
};

/** Persisted opt-in idle-auto-stop preference for a single AKS cluster.
 *  Returned by `GET/PUT /api/aks/autostop` + `POST /api/aks/autostop/extend`. */
export interface AutoStopPreferenceResponse {
  exists: boolean;
  /** False when the row is owned by a different real user — the SPA
   *  MUST render the toggle as read-only in that case so the user
   *  doesn't try to mutate someone else's preference. */
  editable: boolean;
  /** Always present (server returns "" when row absent). */
  subscription_id: string;
  resource_group: string;
  cluster_name: string;
  enabled: boolean;
  /** Selected idle window in minutes. One of `allowed_idle_minutes`. */
  idle_minutes: number;
  /** Always present (0 when row absent). */
  cooldown_minutes: number;
  /** Buckets the backend will accept — drives the dropdown options. */
  allowed_idle_minutes: number[];
  /** ISO 8601 (UTC) of the most recent auto-stop, or "" if never stopped. */
  last_stop_at: string;
  last_stop_reason: string;
  last_skip_at: string;
  last_skip_reason: string;
  /** When non-empty, the user has temporarily extended this cluster — the
   *  beat task will skip auto-stop until this timestamp passes. */
  extend_until: string;
  updated_at: string;
  /** True when the read fell back to default values because the backend
   *  storage layer was unreachable; the SPA should disable the Save button. */
  degraded?: boolean;
}

/** Live evaluator verdict driving the SPA banner / cluster card chip.
 *  Returned by `GET /api/aks/autostop/status`. */
export interface AutoStopStatusResponse {
  exists: boolean;
  /** False for foreign-owned rows — the SPA should hide the banner. */
  editable: boolean;
  enabled: boolean;
  idle_minutes: number;
  /** "stop" → cluster is being stopped now;
   *  "warn" → SPA should render the countdown banner;
   *  "keep" → idle clock running but plenty of time left;
   *  "disabled" → preference exists but `enabled=false` (no banner). */
  verdict: "stop" | "warn" | "keep" | "disabled";
  /** Free-form code (e.g. ``idle:60m``, ``active_jobs:2``,
   *  ``cooldown``, ``extended``, ``power_state:Stopped``). Surfaced in
   *  the banner tooltip. */
  reason: string;
  /** ISO 8601 (UTC) of the *projected* next stop, or "" when no
   *  stop is on the horizon (verdict ∈ {keep, disabled}). */
  next_stop_at: string;
  /** Convenience seconds-to-`next_stop_at`. 0 when not applicable. */
  seconds_until_stop: number;
  active_job_count: number;
  cluster_power_state: string;
  last_stop_at: string;
  last_skip_at: string;
  extend_until: string;
}
