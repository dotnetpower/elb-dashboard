import type { UseMutationResult, UseQueryResult } from "@tanstack/react-query";

import { ResourceRow } from "../ResourceRow";
import type { ResourceConfig } from "../types";
import { ACR_RE, STORAGE_RE, type ValidationErrors } from "../validation";

interface NamedRow {
  name: string;
}

export function Step3Resources({
  config,
  setConfig,
  errors,
  storageQuery,
  acrQuery,
  createStorageMut,
  createAcrMut,
}: {
  config: ResourceConfig;
  setConfig: React.Dispatch<React.SetStateAction<ResourceConfig>>;
  errors: ValidationErrors;
  storageQuery: UseQueryResult<NamedRow[]>;
  acrQuery: UseQueryResult<NamedRow[]>;
  createStorageMut: UseMutationResult<unknown, Error, void>;
  createAcrMut: UseMutationResult<unknown, Error, void>;
}) {
  return (
    <div>
      <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>
        Discover & Create Resources
      </h2>
      <p
        style={{
          fontSize: 12,
          color: "var(--text-muted)",
          marginBottom: 14,
          lineHeight: 1.5,
        }}
      >
        We scan for existing resources. Missing ones can be created here.
      </p>

      <ResourceRow
        label={`Storage Account (${config.workloadResourceGroup})`}
        icon="🗄"
        placeholder="e.g. elbstorage01 (3-24 lowercase + numbers)"
        value={config.storageAccountName}
        onChange={(v) => setConfig((c) => ({ ...c, storageAccountName: v }))}
        query={storageQuery}
        nameKey="name"
        isValid={
          !config.storageAccountName || STORAGE_RE.test(config.storageAccountName)
        }
        mutation={createStorageMut}
        error={errors.storageAccountName}
      />

      <ResourceRow
        label={`Container Registry (${config.acrResourceGroup})`}
        icon="📦"
        placeholder="e.g. elbacr (5-50 alphanumeric)"
        value={config.acrName}
        onChange={(v) => setConfig((c) => ({ ...c, acrName: v }))}
        query={acrQuery}
        nameKey="name"
        isValid={!config.acrName || ACR_RE.test(config.acrName)}
        mutation={createAcrMut}
        error={errors.acrName}
      />

      <div
        style={{
          marginTop: 16,
          padding: "12px 14px",
          background: "rgba(110,159,255,0.06)",
          border: "1px solid rgba(110,159,255,0.15)",
          borderRadius: "var(--radius)",
          fontSize: 12,
          color: "var(--text-muted)",
          lineHeight: 1.5,
        }}
      >
        Config is saved in your browser. Change it anytime via the ⚙ icon.
      </div>
    </div>
  );
}
