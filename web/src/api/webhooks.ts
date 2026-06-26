/**
 * webhooks — typed client for `/api/settings/webhooks` (outbound notifications).
 *
 * The stored URL is a secret; the API only ever returns a masked form
 * (`url_masked`). Saving validates the URL server-side against an SSRF allowlist
 * (Slack / Teams / Discord / Logic Apps).
 */
import { api } from "@/api/client";

export interface WebhookConfigPublic {
  configured: boolean;
  url_masked: string;
  enabled: boolean;
  events: string;
  updated_at: string;
}

export const webhooksApi = {
  get: () => api.get<WebhookConfigPublic>("/api/settings/webhooks"),
  put: (body: { url: string; enabled: boolean; events: string }) =>
    api.put<WebhookConfigPublic>("/api/settings/webhooks", body),
  test: () => api.post<{ delivered: boolean }>("/api/settings/webhooks/test", {}),
};
