/**
 * redactJobJson — sanitise a JobState payload before it is rendered as raw JSON
 * in the Message Flow job-detail inspector.
 *
 * Charter §12 requires UI output to never echo tokens, subscription IDs, or full
 * SAS URLs. The job-detail endpoint returns the raw `payload` dict (nesting
 * `metadata` and other sub-objects), so redaction recurses through arrays and
 * objects. This is a denylist (the payload shape is open-ended, so an allowlist
 * would drop useful diagnostic fields): the keys below are dropped entirely, and
 * any string value carrying a SAS signature has that signature scrubbed so a
 * download URL stays recognisable without leaking the credential.
 *
 * The submitter is shown by its `owner_upn` alias elsewhere in the modal, so
 * `*_upn` is intentionally NOT redacted.
 */

/** Object keys whose values are dropped entirely from the rendered JSON. */
export const REDACTED_JSON_KEYS: ReadonlySet<string> = new Set([
  "owner_oid",
  "tenant_id",
  "subscription_id",
  "sas",
  "sas_url",
  "sas_token",
  "access_token",
]);

/** Scrub the `sig=` signature out of a SAS-bearing URL/string, leaving the rest
 *  intact so the blob path is still recognisable. Returns the value unchanged
 *  when it carries no SAS signature. */
export function scrubSasValue(value: string): string {
  if (!/[?&]sig=/i.test(value)) return value;
  return value.replace(/([?&]sig=)[^&]+/gi, "$1<redacted>");
}

/** Recursively redact a JobState payload for safe raw-JSON display. */
export function redactJobJson(state: unknown): unknown {
  if (typeof state === "string") return scrubSasValue(state);
  if (Array.isArray(state)) return state.map(redactJobJson);
  if (!state || typeof state !== "object") return state;
  return Object.fromEntries(
    Object.entries(state as Record<string, unknown>)
      .filter(([key]) => !REDACTED_JSON_KEYS.has(key))
      .map(([key, value]) => [key, redactJobJson(value)]),
  );
}
