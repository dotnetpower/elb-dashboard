// Single bundled Container App `ca-elb-control` with all six sidecars.
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

@description('Container App name (e.g. ca-elb-control).')
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

@description('AAD tenant id used to validate MSAL bearer tokens.')
param tenantId string

@description('App Registration client id (audience) for the api.')
param apiClientId string

@description('App Insights connection string for telemetry from inside the containers.')
param applicationInsightsConnectionString string

@description('Platform Storage account name (used to derive the table endpoint for jobstate / jobhistory access).')
param platformStorageAccountName string = ''

@description('Subscription id (passed into the api/worker env vars so monitor routes can default subscription_id when not provided in the query string).')
param subscriptionId string = subscription().subscriptionId

@description('CORS allowed origins for the api ingress. Empty list disables CORS (same-origin only).')
param allowedOrigins array = []

@description('Tags applied to every resource in this module.')
param tags object = {}

var bootstrapImage = 'mcr.microsoft.com/azuredocs/containerapps-helloworld:latest'

var apiImage      = useBootstrapImage ? bootstrapImage : '${acrLoginServer}/elb-api:${apiImageTag}'
var frontendImage = useBootstrapImage ? bootstrapImage : '${acrLoginServer}/elb-frontend:${frontendImageTag}'
var terminalImage = useBootstrapImage ? bootstrapImage : '${acrLoginServer}/elb-terminal:${terminalImageTag}'

var tableEndpoint = empty(platformStorageAccountName) ? '' : 'https://${platformStorageAccountName}.table.core.windows.net'
var blobEndpoint = empty(platformStorageAccountName) ? '' : 'https://${platformStorageAccountName}.blob.core.windows.net'

resource controlApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: appName
  location: location
  tags: tags
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
      activeRevisionsMode: 'Single'
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
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'API_CLIENT_ID', value: apiClientId }
            { name: 'AZURE_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
            { name: 'AZURE_TABLE_ENDPOINT', value: tableEndpoint }
            { name: 'AZURE_BLOB_ENDPOINT', value: blobEndpoint }
            { name: 'APPLICATIONINSIGHTS_CONNECTION_STRING', value: applicationInsightsConnectionString }
            { name: 'CELERY_BROKER_URL', value: 'redis://127.0.0.1:6379/0' }
            { name: 'CELERY_RESULT_BACKEND', value: 'redis://127.0.0.1:6379/1' }
            { name: 'FRONTEND_UPSTREAM', value: 'http://127.0.0.1:8081' }
            { name: 'TERMINAL_UPSTREAM', value: 'http://127.0.0.1:7681' }
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
        }
        // -------------------------------------------------------------------
        // 3. worker sidecar  (Celery worker, same image as api)
        // -------------------------------------------------------------------
        {
          name: 'worker'
          image: apiImage
          command: [ '/bin/sh', '-c' ]
          args: [
            'celery -A api_app.celery_app:celery_app worker --loglevel=info -Q default,azure,blast,storage --concurrency=2'
          ]
          resources: {
            cpu: json('0.5')
            memory: '1.0Gi'
          }
          env: [
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'API_CLIENT_ID', value: apiClientId }
            { name: 'AZURE_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'AZURE_SUBSCRIPTION_ID', value: subscriptionId }
            { name: 'AZURE_TABLE_ENDPOINT', value: tableEndpoint }
            { name: 'AZURE_BLOB_ENDPOINT', value: blobEndpoint }
            { name: 'CELERY_BROKER_URL', value: 'redis://127.0.0.1:6379/0' }
            { name: 'CELERY_RESULT_BACKEND', value: 'redis://127.0.0.1:6379/1' }
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
          command: [ '/bin/sh', '-c' ]
          args: [
            'celery -A api_app.celery_app:celery_app beat --loglevel=info --schedule=/tmp/celerybeat-schedule --pidfile=/tmp/celerybeat.pid'
          ]
          resources: {
            cpu: json('0.25')
            memory: '0.5Gi'
          }
          env: [
            { name: 'CELERY_BROKER_URL', value: 'redis://127.0.0.1:6379/0' }
            { name: 'CELERY_RESULT_BACKEND', value: 'redis://127.0.0.1:6379/1' }
            { name: 'LOG_LEVEL', value: 'INFO' }
          ]
        }
        // -------------------------------------------------------------------
        // 5. redis sidecar  (broker, ephemeral — queue is rebuilt from
        // Storage state by the beat reconciler if the revision restarts)
        // -------------------------------------------------------------------
        {
          name: 'redis'
          image: 'redis:7-alpine'
          command: [ 'redis-server' ]
          args: [
            '--save', ''
            '--appendonly', 'no'
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
            { name: 'AZURE_TENANT_ID', value: tenantId }
            { name: 'AZURE_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'AZCOPY_AUTO_LOGIN_TYPE', value: 'MSI' }
            { name: 'AZCOPY_MSI_CLIENT_ID', value: sharedIdentityClientId }
            { name: 'ELB_SKIP_DB_VERIFY', value: 'true' }
            { name: 'ELB_DISABLE_AUTO_SHUTDOWN', value: '1' }
          ]
        }
      ]
      scale: {
        minReplicas: 1
        maxReplicas: 1
      }
    }
  }
}

output controlAppFqdn string = controlApp.properties.configuration.ingress.fqdn
output controlAppName string = controlApp.name
output controlAppResourceId string = controlApp.id
