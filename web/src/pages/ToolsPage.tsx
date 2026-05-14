import { useState } from "react";
import { Database, Wrench } from "lucide-react";

import { loadSavedConfig } from "@/components/SetupWizard";
import {
  AuditTrailTab,
  CostEstimatorTab,
  DbVersionsTab,
  PreprocessorTab,
  PrimerDesignTab,
  SchedulesTab,
  TaxonomyTab,
} from "@/pages/tools/ToolTabs";
import {
  TAB_GROUPS,
  TAB_INDEX,
  type TabKey,
} from "@/pages/tools/toolsPageModel";

export function ToolsPage() {
  const [activeTab, setActiveTab] = useState<TabKey>("cost");
  const cfg = loadSavedConfig();
  const hasConfig = !!cfg?.subscriptionId;
  const activeMeta = TAB_INDEX[activeTab];

  return (
    <div className="page-stack">
      <header
        className="page-header"
        style={{
          display: "flex",
          alignItems: "flex-start",
          justifyContent: "space-between",
          gap: 16,
          flexWrap: "wrap",
          marginBottom: 0,
        }}
      >
        <div>
          <div
            className="page-header__title"
            style={{ display: "flex", alignItems: "center", gap: 10 }}
          >
            <Wrench size={22} strokeWidth={1.5} style={{ color: "var(--accent)" }} />
            Lab Tools
          </div>
          <div className="page-header__desc">
            Pre-flight estimators, sequence utilities, and operations consoles for
            ElasticBLAST on Azure.
          </div>
        </div>
        <div
          style={{
            fontSize: 11,
            color: "var(--text-muted)",
            display: "flex",
            alignItems: "center",
            gap: 6,
            padding: "4px 10px",
            background: "var(--bg-secondary)",
            border: "1px solid var(--border-weak)",
            borderRadius: 20,
          }}
          title="Active workspace context shared across tools"
        >
          <Database size={12} />
          {hasConfig ? (
            <>
              <code className="code-val" style={{ fontSize: 11 }}>
                {cfg?.storageAccountName || "—"}
              </code>
              <span>·</span>
              <span>{cfg?.region}</span>
            </>
          ) : (
            <span>No workspace selected</span>
          )}
        </div>
      </header>

      <nav
        aria-label="Lab tool categories"
        style={{ display: "flex", flexDirection: "column", gap: 8 }}
      >
        {TAB_GROUPS.map((group) => (
          <div
            key={group.label}
            style={{ display: "flex", alignItems: "center", gap: 10 }}
          >
            <span
              style={{
                fontSize: 10,
                textTransform: "uppercase",
                letterSpacing: "0.08em",
                color: "var(--text-faint)",
                minWidth: 72,
              }}
            >
              {group.label}
            </span>
            <div className="blast-program-tabs" style={{ flex: 1, flexWrap: "wrap" }}>
              {group.tabs.map((tab) => {
                const isActive = activeTab === tab.key;
                return (
                  <button
                    key={tab.key}
                    type="button"
                    onClick={() => setActiveTab(tab.key)}
                    className={`blast-program-tab${isActive ? " blast-program-tab--active" : ""}`}
                    style={{ minWidth: 150, textAlign: "left" }}
                    aria-pressed={isActive}
                    title={tab.desc}
                  >
                    <span
                      className="blast-program-tab__name"
                      style={{
                        display: "inline-flex",
                        alignItems: "center",
                        gap: 6,
                        fontFamily: "inherit",
                      }}
                    >
                      {tab.icon} {tab.label}
                    </span>
                    <span
                      className="blast-program-tab__desc"
                      style={{ whiteSpace: "normal" }}
                    >
                      {tab.desc}
                    </span>
                  </button>
                );
              })}
            </div>
          </div>
        ))}
      </nav>

      {activeTab === "cost" && <CostEstimatorTab meta={activeMeta} />}
      {activeTab === "preprocess" && <PreprocessorTab meta={activeMeta} />}
      {activeTab === "primer" && (
        <PrimerDesignTab meta={activeMeta} hasConfig={hasConfig} />
      )}
      {activeTab === "taxonomy" && <TaxonomyTab meta={activeMeta} />}
      {activeTab === "schedules" && <SchedulesTab meta={activeMeta} />}
      {activeTab === "versions" && (
        <DbVersionsTab meta={activeMeta} hasConfig={hasConfig} />
      )}
      {activeTab === "audit" && <AuditTrailTab meta={activeMeta} />}
    </div>
  );
}