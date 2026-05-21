import { useState, type ReactNode } from "react";
import { useMutation, useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, CheckCircle2, Loader2, Plus } from "lucide-react";

import { resourceApi } from "@/api/endpoints";
import { Tooltip } from "@/components/Tooltip";

import { ErrorMsg } from "./ErrorMsg";
import type { ResourceConfig } from "./types";
import { REGIONS, RG_RE } from "./validation";

export interface RgFieldProps {
  label: string;
  configKey: "workloadResourceGroup" | "acrResourceGroup";
  placeholder: string;
  config: ResourceConfig;
  setConfig: React.Dispatch<React.SetStateAction<ResourceConfig>>;
  rgData: Array<{ name: string; location: string }> | undefined;
  isManual: boolean;
  error?: string;
  tooltip: ReactNode;
  /** When true, selecting this RG also sets config.region */
  isPrimary?: boolean;
}

export function RgField({
  label,
  configKey,
  placeholder,
  config,
  setConfig,
  rgData,
  isManual,
  error,
  tooltip,
  isPrimary,
}: RgFieldProps) {
  const queryClient = useQueryClient();
  const [creating, setCreating] = useState(false);
  const [newName, setNewName] = useState(placeholder);
  const [newRegion, setNewRegion] = useState(config.region || "koreacentral");

  const createMut = useMutation({
    mutationFn: () =>
      resourceApi.ensureRg({
        subscription_id: config.subscriptionId,
        resource_group: newName,
        region: newRegion,
      }),
    onSuccess: async () => {
      setConfig((c) => {
        const next = { ...c, [configKey]: newName };
        if (isPrimary) next.region = newRegion;
        return next;
      });
      await queryClient.invalidateQueries({ queryKey: ["wizard-rgs"] });
      setCreating(false);
    },
  });

  const nameValid = RG_RE.test(newName) && newName.length > 0;
  const nameDuplicate = Boolean(
    rgData?.some((g) => g.name.toLowerCase() === newName.toLowerCase()),
  );

  return (
    <div style={{ marginBottom: 12 }}>
      <span className="glass-label">
        {label}
        <Tooltip content={tooltip} width={340} />
      </span>

      {creating ? (
        /* ── Create-new mode ── */
        <div style={{ display: "flex", flexDirection: "column", gap: 8 }}>
          <div style={{ display: "flex", gap: 6, alignItems: "flex-start" }}>
            <div style={{ flex: 1 }}>
              <input
                className="glass-input"
                placeholder={placeholder}
                value={newName}
                onChange={(e) => setNewName(e.target.value.trim())}
                spellCheck={false}
                autoFocus
              />
              {!nameValid && newName && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                    color: "var(--danger)",
                    fontSize: 11,
                    marginTop: 4,
                  }}
                >
                  <AlertTriangle size={11} /> Letters, numbers, hyphens,
                  underscores, periods only
                </div>
              )}
              {nameValid && nameDuplicate && (
                <div
                  style={{
                    display: "flex",
                    alignItems: "center",
                    gap: 4,
                    color: "var(--warning)",
                    fontSize: 11,
                    marginTop: 4,
                  }}
                >
                  <AlertTriangle size={11} /> Resource group &quot;{newName}&quot;
                  already exists. It will be reused.
                </div>
              )}
              {createMut.isError && (
                <div style={{ fontSize: 11, color: "var(--danger)", marginTop: 4 }}>
                  {(createMut.error as Error).message}
                </div>
              )}
            </div>
            <button
              className="glass-button glass-button--primary"
              style={{
                padding: "7px 14px",
                fontSize: 12,
                whiteSpace: "nowrap",
                marginTop: 0,
              }}
              disabled={!nameValid || createMut.isPending}
              onClick={() => createMut.mutate()}
            >
              {createMut.isPending ? (
                <>
                  <Loader2 size={12} className="spin" /> Creating...
                </>
              ) : createMut.isSuccess ? (
                <>
                  <CheckCircle2 size={12} /> Created
                </>
              ) : (
                <>
                  <Plus size={12} /> Create
                </>
              )}
            </button>
            <button
              className="glass-button"
              style={{ padding: "7px 10px", fontSize: 12 }}
              onClick={() => {
                setCreating(false);
                createMut.reset();
              }}
            >
              Cancel
            </button>
          </div>
          <div>
            <span className="glass-label" style={{ fontSize: 11 }}>
              Region
            </span>
            <select
              className="glass-input"
              value={newRegion}
              onChange={(e) => setNewRegion(e.target.value)}
              style={{ fontSize: 12 }}
            >
              {REGIONS.map((r) => (
                <option key={r} value={r}>
                  {r}
                </option>
              ))}
            </select>
          </div>
        </div>
      ) : isManual ? (
        /* ── Manual input mode (ARM unavailable) ── */
        <div>
          <input
            className="glass-input"
            placeholder={placeholder}
            value={config[configKey]}
            onChange={(e) =>
              setConfig((c) => ({ ...c, [configKey]: e.target.value.trim() }))
            }
            spellCheck={false}
          />
        </div>
      ) : (
        /* ── Select from existing + Create new button ── */
        <div style={{ display: "flex", gap: 6 }}>
          <select
            className="glass-input"
            style={{ flex: 1 }}
            value={config[configKey]}
            onChange={(e) => {
              const selected = rgData?.find((g) => g.name === e.target.value);
              setConfig((c) => {
                const next = { ...c, [configKey]: e.target.value };
                if (isPrimary && selected) next.region = selected.location;
                return next;
              });
            }}
          >
            <option value="">Select...</option>
            {rgData?.map((g) => (
              <option key={g.name} value={g.name}>
                {g.name} · {g.location}
              </option>
            ))}
          </select>
          <button
            className="glass-button"
            style={{ padding: "7px 10px", fontSize: 11, whiteSpace: "nowrap" }}
            onClick={() => {
              setCreating(true);
              setNewName(placeholder);
              setNewRegion(config.region || "koreacentral");
              createMut.reset();
            }}
          >
            <Plus size={12} /> New
          </button>
        </div>
      )}
      <ErrorMsg msg={error} />
    </div>
  );
}
