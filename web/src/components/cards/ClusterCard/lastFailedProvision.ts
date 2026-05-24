/**
 * lastFailedProvision — minimal localStorage persistence for the last
 * failed AKS provision attempt.
 *
 * Background: when `provision_aks` fails (quota, SKU block, ARM
 * rejection, worker crash), the dashboard shows a structured error
 * card in `ClusterCard`. If the user reloads the browser before
 * dismissing it, the error vanishes — the next session lands on a
 * clean dashboard with no indication that anything went wrong. For a
 * tenant who doesn't open the dashboard often this means the failure
 * is silently lost.
 *
 * This module captures the last failure into `localStorage` so the
 * dashboard can render a small "Last attempt failed" banner on the
 * next load (within a 24 h freshness window). One slot per browser
 * profile is enough — the previous failure is overwritten when a new
 * one lands, and the slot is cleared on dismiss.
 *
 * Note: this is a *FE-only* fallback. The backend `JobStateRepository`
 * keeps a more authoritative record but exposing it would require
 * route + filter work that is out of scope here. localStorage covers
 * the common case (single user, single browser).
 */

const STORAGE_KEY = "elb_last_failed_provision_v1";
const FRESHNESS_WINDOW_MS = 24 * 60 * 60 * 1000;

export interface LastFailedProvision {
  /** Raw Azure / Celery error string — fed back into the
   *  `armErrorClassifier` on next render. */
  raw: string;
  /** Cluster name the user picked (for display in the banner). */
  clusterName: string;
  /** Region the failed attempt targeted — used by the classifier for
   *  a region-scoped portal deep-link. */
  region: string;
  /** Resource group the failed attempt targeted — used by the
   *  classifier for RG-permission errors. */
  resourceGroup: string;
  /** Subscription id — used by the classifier for portal deep-links. */
  subscriptionId: string;
  /** ms since epoch. Used to expire entries older than the freshness
   *  window so a stale failure from last month never reappears. */
  when: number;
}

/** Save the latest failure. Overwrites any previous slot. Errors
 *  during save (storage full, disabled in privacy mode) are silently
 *  ignored — persistence is best-effort. */
export function saveLastFailedProvision(payload: LastFailedProvision): void {
  try {
    window.localStorage.setItem(STORAGE_KEY, JSON.stringify(payload));
  } catch {
    // ignore
  }
}

/** Read the saved failure, returning null when:
 *  - storage is unavailable / disabled
 *  - no entry exists
 *  - the entry is older than the 24 h freshness window
 *  - the entry is malformed (defensive against a stale schema)
 *
 * Entries older than the window are also pruned on read so the slot
 * doesn't sit indefinitely after the freshness window expires. */
export function loadLastFailedProvision(): LastFailedProvision | null {
  try {
    const raw = window.localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<LastFailedProvision>;
    if (
      !parsed ||
      typeof parsed.raw !== "string" ||
      typeof parsed.when !== "number" ||
      typeof parsed.clusterName !== "string" ||
      typeof parsed.region !== "string" ||
      typeof parsed.resourceGroup !== "string" ||
      typeof parsed.subscriptionId !== "string"
    ) {
      clearLastFailedProvision();
      return null;
    }
    if (Date.now() - parsed.when > FRESHNESS_WINDOW_MS) {
      clearLastFailedProvision();
      return null;
    }
    return parsed as LastFailedProvision;
  } catch {
    return null;
  }
}

/** Clear the slot. Called when the user clicks Dismiss on the
 *  "Last attempt failed" banner, or when a new provision succeeds. */
export function clearLastFailedProvision(): void {
  try {
    window.localStorage.removeItem(STORAGE_KEY);
  } catch {
    // ignore
  }
}
