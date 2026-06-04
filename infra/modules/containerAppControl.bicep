// Single bundled Container App `ca-elb-dashboard` with all six sidecars.
//
// minReplicas: 1, maxReplicas: 1  (beat singleton + Redis state locality)
// Public ingress on :8080 routed to the api sidecar.
//
// Bootstrap: when `useBootstrapImage=true`, all sidecars boot from a public
// hello-world image so the Container App provisions before the real ACR
// images exist. The postprovision hook then runs `az containerapp update`
// to swap each sidecar to its real image.

@description('Azure region.')
param location string

@description('Container App name (e.g. ca-elb-dashboard).')
param appName string

@description('Resource id of the Container Apps Environment.')
param environmentResourceId string

@description('Login server for the platform ACR.')
param acrLoginServer string

@description('Tag of the api image in the platform ACR.')
param apiImageTag string = 'latest'

@description('Tag of the frontend image in the platform ACR.')
param frontendImageTag string = 'latest'

@description('Tag of the terminal image in the platform ACR.')
param terminalImageTag string = 'latest'

@description('If true, all sidecars boot from a public hello-world image. Postprovision hook flips this to false via az containerapp update.')
param useBootstrapImage bool = true

@description('Resource id of the user-assigned managed identity shared by all sidecars.')
param sharedIdentityResourceId string

@description('Client id of the same UAMI (used by azure-identity inside the containers).')
param sharedIdentityClientId string

@description('Principal/object id of the same UAMI (used for runtime RBAC assignments).')
param sharedIdentityPrincipalId string = ''

@description('AAD tenant id used to validate MSAL bearer tokens.')
param tenantId string

@description('App Registration client id (audience) for the api.')
param apiClientId string

@description('Frontend feature flag for the custom database builder. Set to false to hide menu entries and route access.')
param featureCustomDb string = 'true'

@description('Frontend feature flag for lab tools. Set to false to hide menu entries and route access.')
param featureLabTools string = 'true'

@description('Frontend feature flag for the browser terminal. Set to false to hide menu entries, dashboard card, shortcuts, and route access.')
param featureTerminal string = 'true'

@description('App Insights connection string for telemetry from inside the containers.')
param applicationInsightsConnectionString string

@description('Log Analytics workspace id (customerId GUID). Used by the api sidecar to KQL `ContainerAppConsoleLogs_CL` for the Live Wall log tail when the historical project-local log files are not available (i.e. always, in deployment). Empty disables the LA fallback and the Live Wall log tiles stay blank.')
param logAnalyticsWorkspaceId string = ''

@description('Platform Storage account name (used to derive the table endpoint for jobstate / jobhistory access).')
param platformStorageAccountName string = ''

@description('Resource id of the platform subnet where workload Storage private endpoints are created for api/worker/terminal access.')
param platformPrivateEndpointSubnetId string = ''

@description('Resource id of the hub VNet snet-aks subnet. New AKS clusters are created in this subnet (BYO-subnet) so their nodes resolve and route to the workload Storage private endpoints intra-VNet. Empty falls back to managed-VNet mode (no Storage connectivity).')
param platformAksSubnetId string = ''

@description('Subscription id (passed into the api/worker env vars so monitor routes can default subscription_id when not provided in the query string).')
param subscriptionId string = subscription().subscriptionId

@description('CORS allowed origins for the api ingress. Empty list disables CORS (same-origin only).')
param allowedOrigins array = []

@description('Tags applied to every resource in this module.')
param tags object = {}

@secure()
@description('Shared secret used by the api / worker sidecars to authenticate with the terminal exec server (loopback :7682). Auto-rotated on every deployment via newGuid(); both sidecars receive the same Container Apps secret reference so they always agree.')
param execToken string = newGuid()

var moduleTags = union(tags, {
  role: 'control-plane'
})

var bootstrapImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var apiImage      = useBootstrapImage ? bootstrapImage : '${acrLoginServer}/elb-api:${apiImageTag}'
var frontendImage = useBootstrapImage ? bootstrapImage : '${acrLoginServer}/elb-frontend:${frontendImageTag}'
var terminalImage = useBootstrapImage ? bootstrapImage : '${acrLoginServer}/elb-terminal:${terminalImageTag}'

var storageDnsSuffix = environment().suffixes.storage
var tableEndpoint = empty(platformStorageAccountName) ? '' : 'https://${platformStorageAccountName}.table.${storageDnsSuffix}'
var blobEndpoint = empty(platformStorageAccountName) ? '' : 'https://${platformStorageAccountName}.blob.${storageDnsSuffix}'
var platformResourceGroupName = resourceGroup().name
// ACR short name (registry label) derived from the login server, e.g.
// `acrelbdashboard.azurecr.io` -> `acrelbdashboard`. Used by the terminal
// sidecar's `elb-cfg` helper to seed `azure-acr-name` defaults so a
// researcher does not have to remember the registry name by hand.
var platformAcrName = empty(acrLoginServer) ? '' : split(acrLoginServer, '.')[0]

resource controlApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: moduleTags
  identity: {
    type: 'UserAssigned'
    userAssignedIdentities: {
      '${sharedIdentityResourceId}': {}
    }
  }
  properties: {
    environmentId: environmentResourceId
    workloadProfileName: 'Consumption'
    configuration: {
      // Single-revision mode: one revision serves 100% of traffic and every
      // template change recreates it in place. This is the legacy
      // self-upgrade posture and the steady state while STRICT_BLUEGREEN is
      // OFF (default).
      //
      // Native blue/green (STRICT_BLUEGREEN=true) requires
      // `activeRevisionsMode: 'Multiple'` so the runtime can stage a green
      // revision at 0% traffic, cut over, and flip back to the warm blue
      // revision in seconds. This flip is intentionally NOT applied here yet
      // because it is irreversible-by-provision and carries two hazards that
      // must be handled operationally first:
      //   1. Regression guard — Multiple mode with STRICT_BLUEGREEN still OFF
      //      gives every new revision 0% traffic by default, so the legacy
      //      in-place recreate would silently never take traffic. The two
      //      switches MUST be flipped together.
      //   2. IaC vs runtime traffic ownership — in Multiple mode the upgrade
      //      reconciler mutates the `traffic` array at runtime (pin/cutover/
      //      flip). A declarative `traffic` block here would be reset by the
      //      next `azd provision`, which during a confirm window or after a
      //      rollback would yank traffic to the wrong revision. The cutover
      //      must therefore stay runtime-owned (no static traffic block), and
      //      operators must avoid `azd provision` mid-cutover.
      // See docs/features_change/2026-06 for the rollout runbook.
      activeRevisionsMode: 'Single'
      secrets: [
        // Shared secret for the loopback exec channel between the api/worker
        // sidecars and the terminal sidecar's exec server. Container Apps
        // stores secrets encrypted at rest; both sidecars receive it via
        // `secretRef` below so the value never appears in env-var listings.
        {
          name: 'exec-token'
          value: execToken
        }
      ]
      ingress: {
        external: true
        targetPort: 8080
        transport: 'auto'
        allowInsecure: false
        corsPolicy: empty(allowedOrigins) ? null : {
          allowedOrigins: allowedOrigins
          allowedMethods: [ 'GET', 'POST', 'PUT', 'DELETE', 'OPTIONS' ]
          allowedHeaders: [ '*' ]
          allowCredentials: false
        }
      }
      registries: useBootstrapImage ? [] : [
        {
          server: acrLoginServer
          identity: sharedIdentityResourceId
        }
      ]
    }
    template: {
      containers: useBootstrapImage ? [
        // During bootstrap, the Container App must satisfy the per-replica
        // 1 vCPU : 2 GiB ratio. We ship a single hello-world container at
        // the smallest valid size; the postprovision hook then replaces the
        // entire template with the six-sidecar layout below.
        {
          name: 'bootstrap'
          image: bootstrapImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
      ] : [
        // -------------------------------------------------------------------
        // 1. api sidecar  (public ingress → :8080)
        // -------------------------------------------------------------------
        {
          name: 'api'
          image: apiImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            { name: 'SIDECAR_NAME', value: 'api' }
            { name: 'OPS_REDIS_URL', value: 'redis://127.0.0.1:6379/2' }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'API_CLIENT_ID', value: apiClientId }
            { name: 'AZURE_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'SHARED_IDENTITY_PRINCIPAL_ID', value: sharedIdentityPrincipalId }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
            { name: 'AZURE_RESOURCE_GROUP', value: platformResourceGroupName }
            { name: 'AZURE_TABLE_ENDPOINT', value: tableEndpoint }
            { name: 'AZURE_BLOB_ENDPOINT', value: blobEndpoint }
            { name: 'PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID', value: platformPrivateEndpointSubnetId }
            { name: 'PLATFORM_AKS_SUBNET_ID', value: platformAksSubnetId }
            { name: 'PLATFORM_PRIVATE_DNS_ZONE_RESOURCE_GROUP', value: platformResourceGroupName }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: applicationInsightsConnectionString }
            { name: 'CELERY_BROKER_URL', value: 'redis://127.0.0.1:6379/0' }
            { name: 'CELERY_RESULT_BACKEND', value: 'redis://127.0.0.1:6379/1' }
            { name: 'FRONTEND_UPSTREAM', value: 'http://127.0.0.1:8081' }
            { name: 'TERMINAL_UPSTREAM', value: 'http://127.0.0.1:7681' }
            { name: 'TERMINAL_EXEC_UPSTREAM', value: 'http://127.0.0.1:7682' }
            // Live Wall log-tail fallback target. When non-empty,
            // `api.services.sidecar_logs` switches from local file tailing
            // to KQL against the LA workspace. Empty disables the fallback
            // and the tiles render `: ready` + heartbeats only.
            { name: 'LOG_ANALYTICS_WORKSPACE_ID', value: logAnalyticsWorkspaceId }
            // Display-only contract for /api/terminal/ticket. The actual
            // browser caller comes from the validated MSAL token; the shell
            // process itself runs as this fixed Unix account in the terminal
            // sidecar.
            { name: 'TERMINAL_SHELL_USER', value: 'azureuser' }
            { name: 'EXEC_TOKEN', secretRef: 'exec-token' }
            // Default deploy exposes `elb-openapi` as a public LoadBalancer
            // (see api/tasks/openapi/__init__.py `_build_manifests`). The
            // proxy guard added by security audit #12 (2026-05-22) would
            // otherwise refuse every API menu call with 502
            // `openapi_unsafe_transport`. Opt-in unblocks the dashboard;
            // flip back to `false` (or remove this entry) once the Service
            // is moved behind an internal LB or TLS-terminated ingress.
            { name: 'OPENAPI_ALLOW_PUBLIC_LB', value: 'true' }
            // OpenAPI proxy execution RBAC gate (security audit, 2026-05-31).
            // Default OFF preserves the legacy behaviour where any
            // authenticated tenant member can drive state-changing OpenAPI
            // "Try it" / curl calls through the admin token (Charter §12a
            // Rule 4). Flip to 'true' to require the caller to hold a write
            // role (Contributor / Owner / AKS write) on the target resource
            // group before forwarding POST/PUT/PATCH/DELETE. When enabled
            // the api managed identity needs
            // Microsoft.Authorization/roleAssignments/read at the
            // subscription scope (the Reader built-in grants this).
            { name: 'ENFORCE_OPENAPI_EXEC_RBAC', value: 'false' }
            // BLAST capacity gate (issue #23). Default OFF preserves the
            // existing per-cluster Redis submit lock + max_slots=1 behaviour
            // (Charter §12a Rule 4). Flip to 'true' to enable cluster-aware
            // admission. Optional knobs (env_int defaults apply when unset):
            //   BLAST_GATE_MAX_SLOTS_PER_CLUSTER (default 1)
            //   BLAST_GATE_CPU_WATERMARK_PCT     (default 75)
            //   BLAST_GATE_MEM_WATERMARK_PCT     (default 75)
            //   BLAST_GATE_SIGNAL_CACHE_S        (default 30)
            { name: 'BLAST_GATE_ENABLED', value: 'false' }
            // Dev-stage job visibility (issue: recent searches only showed
            // API-submitted jobs). Default OFF preserves per-owner BLAST job
            // isolation (Charter §12a Rule 4). Flip to 'true' to let every
            // authenticated tenant member see and open all jobs regardless of
            // `owner_oid` — intended for the single-tenant development phase
            // only. Flip back to 'false' before multi-user production.
            { name: 'BLAST_JOBS_SHARED_VISIBILITY', value: 'false' }
            // Native ACA blue/green self-upgrade (issue: guaranteed rollback +
            // no leftover revisions). Default OFF preserves the legacy
            // Single-mode in-place revision recreate (Charter §12a Rule 4):
            // pipeline/reconciler/rollback all branch on this flag, so OFF is
            // a zero-regression no-op. Flipping to 'true' REQUIRES the
            // Container App to also run in `activeRevisionsMode: Multiple`
            // (see the configuration block above) — otherwise traffic always
            // follows the latest revision and the traffic-flip rollback /
            // GC cannot work. Optional knobs (read at call time; defaults
            // apply when unset — see reconciler.py validating_timeout_seconds()
            // / confirm_window_seconds() and revision_gc.keep_n_revisions()):
            //   UPGRADE_VALIDATING_TIMEOUT_SECONDS (default 900)
            //   UPGRADE_CONFIRM_WINDOW_SECONDS (default 300)
            //   UPGRADE_REVISION_KEEP_N (default 2)
            { name: 'STRICT_BLUEGREEN', value: 'false' }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/api/health', port: 8080, scheme: 'HTTP' }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/api/health', port: 8080, scheme: 'HTTP' }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
        }
        // -------------------------------------------------------------------
        // 2. frontend sidecar  (loopback :8081, served via api reverse proxy)
        // -------------------------------------------------------------------
        {
          name: 'frontend'
          image: frontendImage
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'SIDECAR_NAME', value: 'frontend' }
            { name: 'OPS_REDIS_URL', value: 'redis://127.0.0.1:6379/2' }
            { name: 'VITE_API_BASE_URL', value: '' }
            { name: 'VITE_AUTH_DEV_BYPASS', value: 'false' }
            { name: 'VITE_AZURE_REDIRECT_URI', value: '__RUNTIME__' }
            { name: 'VITE_AZURE_TENANT_ID', value: tenantId }
            { name: 'VITE_AZURE_CLIENT_ID', value: apiClientId }
            { name: 'VITE_FEATURE_CUSTOM_DB', value: featureCustomDb }
            { name: 'VITE_FEATURE_LAB_TOOLS', value: featureLabTools }
            { name: 'VITE_FEATURE_TERMINAL', value: featureTerminal }
            { name: 'API_CLIENT_ID', value: apiClientId }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
        }
        // -------------------------------------------------------------------
        // 3. worker sidecar  (Celery worker, same image as api)
        // -------------------------------------------------------------------
        {
          name: 'worker'
          image: apiImage
          command: [ 'python3', '/app/api/wait_redis.py' ]
          args: [
            'python3'
            '/app/api/run_celery_workers.py'
          ]
          // The worker sidecar runs run_celery_workers.py, which spawns TWO
          // celery parents (worker-main @ concurrency 4 + worker-artifacts @
          // concurrency 2) = 2 parents + 6 prefork children = 8 Python
          // processes. At 0.5 vCPU / 1.0Gi that pool is heavily
          // over-subscribed; bump to 1.0 vCPU / 2.0Gi so the prefork children
          // are not starved when several azure / blast / storage tasks run at
          // once. New per-replica total = 2.75 vCPU / 5.5Gi (api 0.5/1.0,
          // frontend 0.25/0.5, worker 1.0/2.0, beat 0.25/0.5, redis 0.25/0.5,
          // terminal 0.5/1.0), still under the Consumption 4 vCPU / 8Gi cap
          // and keeping the 1 vCPU : 2 GiB ratio.
          resources: {
            cpu: json('1.0')
            memory: '2.0Gi'
          }
          env: [
            { name: 'SIDECAR_NAME', value: 'worker' }
            { name: 'OPS_REDIS_URL', value: 'redis://127.0.0.1:6379/2' }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'API_CLIENT_ID', value: apiClientId }
            { name: 'AZURE_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'SHARED_IDENTITY_PRINCIPAL_ID', value: sharedIdentityPrincipalId }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
            { name: 'AZURE_RESOURCE_GROUP', value: platformResourceGroupName }
            { name: 'AZURE_TABLE_ENDPOINT', value: tableEndpoint }
            { name: 'AZURE_BLOB_ENDPOINT', value: blobEndpoint }
            { name: 'PLATFORM_PRIVATE_ENDPOINT_SUBNET_ID', value: platformPrivateEndpointSubnetId }
            { name: 'PLATFORM_AKS_SUBNET_ID', value: platformAksSubnetId }
            { name: 'PLATFORM_PRIVATE_DNS_ZONE_RESOURCE_GROUP', value: platformResourceGroupName }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: applicationInsightsConnectionString }
            { name: 'CELERY_BROKER_URL', value: 'redis://127.0.0.1:6379/0' }
            { name: 'CELERY_RESULT_BACKEND', value: 'redis://127.0.0.1:6379/1' }
            // Worker calls api.services.terminal_exec which needs the same
            // shared secret as the api sidecar.
            { name: 'TERMINAL_EXEC_UPSTREAM', value: 'http://127.0.0.1:7682' }
            { name: 'EXEC_TOKEN', secretRef: 'exec-token' }
            // BLAST capacity gate (issue #23) — must match the api sidecar.
            // Default OFF preserves the existing submit-lock path.
            { name: 'BLAST_GATE_ENABLED', value: 'false' }
            // Blue/green self-upgrade flag — must match the api sidecar so the
            // worker-run pipeline/rollback tasks branch identically. Default
            // OFF (Charter §12a Rule 4).
            { name: 'STRICT_BLUEGREEN', value: 'false' }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
        }
        // -------------------------------------------------------------------
        // 4. beat sidecar  (Celery beat singleton, same image as api)
        // schedule + pidfile pinned to /tmp because the api image runs as
        // non-root uid 10001 with a root-owned WORKDIR=/app, so beat cannot
        // write the default celerybeat-schedule DB or pidfile in cwd.
        // -------------------------------------------------------------------
        {
          name: 'beat'
          image: apiImage
          command: [ 'python3', '/app/api/wait_redis.py' ]
          args: [
            'celery'
            '-A'
            'api.celery_app:celery_app'
            'beat'
            '--loglevel=info'
            '--schedule=/tmp/celerybeat-schedule'
            '--pidfile=/tmp/celerybeat.pid'
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'SIDECAR_NAME', value: 'beat' }
            { name: 'OPS_REDIS_URL', value: 'redis://127.0.0.1:6379/2' }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: applicationInsightsConnectionString }
            { name: 'CELERY_BROKER_URL', value: 'redis://127.0.0.1:6379/0' }
            { name: 'CELERY_RESULT_BACKEND', value: 'redis://127.0.0.1:6379/1' }
            // BLAST capacity gate (issue #23) — must match the api sidecar.
            // Default OFF preserves the existing submit-lock path.
            { name: 'BLAST_GATE_ENABLED', value: 'false' }
            // Blue/green self-upgrade flag — must match the api sidecar so the
            // beat-driven reconciler drives validating→confirming→succeeded
            // identically. Default OFF (Charter §12a Rule 4).
            { name: 'STRICT_BLUEGREEN', value: 'false' }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
        }
        // -------------------------------------------------------------------
        // 5. redis sidecar  (broker, ephemeral — queue is rebuilt from
        // Storage state by the beat reconciler if the revision restarts)
        // Image is pulled from the workload ACR mirror to avoid Docker Hub
        // unauthenticated pull rate limits (HTTP 429 ImagePullBackOff would
        // otherwise hold the whole replica in NotRunning state). The mirror
        // is seeded by postprovision.sh / quick-deploy.sh via `az acr import`.
        // -------------------------------------------------------------------
        {
          name: 'redis'
          image: '${acrLoginServer}/library/redis:7-alpine'
          command: [ 'redis-server' ]
          args: [
            '--save', ''
            '--appendonly', 'no'
            '--maxmemory', '384mb'
            '--maxmemory-policy', 'allkeys-lru'
            '--bind', '127.0.0.1'
            '--protected-mode', 'no'
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
        }
        // -------------------------------------------------------------------
        // 6. terminal sidecar  (ttyd + bash + elastic-blast toolchain)
        // /home/azureuser is ephemeral. The terminal re-authenticates with
        // the MI on each browser session; user files stage to workload
        // Storage via azcopy rather than to a local mount.
        // -------------------------------------------------------------------
        {
          name: 'terminal'
          image: terminalImage
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            { name: 'SIDECAR_NAME', value: 'terminal' }
            { name: 'OPS_REDIS_URL', value: 'redis://127.0.0.1:6379/2' }
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'AZURE_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'SHARED_IDENTITY_PRINCIPAL_ID', value: sharedIdentityPrincipalId }
            { name: 'AZCOPY_AUTO_LOGIN_TYPE', value: 'MSI' }
            { name: 'AZCOPY_MSI_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'ELB_SKIP_DB_VERIFY', value: 'true' }
            { name: 'ELB_DISABLE_AUTO_SHUTDOWN', value: '1' }
            // Non-secret platform coordinates surfaced to the interactive
            // shell so the `elb-cfg` helper and the scaffolded
            // `elastic-blast.ini` template can pre-fill region / resource
            // group / storage account / ACR without the researcher having to
            // memorise them. These mirror the api/worker sidecars and are
            // safe to expose (no credentials, only resource names).
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
            { name: 'AZURE_RESOURCE_GROUP', value: platformResourceGroupName }
            { name: 'AZURE_REGION', value: location }
            { name: 'STORAGE_ACCOUNT_NAME', value: platformStorageAccountName }
            { name: 'PLATFORM_ACR_NAME', value: platformAcrName }
            // Programmatic exec channel — see api/services/terminal_exec.py
            // and terminal/exec_server.py. Same secret as the api sidecar.
            { name: 'EXEC_TOKEN', secretRef: 'exec-token' }
            { name: 'EXEC_MAX_CONCURRENCY', value: '4' }
            // 8 MiB body cap so callers can pipe full `kubectl apply -f -`
            // install manifests (cert-manager.yaml alone is ~1.7 MiB).
            // The endpoint is loopback-only + token-authenticated.
            { name: 'EXEC_MAX_BODY_BYTES', value: '8388608' }
          ]
          // Probe the exec server's /healthz (no auth required). Catches the
          // case where the supervisor's `wait -n` did not fire because the
          // python process is still alive but the HTTP server is hung.
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 7682, scheme: 'HTTP' }
              periodSeconds: 30
              timeoutSeconds: 5
              failureThreshold: 3
            }
            {
              type: 'Readiness'
              httpGet: { path: '/healthz', port: 7682, scheme: 'HTTP' }
              periodSeconds: 10
              timeoutSeconds: 3
              failureThreshold: 3
            }
          ]
        }
      ]
      scale: {
        // Intentionally locked at 1/1: the in-revision Redis sidecar holds
        // Celery broker state and Beat is a singleton (see file header).
        // If maxReplicas is ever raised:
        //   1. Move Redis to a managed cache (charter §3 forbids it today)
        //      OR pin Beat to a separate Container App.
        //   2. Add a KEDA rule (cpu @ 70% utilisation, 60s stabilisation),
        //      e.g.:
        //        rules: [
        //          {
        //            name: 'cpu-rule'
        //            custom: {
        //              type: 'cpu'
        //              metadata: { type: 'Utilization', value: '70' }
        //            }
        //          }
        //        ]
        //   3. Verify the warmup beat schedule does not double-fire (Beat
        //      must remain singleton across replicas).
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output controlAppFqdn string = controlApp.properties.configuration.ingress.fqdn
output controlAppName string = controlApp.name
output controlAppResourceId string = controlApp.id
