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
  ];
}
