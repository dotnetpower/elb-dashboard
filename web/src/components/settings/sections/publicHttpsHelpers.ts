/**
 * Pure helpers + phase metadata for {@link PublicHttpsSection} — extracted from
 * the section component (issue #24).
 *
 * Owns: the Let's Encrypt contact-email validation gate (mirrors the backend
 * `_PRIVATE_USE_TLDS` so the client cannot drift), the ordered public-HTTPS
 * setup phase list emitted by `setup_openapi_public_https.record_progress(...)`
 * in `api/tasks/openapi/public_https.py`, the phase lookup, and the elapsed
 * time formatter. No React, no side effects beyond the module-local mutable
 * `PRIVATE_USE_TLDS` set the panel refreshes from the canonical backend list on
 * mount via `setPrivateUseTlds(...)`.
 */

// Let's Encrypt rejects ACME account registration when the contact
// email's domain has no public TLD (`urn:ietf:params:acme:error:invalidContact`).
// `.local` / `.localhost` / `.internal` / `.test` / `.example` / `.invalid`
// are the IANA reserved private-use TLDs that trip it most often in our
// deployments (the old `_FALLBACK_OPERATOR_EMAIL = noreply@elb-dashboard.local`
// hit exactly this on elb-cluster-01 on 2026-05-27). The set below is the
// SPA's fallback; on mount the panel fetches the canonical list from
// `aksApi.openApiOperatorEmailRules()` so the client gate cannot drift
// when a new TLD is added to the backend-only `_PRIVATE_USE_TLDS`.
export const DEFAULT_PRIVATE_USE_TLDS: readonly string[] = [
  "local",
  "localhost",
  "internal",
  "test",
  "example",
  "invalid",
  "lan",
  "home",
  "corp",
  "private",
];
let PRIVATE_USE_TLDS = new Set(DEFAULT_PRIVATE_USE_TLDS);

/**
 * Replace the private-use TLD set. Called by the panel on mount with the
 * union of the fallback list and the canonical backend list, and by tests.
 */
export function setPrivateUseTlds(values: readonly string[]): void {
  PRIVATE_USE_TLDS = new Set(values);
}

export function isPublicLetsEncryptEmail(value: string): boolean {
  const text = (value ?? "").trim();
  if (!text || text.length > 254) return false;
  const at = text.indexOf("@");
  if (at <= 0 || at !== text.lastIndexOf("@")) return false;
  const local = text.slice(0, at);
  const domain = text.slice(at + 1).toLowerCase();
  if (!local || !domain || domain.includes("..") || domain.endsWith(".")) return false;
  const labels = domain.split(".");
  if (labels.length < 2 || labels.some((label) => !label)) return false;
  const tld = labels[labels.length - 1];
  if (!/^[a-z]{2,}$/.test(tld)) return false;
  if (PRIVATE_USE_TLDS.has(tld)) return false;
  return /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+$/.test(text);
}

/**
 * Validate the optional public-HTTPS custom domain. Empty is VALID (the
 * endpoint falls back to the auto-generated `*.cloudapp.azure.com` FQDN).
 * A non-empty value must be a bare public-TLD FQDN (no scheme/path/port) —
 * Let's Encrypt rejects private-use TLDs, so they are gated here too. Mirrors
 * the backend `_validate_custom_domain` so the Enable button reflects the same
 * rule before the request leaves the browser.
 */
export function isValidCustomDomain(value: string): boolean {
  let host = (value ?? "").trim().toLowerCase().replace(/\/+$/, "");
  if (!host) return true; // optional
  for (const scheme of ["https://", "http://"]) {
    if (host.startsWith(scheme)) {
      host = host.slice(scheme.length);
      break;
    }
  }
  host = host.split("/", 1)[0];
  const fqdnRe =
    /^(?=.{1,253}$)[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?(\.[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?)+$/;
  if (!fqdnRe.test(host)) return false;
  const tld = host.split(".").pop() ?? "";
  return !PRIVATE_USE_TLDS.has(tld);
}

// Ordered phases emitted by `setup_openapi_public_https.record_progress(...)`
// in api/tasks/openapi/public_https.py. Keep the ids in sync with the task;
// the SPA renders the stepper from this list and falls back to the raw
// phase id when the backend ships a new phase before the SPA catches up.
export interface PublicHttpsPhaseMeta {
  id: string;
  label: string;
  hint?: string;
}
export const PUBLIC_HTTPS_PHASES: PublicHttpsPhaseMeta[] = [
  { id: "queued", label: "Queued", hint: "Waiting for a Celery worker slot." },
  { id: "ensure_kubeconfig", label: "Fetching AKS kubeconfig", hint: "Resolving cluster-admin credentials via ARM." },
  { id: "install_ingress_nginx", label: "Installing ingress-nginx", hint: "Applying the upstream manifest patched for the blastpool." },
  { id: "patch_dns_label", label: "Patching Azure DNS label", hint: "Annotating the LoadBalancer Service with a stable FQDN." },
  { id: "wait_external_ip", label: "Waiting for public IP", hint: "Azure assigns the LB's EXTERNAL-IP. Usually 30-90s." },
  { id: "ensure_node_subnet_nsg", label: "Opening node-subnet firewall", hint: "Allowing Internet 80/443 to the LB on the BYO node-subnet NSG so the ACME challenge can reach ingress." },
  { id: "ensure_custom_domain_dns", label: "Configuring custom domain DNS", hint: "Upserting the CNAME/A record in your Azure DNS zone (skipped when no custom domain is set)." },
  { id: "install_cert_manager", label: "Installing cert-manager", hint: "Applying CRDs + controller + webhook." },
  { id: "wait_cert_manager_webhook", label: "Waiting for cert-manager webhook", hint: "Webhook Pod must become Available before any Issuer applies." },
  { id: "apply_cluster_issuer", label: "Applying ClusterIssuer", hint: "Registering the Let's Encrypt prod ACME account." },
  { id: "wait_ingress_nginx_controller", label: "Waiting for ingress-nginx controller", hint: "Controller Pod must be Ready before the admission webhook can validate Ingresses." },
  { id: "wait_admission_jobs_complete", label: "Waiting for admission bootstrap Jobs", hint: "Generates the admission webhook's TLS keypair." },
  { id: "wait_admission_endpoints_ready", label: "Waiting for admission endpoints", hint: "EndpointSlice must list the controller Pod before applying the Ingress." },
  { id: "apply_ingress", label: "Applying elb-openapi Ingress", hint: "Routing the cloudapp.azure.com FQDN to the in-cluster Service." },
  { id: "wait_certificate_ready", label: "Waiting for TLS certificate", hint: "Let's Encrypt HTTP-01 challenge. First-time issuance takes 1-3 min." },
  { id: "persist_runtime_cache", label: "Saving runtime cache", hint: "Storing the public base URL so the dashboard flips to HTTPS." },
];
const PUBLIC_HTTPS_PHASE_INDEX = new Map(PUBLIC_HTTPS_PHASES.map((p, i) => [p.id, i] as const));

export function lookupPublicHttpsPhase(phase: string): {
  meta: PublicHttpsPhaseMeta;
  index: number;
  total: number;
} {
  const idx = PUBLIC_HTTPS_PHASE_INDEX.get(phase) ?? 0;
  const meta = PUBLIC_HTTPS_PHASES[idx] ?? {
    id: phase || "queued",
    label: phase ? phase.replace(/_/g, " ") : "Queued",
  };
  return { meta, index: idx, total: PUBLIC_HTTPS_PHASES.length };
}

export function formatElapsedSeconds(seconds: number): string {
  if (!Number.isFinite(seconds) || seconds < 0) return "0s";
  const s = Math.floor(seconds);
  if (s < 60) return `${s}s`;
  const m = Math.floor(s / 60);
  const rem = s % 60;
  return rem === 0 ? `${m}m` : `${m}m ${rem}s`;
}
