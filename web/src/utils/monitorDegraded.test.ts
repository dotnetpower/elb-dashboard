/**
 * Unit tests for `monitorDegraded.ts`. The backend reason taxonomy lives in
 * `api/routes/monitor/common.py::_classify_exception`; these tests pin the
 * SPA-side mapping so a backend rename is caught before it ships.
 */

import { describe, expect, it } from "vitest";

import {
  aggregateDiagnostics,
  getDegradedInfo,
  type CardDiagnosticInput,
} from "./monitorDegraded";

describe("getDegradedInfo", () => {
  it("returns non-degraded for null / undefined", () => {
    expect(getDegradedInfo(null).degraded).toBe(false);
    expect(getDegradedInfo(undefined).degraded).toBe(false);
  });

  it("returns non-degraded when the flag is missing", () => {
    expect(getDegradedInfo({ clusters: [] }).degraded).toBe(false);
  });

  it("classifies auth_wrong_tenant as an auth issue", () => {
    const info = getDegradedInfo({
      clusters: [],
      degraded: true,
      degraded_reason: "auth_wrong_tenant",
    });
    expect(info.degraded).toBe(true);
    expect(info.reason).toBe("auth_wrong_tenant");
    expect(info.isAuthIssue).toBe(true);
    expect(info.label).toMatch(/tenant/i);
  });

  it("classifies forbidden as an auth issue with a recognisable label", () => {
    const info = getDegradedInfo({ degraded: true, degraded_reason: "forbidden" });
    expect(info.isAuthIssue).toBe(true);
    expect(info.label).toBe("No access");
  });

  it("classifies not_found as a non-auth degrade", () => {
    const info = getDegradedInfo({ degraded: true, degraded_reason: "not_found" });
    expect(info.isAuthIssue).toBe(false);
    expect(info.label).toBe("Not found");
  });

  it("falls back gracefully for unknown reason codes", () => {
    const info = getDegradedInfo({
      degraded: true,
      degraded_reason: "http_500",
    });
    expect(info.degraded).toBe(true);
    expect(info.reason).toBe("http_500");
    expect(info.label).toBe("Degraded");
  });

  it("defaults to azure_error when reason field is missing", () => {
    const info = getDegradedInfo({ degraded: true });
    expect(info.reason).toBe("azure_error");
  });
});

function input(card: string, reason: string | null): CardDiagnosticInput {
  return {
    card,
    info: reason
      ? getDegradedInfo({ degraded: true, degraded_reason: reason })
      : getDegradedInfo(null),
  };
}

describe("aggregateDiagnostics", () => {
  it("does not show the banner when nothing is degraded", () => {
    const agg = aggregateDiagnostics([
      input("aks", null),
      input("storage", null),
      input("acr", null),
    ]);
    expect(agg.show).toBe(false);
    expect(agg.primaryReason).toBeNull();
  });

  it("does not show the banner for a single forbidden card on a leaf resource", () => {
    const agg = aggregateDiagnostics([
      input("aks", null),
      input("storage", null),
      input("acr", "forbidden"),
    ]);
    expect(agg.show).toBe(false);
  });

  it("shows the banner immediately for auth_wrong_tenant on any card", () => {
    const agg = aggregateDiagnostics([
      input("aks", "auth_wrong_tenant"),
      input("storage", null),
      input("acr", null),
    ]);
    expect(agg.show).toBe(true);
    expect(agg.primaryReason).toBe("auth_wrong_tenant");
    expect(agg.title).toMatch(/tenant/i);
  });

  it("shows the banner when two or more cards report auth issues", () => {
    const agg = aggregateDiagnostics([
      input("aks", "forbidden"),
      input("storage", "forbidden"),
      input("acr", null),
    ]);
    expect(agg.show).toBe(true);
    expect(agg.authIssueCount).toBe(2);
  });

  it("shows the banner when two or more cards report not_found", () => {
    const agg = aggregateDiagnostics([
      input("aks", "not_found"),
      input("storage", "not_found"),
      input("acr", null),
    ]);
    expect(agg.show).toBe(true);
    expect(agg.notFoundCount).toBe(2);
    expect(agg.title).toMatch(/not found/i);
  });

  it("orders reasons by severity (wrong_tenant > forbidden > not_found)", () => {
    const agg = aggregateDiagnostics([
      input("aks", "not_found"),
      input("storage", "forbidden"),
      input("acr", "auth_wrong_tenant"),
    ]);
    expect(agg.reasons[0]).toBe("auth_wrong_tenant");
    expect(agg.reasons[agg.reasons.length - 1]).toBe("not_found");
  });

  it("places invisible_subscription above wrong_tenant in severity", () => {
    const agg = aggregateDiagnostics([
      input("aks", "auth_wrong_tenant"),
      input("subscription", "invisible_subscription"),
    ]);
    expect(agg.reasons[0]).toBe("invisible_subscription");
    expect(agg.show).toBe(true);
    expect(agg.title).toMatch(/subscription is not visible/i);
  });

  it("shows the banner for invisible_subscription alone", () => {
    const agg = aggregateDiagnostics([
      input("subscription", "invisible_subscription"),
      input("aks", null),
      input("storage", null),
      input("acr", null),
    ]);
    expect(agg.show).toBe(true);
    expect(agg.primaryReason).toBe("invisible_subscription");
  });

  it("shows the banner for subscriptions_unavailable alone", () => {
    const agg = aggregateDiagnostics([
      input("subscription", "subscriptions_unavailable"),
      input("aks", null),
      input("storage", null),
      input("acr", null),
    ]);
    expect(agg.show).toBe(true);
    expect(agg.primaryReason).toBe("subscriptions_unavailable");
    expect(agg.title).toMatch(/sign in to azure/i);
  });

  it("orders subscriptions_unavailable above every other reason", () => {
    const agg = aggregateDiagnostics([
      input("subscription", "subscriptions_unavailable"),
      input("aks", "auth_wrong_tenant"),
      input("storage", "forbidden"),
      input("acr", "not_found"),
    ]);
    expect(agg.reasons[0]).toBe("subscriptions_unavailable");
  });
});

import { describe as describe2, it as it2, expect as expect2 } from "vitest";

describe2("cluster_stopped / cluster_not_found descriptors (Pillar B)", () => {
  it2("classifies cluster_stopped with a non-auth label and actionable description", () => {
    const info = getDegradedInfo({ degraded: true, degraded_reason: "cluster_stopped" });
    expect2(info.degraded).toBe(true);
    expect2(info.reason).toBe("cluster_stopped");
    expect2(info.label.toLowerCase()).toContain("stopped");
    expect2(info.isAuthIssue).toBe(false);
    expect2(info.description.toLowerCase()).toMatch(/start/);
  });

  it2("classifies cluster_not_found with a non-auth label and actionable description", () => {
    const info = getDegradedInfo({ degraded: true, degraded_reason: "cluster_not_found" });
    expect2(info.reason).toBe("cluster_not_found");
    expect2(info.label.toLowerCase()).toContain("missing");
    expect2(info.isAuthIssue).toBe(false);
    expect2(info.description.toLowerCase()).toMatch(/deleted|moved/);
  });
});