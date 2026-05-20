// Platform-state Storage layout for the bundled Container App.
//
// On the existing platform Storage account this module adds:
//   * `job-state`        table      — single-row-per-job state (PartitionKey=job_id, RowKey="current")
//   * `job-history`      table      — per-step transitions (PartitionKey=job_id, RowKey=ulid)
//   * `autowarmup`       table      — Auto warm preferences, kept out of jobstate
//   * `audit`            container  — append blobs, daily-rolled JSON Lines
//   * `dead-letter`      container  — one blob per Celery task that exhausted retries
//   * `job-payloads`     container  — sanitised request/result payloads, append blobs
//   * `schedules`        container  — single JSON blob, ETag-versioned
//   * `blast-db`         container  — ElasticBLAST database files
//   * `queries`          container  — user query FASTA uploads
//   * `results`          container  — ElasticBLAST outputs streamed through the API
//   * `redis-data`       file share — AOF persistence for the redis sidecar
//   * `terminal-home`    file share — /home/azureuser persistence for the terminal sidecar
//
// The Storage account itself is created elsewhere (existing platform module);
// this module only adds the children, so it can be wired in without touching
// the existing account.

@description('Name of the existing platform Storage account.')
param storageAccountName string

resource storage 'Microsoft.Storage/storageAccounts@2023-05-01' existing = {
  name: storageAccountName
}

// ---------------------------------------------------------------------------
// Tables
// ---------------------------------------------------------------------------
resource tableService 'Microsoft.Storage/storageAccounts/tableServices@2023-05-01' = {
  parent: storage
  name: 'default'
}

resource jobStateTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'jobstate'
}

resource jobHistoryTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'jobhistory'
}

resource autoWarmupTable 'Microsoft.Storage/storageAccounts/tableServices/tables@2023-05-01' = {
  parent: tableService
  name: 'autowarmup'
}

// ---------------------------------------------------------------------------
// Blob containers
// ---------------------------------------------------------------------------
resource blobService 'Microsoft.Storage/storageAccounts/blobServices@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    deleteRetentionPolicy: { enabled: true, days: 7 }
    containerDeleteRetentionPolicy: { enabled: true, days: 7 }
  }
}

var stateContainers = [
  'audit'
  'dead-letter'
  'job-payloads'
  'schedules'
  'blast-db'
  'queries'
  'results'
]

resource containers 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for name in stateContainers: {
  parent: blobService
  name: name
  properties: {
    publicAccess: 'None'
  }
}]

// ---------------------------------------------------------------------------
// File shares were intentionally removed from this module.
//
// Earlier revisions mounted Azure Files (SMB) shares for the redis sidecar's
// AOF persistence and the terminal sidecar's /home/azureuser. SMB mounts in
// Container Apps require an account key, which conflicts with the
// `allowSharedKeyAccess: false` invariant on the platform Storage account.
// Switching to NFS Files would require Premium FileStorage SKU (~$32/month
// minimum) just for two "nice to have" mounts.
//
// The control plane is designed to tolerate ephemeral sidecar state:
//   * redis: queue is rebuilt from `jobstate` table (in-flight tasks observed
//     as `running` are re-dispatched by the beat reconciler).
//   * terminal: az login uses the MI on each session; user query/result files
//     stage directly to workload Storage via azcopy.
// So both file shares are dropped entirely.

// ---------------------------------------------------------------------------
// Lifecycle: cool tier dead-letter / job-payloads (block blobs) after 30 days,
// delete after 365 days. Note: append blobs (audit) do not support
// tierToCool/delete actions, so audit retention is managed by an external
// reconciler instead of a lifecycle rule.
// ---------------------------------------------------------------------------
resource lifecycle 'Microsoft.Storage/storageAccounts/managementPolicies@2023-05-01' = {
  parent: storage
  name: 'default'
  properties: {
    policy: {
      rules: [
        {
          name: 'block-blob-tiering'
          enabled: true
          type: 'Lifecycle'
          definition: {
            filters: {
              blobTypes: [ 'blockBlob' ]
              prefixMatch: [ 'dead-letter/', 'job-payloads/' ]
            }
            actions: {
              baseBlob: {
                tierToCool: { daysAfterModificationGreaterThan: 30 }
                delete: { daysAfterModificationGreaterThan: 365 }
              }
            }
          }
        }
      ]
    }
  }
}

output jobStateTableName string = jobStateTable.name
output jobHistoryTableName string = jobHistoryTable.name
output autoWarmupTableName string = autoWarmupTable.name
