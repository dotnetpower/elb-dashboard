/**
 * Tests for armErrorClassifier — locks in the regex contracts so a
 * regression that changes one of the patterns can't quietly degrade
 * the modal's error card to "unknown".
 */
import { describe, expect, it } from "vitest";

import { classifyArmError } from "./armErrorClassifier";

describe("classifyArmError", () => {
  it("classifies InsufficientVCPUQuota with parsed numbers", () => {
    const raw =
      "Provisioning task failed: (BadRequest) ErrCode_InsufficientVCPUQuota " +
      "Insufficient regional vcpu quota left for location koreacentral. " +
      "left regional vcpu quota 100, requested quota 162. If you want to ...";
    const res = classifyArmError(raw, {
      subscriptionId: "sub-1",
      region: "koreacentral",
    });
    expect(res.category).toBe("quota");
    expect(res.summary).toContain("162 vCPUs");
    expect(res.summary).toContain("100 free");
    expect(res.summary).toContain("koreacentral");
    // Action #1 is the portal quota deep-link scoped to sub+region.
    // We use aka.ms/quotas/view-quotas as the durable shortlink so the
    // URL keeps working even if Azure renames the QuotaMenuBlade.
    const portal = res.actions.find((a) => a.kind === "portal");
    expect(portal?.href).toContain("subscriptionId=sub-1");
    expect(portal?.href).toContain("location=koreacentral");
    expect(portal?.href).toMatch(/^https:\/\/(aka\.ms|portal\.azure\.com)/);
  });

  it("classifies blocked SKU and parses SKU names", () => {
    const raw =
      "(BadRequest) The VM size of Standard_E16s_v5,Standard_D2s_v3 is not " +
      "allowed in your subscription in location 'koreacentral'. For more ...";
    const res = classifyArmError(raw, { region: "koreacentral" });
    expect(res.category).toBe("sku_blocked");
    expect(res.summary).toContain("Standard_E16s_v5");
    expect(res.summary).toContain("Standard_D2s_v3");
    expect(res.summary).toContain("koreacentral");
  });

  it("classifies RG authorization failure", () => {
    const raw =
      "AuthorizationFailed: The client 'mi' does not have authorization to " +
      "perform action 'Microsoft.Resources/subscriptions/resourceGroups/read' " +
      "on scope ...";
    const res = classifyArmError(raw, {
      subscriptionId: "sub-1",
      resourceGroup: "rg-elb-cluster",
    });
    expect(res.category).toBe("rg_permission");
    expect(res.summary).toContain("rg-elb-cluster");
    const portal = res.actions.find((a) => a.kind === "portal");
    expect(portal?.href).toContain("rg-elb-cluster");
  });

  it("emits a concrete az role assignment command when the raw error carries oid + scope", () => {
    // Verbatim shape Azure returns on the fresh-subscription create-cluster
    // path: `with object id '<oid>'` + `over scope '/subscriptions/<sub>/
    // resourcegroups/<rg>'`. The classifier must surface a `command`
    // action that the SPA renders as a clipboard-copy button instead of
    // forwarding to a generic docs link.
    const raw =
      "Provisioning task failed: HttpResponseError(\"(AuthorizationFailed) " +
      "The client '9b96face-65e5-406c-bb27-15e506fea865' with object id " +
      "'17c635ef-a30f-4942-9da7-60c8219b4d69' does not have authorization to " +
      "perform action 'Microsoft.Resources/subscriptions/resourcegroups/write' " +
      "over scope '/subscriptions/00000000-0000-0000-0000-000000000000/" +
      "resourcegroups/rg-elb-cluster' or the scope is invalid.\")";
    const res = classifyArmError(raw);
    expect(res.category).toBe("rg_permission");
    // RG parsed from scope wins over the (absent) context.resourceGroup.
    expect(res.summary).toContain("rg-elb-cluster");
    const command = res.actions.find((a) => a.kind === "command");
    expect(command).toBeDefined();
    expect(command?.label).toMatch(/copy/i);
    expect(command?.href).toContain("17c635ef-a30f-4942-9da7-60c8219b4d69");
    expect(command?.href).toContain("00000000-0000-0000-0000-000000000000");
    expect(command?.href).toContain("rg-elb-cluster");
    expect(command?.href).toContain("--role Contributor");
    // ServicePrincipal type is non-negotiable: the MI auth call refuses
    // role assignments missing this field on newly-created principals.
    expect(command?.href).toContain("ServicePrincipal");
  });

  it("falls through to unknown but strips the Provisioning task failed wrapper", () => {
    const raw =
      "Provisioning task failed: (Conflict) The cluster name is already " +
      "taken. Code: Conflict Message: The cluster name is already taken.";
    const res = classifyArmError(raw);
    expect(res.category).toBe("unknown");
    // Wrapper + trailing Code/Message duplicate both stripped.
    expect(res.summary.startsWith("Provisioning task failed")).toBe(false);
    expect(res.summary).toContain("(Conflict)");
    expect(res.summary).not.toContain("Code: Conflict");
  });

  it("returns a portal URL even without subscription context", () => {
    const raw =
      "Insufficient regional vcpu quota left for location eastus2. left " +
      "regional vcpu quota 50, requested quota 200.";
    const res = classifyArmError(raw);
    expect(res.category).toBe("quota");
    const portal = res.actions.find((a) => a.kind === "portal");
    // Either the aka.ms shortlink or the canonical portal.azure.com URL
    // is acceptable as the fallback; both forward to the same blade.
    expect(portal?.href).toMatch(/^https:\/\/(aka\.ms|portal\.azure\.com)/);
  });
});
