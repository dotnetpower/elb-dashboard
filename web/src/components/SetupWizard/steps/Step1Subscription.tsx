import { Loader2 } from "lucide-react";
import type { UseQueryResult } from "@tanstack/react-query";

import { ErrorMsg } from "../ErrorMsg";
import type { ResourceConfig } from "../types";
import type { ValidationErrors } from "../validation";

interface SubscriptionRow {
  subscriptionId: string;
  displayName: string;
}

export function Step1Subscription({
  config,
  setConfig,
  errors,
  subsQuery,
}: {
  config: ResourceConfig;
  setConfig: React.Dispatch<React.SetStateAction<ResourceConfig>>;
  errors: ValidationErrors;
  subsQuery: UseQueryResult<SubscriptionRow[]>;
}) {
  return (
    <div>
      <h2 style={{ fontSize: 14, fontWeight: 600, marginBottom: 4 }}>
        Choose your Azure account
      </h2>
      <p
        style={{
          fontSize: 12,
          color: "var(--text-muted)",
          marginBottom: 14,
          lineHeight: 1.5,
        }}
      >
        A subscription is your Azure billing account. If you're not sure which to
        choose, ask your lab administrator or IT team.
      </p>
      <label style={{ display: "block" }}>
        <span className="glass-label">Subscription</span>
        {subsQuery.isLoading ? (
          <div
            style={{
              display: "flex",
              alignItems: "center",
              gap: 8,
              padding: "10px 0",
              color: "var(--text-muted)",
            }}
          >
            <Loader2 size={14} className="spin" /> Loading subscriptions...
          </div>
        ) : subsQuery.isError ? (
          <>
            <div
              style={{
                color: "var(--warning)",
                fontSize: 12,
                marginBottom: 8,
                lineHeight: 1.5,
              }}
            >
              Could not load subscriptions. Enter your Subscription ID manually.{" "}
              <a
                href="https://portal.azure.com/#view/Microsoft_Azure_Billing/SubscriptionsBlade"
                target="_blank"
                rel="noreferrer"
                style={{ color: "var(--accent)" }}
              >
                Find it in the Azure Portal →
              </a>
            </div>
            <input
              className="glass-input"
              placeholder="12345678-1234-1234-1234-123456789abc"
              value={config.subscriptionId}
              onChange={(e) =>
                setConfig((c) => ({ ...c, subscriptionId: e.target.value.trim() }))
              }
              spellCheck={false}
              style={{ fontFamily: "var(--font-mono)", fontSize: 12 }}
            />
          </>
        ) : (
          <select
            className="glass-input"
            value={config.subscriptionId}
            onChange={(e) =>
              setConfig((c) => ({ ...c, subscriptionId: e.target.value }))
            }
          >
            <option value="">Select a subscription</option>
            {subsQuery.data?.map((s) => (
              <option key={s.subscriptionId} value={s.subscriptionId}>
                {s.displayName} ({s.subscriptionId})
              </option>
            ))}
          </select>
        )}
        <ErrorMsg msg={errors.subscriptionId} />
      </label>
    </div>
  );
}
