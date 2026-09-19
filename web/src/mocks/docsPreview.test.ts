import { describe, expect, it } from "vitest";

import { docsPreviewApiResponse } from "./docsPreview";

async function payload(path: string): Promise<Record<string, unknown>> {
  const response = docsPreviewApiResponse(path, "GET");
  expect(response).not.toBeNull();
  return (await response!.json()) as Record<string, unknown>;
}

describe("documentation preview fixtures", () => {
  it("matches the complete caller-permissions contract", async () => {
    const body = await payload("/api/me/permissions");

    expect(body.matched_roles).toEqual(["b24988ac-6180-42a0-ab88-20f7382dd24c"]);
    expect(body.matched_role_names).toEqual(["Contributor"]);
    expect(body.can_submit_blast).toBe(true);
    expect(body.degraded).toBe(false);
  });

  it("uses the current OpenAPI pin and a documentation-only IP", async () => {
    const acr = await payload("/api/monitor/acr");
    const service = await payload("/api/monitor/aks/service-ip");

    expect(acr.expected_image_tags).toMatchObject({ "elb-openapi": "4.61" });
    expect(service.external_ip).toBe("192.0.2.10");
  });
});