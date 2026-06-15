import type { SpecEndpoint } from "@/pages/apiReference/types";

/**
 * Static endpoint definitions for the always-on "Core" control-plane section
 * of the API Reference.
 *
 * Unlike the rest of the page (which is parsed from the live `elb-openapi`
 * OpenAPI document hosted INSIDE the AKS cluster), these endpoints live on the
 * dashboard's own `api` sidecar — a different host that stays up even while the
 * cluster (and therefore `elb-openapi`) is stopped. They are defined here
 * rather than discovered from a spec precisely because they must remain
 * documented + executable when the cluster is down.
 *
 * The example request bodies are seeded with the caller's resolved cluster
 * context so the "Send Request" button is effectively one-click, mirroring the
 * `/healthz`-style instant Try-it experience.
 */

export interface CoreApiContext {
  subscriptionId: string;
  resourceGroup: string;
  clusterName: string;
}

export function buildCoreEndpoints(ctx: CoreApiContext): SpecEndpoint[] {
  const target = {
    subscription_id: ctx.subscriptionId,
    resource_group: ctx.resourceGroup,
    cluster_name: ctx.clusterName,
  };
  return [
    {
      method: "post",
      path: "/api/aks/openapi/ensure-running",
      summary: "Bring the OpenAPI cluster up and report its serving phase",
      description:
        "Wake-on-request gate for the AKS cluster that hosts elb-openapi. Because " +
        "the OpenAPI service runs inside the cluster, a stopped cluster cannot " +
        "answer its own requests — so this endpoint lives on the always-on " +
        "dashboard api sidecar instead. Poll it until status='ready'.\n\n" +
        "status transitions:\n" +
        "  • stopped  — cluster is stopped; a start is enqueued (start_triggered=true)\n" +
        "  • starting — start operation in progress\n" +
        "  • warming  — Running, but the warmup nodes are not all Ready yet, or a " +
        "configured database is still loading onto the node-local SSD\n" +
        "  • ready    — Running and warmed; safe to submit\n" +
        "  • not_found / unknown — cluster missing / ARM unreachable (no start)\n\n" +
        "Pass start=false to observe the phase without triggering a start.\n\n" +
        "⚠️ IMPORTANT — this is STRONGER than the upstream elb-openapi /v1/ready " +
        "probe. /v1/ready returns ready=true as soon as the K8s API, a Ready " +
        "workload node, AND the elb-openapi pod are up — it does NOT wait for the " +
        "node-local BLAST DB warmup, so a /v1/jobs submitted right after /v1/ready " +
        "still works but falls back to the slow on-node SSD DB init. This " +
        "endpoint's status='ready' additionally waits for the configured warmup " +
        "nodes to be Ready AND for every configured database to finish its " +
        "node-local warmup, so 'ready' here means the cluster is warmed and " +
        "/v1/jobs runs at full speed. (When no warmup preference is configured " +
        "there is nothing to warm, so a Running cluster reports 'ready' " +
        "immediately — same as /v1/ready.)\n\n" +
        "Warmup is best-effort: a database whose warmup is terminally Failed (a " +
        "cause retrying cannot fix) does NOT block 'ready' forever — the cluster " +
        "reports 'ready' with warmup.phase='ready_degraded' and the failed set in " +
        "warmup.failed_databases, and /v1/jobs for that database falls back to the " +
        "slow on-node init. NOTE: 'ready' confirms each database is warm, not that " +
        "it is the latest NCBI generation — the per-submit gate owns the " +
        "generation check, so a brand-new snapshot may still be re-warming.",
      tags: ["Core"],
      parameters: [],
      requestBody: {
        required: true,
        content: {
          "application/json": {
            schema: {
              type: "object",
              required: ["resource_group", "cluster_name"],
            },
            examples: {
              ensure_running: {
                summary: "Ensure running (start if stopped)",
                description:
                  "Reports the current phase and starts the cluster when it is " +
                  "fully stopped. Poll until status='ready' — and note 'ready' " +
                  "here waits for the BLAST DB warmup, NOT just a started cluster " +
                  "(unlike the upstream /v1/ready probe).",
                value: target,
              },
              observe_only: {
                summary: "Observe phase only (do not start)",
                description:
                  "Returns the phase without enqueuing a start. Use this to read " +
                  "warmup progress (status='warming' vs 'ready') without spending " +
                  "a cluster start.",
                value: { ...target, start: false },
              },
            },
          },
        },
      },
      responses: {
        "200": {
          description:
            "Current serving phase. Poll until status='ready'. ⚠️ status='ready' " +
            "means Running AND warmed (warmup nodes Ready AND every configured " +
            "database warmed onto the node-local SSD, or terminally Failed) — " +
            "stricter than the upstream /v1/ready. status='warming' = started but " +
            "the warmup is still running; 'warmup' carries ready_node_count / " +
            "expected_node_count for node progress and, once the nodes are Ready, " +
            "databases_ready / databases_total / pending_databases / " +
            "failed_databases for per-database progress.",
          fields: [
            "status",
            "start_triggered",
            "start_task_id",
            "warmup",
            "retry_after_seconds",
            "message",
          ],
          example: {
            status: "warming",
            cluster: {
              subscription_id: ctx.subscriptionId,
              resource_group: ctx.resourceGroup,
              name: ctx.clusterName,
              power_state: "Running",
              provisioning_state: "Succeeded",
              exists: true,
            },
            start_triggered: false,
            start_task_id: null,
            warmup: {
              ready: false,
              phase: "warming_databases",
              expected_node_count: 2,
              ready_node_count: 2,
              databases_total: 2,
              databases_ready: 1,
              pending_databases: [{ db: "core_nt", status: "Loading" }],
              failed_databases: [],
            },
            retry_after_seconds: 15,
            message: "warmup databases still loading: core_nt",
          },
        },
        "400": {
          description: "resource_group and cluster_name are required.",
          example: {
            status: "error",
            code: "missing_parameters",
            message:
              "subscription_id (or AZURE_SUBSCRIPTION_ID env), resource_group " +
              "and cluster_name are required.",
          },
        },
      },
    },
    {
      method: "get",
      path: "/api/aks/openapi/databases",
      summary: "List prepared BLAST databases (cluster-independent)",
      description:
        "Promotes the elb-openapi GET /v1/databases catalogue read to the " +
        "always-on dashboard api sidecar. Because the catalogue is sourced " +
        "directly from the workspace Storage account (which the api sidecar " +
        "reaches over its private endpoint), this answers even while the AKS " +
        "cluster — and therefore the in-cluster elb-openapi service — is " +
        "stopped.\n\n" +
        "The Storage scope (account / resource group / subscription) is " +
        "resolved automatically from the deployed api sidecar's environment " +
        "(STORAGE_ACCOUNT_NAME / AZURE_RESOURCE_GROUP / AZURE_SUBSCRIPTION_ID), " +
        "so this is a one-click call against this deployment's own workload " +
        "account — no parameters to fill in. (Direct external callers may still " +
        "override via storage_account / resource_group / subscription_id query " +
        "params.) The response mirrors elb-openapi's shape: " +
        "{ databases: [{ name }], count, container }.",
      tags: ["Core"],
      parameters: [],
      responses: {
        "200": {
          description:
            "The prepared-database catalogue (drop-in for elb-openapi " +
            "GET /v1/databases).",
          fields: ["databases", "count", "container"],
          example: {
            databases: [{ name: "16S_ribosomal_RNA" }, { name: "core_nt" }],
            count: 2,
            container: "blast-db",
          },
        },
        "400": {
          description:
            "storage_account is missing and no STORAGE_ACCOUNT_NAME env is set.",
          example: {
            status: "error",
            code: "missing_parameters",
            message: "storage_account (or STORAGE_ACCOUNT_NAME env) is required.",
            databases: [],
            count: 0,
            container: "blast-db",
          },
        },
        "503": {
          description:
            "Storage data plane is unreachable (e.g. network-blocked while " +
            "debugging from a laptop). Degraded, never a 500.",
          example: {
            databases: [],
            count: 0,
            container: "blast-db",
            degraded: true,
            degraded_reason: "network_blocked",
          },
        },
      },
    },
    {
      method: "get",
      path: "/api/aks/openapi/databases/{db_name}",
      summary: "Get BLAST database metadata (cluster-independent)",
      description:
        "Promotes the elb-openapi GET /v1/databases/{db_name} metadata read to " +
        "the always-on dashboard api sidecar, sourced from Storage so it " +
        "answers while the cluster is stopped. The response mirrors the " +
        "elb-openapi DatabaseMetadata shape (name, molecule_type in " +
        "{dna, protein} plus molecule_label, snapshot, title, sequence/letter " +
        "counts, byte sizes, cached_at).\n\n" +
        "The Storage scope is resolved automatically from the deployed api " +
        "sidecar's environment like the list endpoint, so only db_name is " +
        "needed. Unknown db_name returns 404; an invalid name returns 400; a " +
        "transient Storage outage returns a degraded 503.",
      tags: ["Core"],
      parameters: [
        {
          name: "db_name",
          in: "path",
          required: true,
          description:
            "Database name as listed by GET /api/aks/openapi/databases " +
            "(e.g. core_nt, nr, 16S_ribosomal_RNA).",
          schema: { type: "string", default: "core_nt" },
        },
      ],
      responses: {
        "200": {
          description:
            "Single-database metadata (drop-in for elb-openapi " +
            "GET /v1/databases/{db_name}).",
          fields: [
            "name",
            "container",
            "title",
            "molecule_type",
            "molecule_label",
            "snapshot",
            "number_of_sequences",
            "number_of_letters",
            "cached_at",
          ],
          example: {
            name: "core_nt",
            container: "blast-db",
            title: "Core nucleotide BLAST database",
            dbtype: "Nucleotide",
            molecule_type: "dna",
            molecule_label: "mixed DNA",
            snapshot: "2026-06-01",
            number_of_sequences: 102_456_789,
            number_of_letters: 1_234_567_890,
            cached_at: "2026-06-15T00:00:00+00:00",
          },
        },
        "404": {
          description: "No database with that name exists in the catalogue.",
          example: {
            status: "error",
            code: "not_found",
            message: "Database 'core_nt' not found.",
          },
        },
      },
    },
  ];
}
