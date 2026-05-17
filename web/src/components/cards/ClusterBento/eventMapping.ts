/**
 * Map raw k8s `Event` objects from `/api/monitor/aks/events` into the
 * `EventLine` view model used by the cluster bento Live Activity rail.
 *
 * Two responsibilities live here:
 *
 *  1. Classify each event into one of `ok | info | warn | err`. K8s only
 *     marks events `Normal | Warning`, but several `Normal`-type reasons
 *     are operationally notable (`RemovingNode`, `NodeNotSchedulable`,
 *     scaling activity, drains) and deserve a distinct icon so they don't
 *     hide behind a green check.
 *
 *  2. **Group** near-simultaneous events with the same `(reason, kind)`
 *     into a single line. A pool scaledown emits one `RemovingNode` event
 *     per node — without grouping the rail fills with eleven identical
 *     lines that all read "56s ago". Grouped output collapses them into
 *     `[RemovingNode] node/vmss000000..009 (10) · 56s ago`.
 */

import type { K8sEvent } from "@/api/endpoints";

import type { EventKind } from "./atoms";

export interface EventLineView {
  key: string;
  kind: EventKind;
  message: string;
  time: string;
  /** Number of source events collapsed into this row (>= 1). */
  count: number;
}

const WARN_REASONS = new Set([
  "BackOff",
  "FailedScheduling",
  "Unhealthy",
  "FailedMount",
  "FailedAttachVolume",
  "ProvisioningFailed",
  "Evicted",
  "NodeNotReady",
  "ContainerCannotRun",
  "ImagePullBackOff",
  "ErrImagePull",
  "OOMKilling",
  "NetworkNotReady",
]);

const ERR_REASONS = new Set(["Failed", "FailedCreate", "OOMKilled", "Killing"]);

/**
 * `Normal`-type reasons that the operator still wants to *see* — scaling
 * activity, node lifecycle, drains. These render with a neutral info icon
 * (not green check) so they don't masquerade as health signals.
 */
const INFO_NOTABLE_REASONS = new Set([
  "RemovingNode",
  "NodeNotSchedulable",
  "NodeAllocatableEnforced",
  "ScalingReplicaSet",
  "ScaleDown",
  "ScaleUp",
  "Drain",
  "Cordon",
  "Uncordon",
  "Preempted",
]);

function classifyEvent(ev: K8sEvent): EventKind {
  if (ev.type === "Warning") {
    if (ERR_REASONS.has(ev.reason)) return "err";
    if (WARN_REASONS.has(ev.reason)) return "warn";
    return "warn";
  }
  if (INFO_NOTABLE_REASONS.has(ev.reason)) return "info";
  return "ok";
}

function relativeTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "—";
  const sec = Math.max(0, Math.round((Date.now() - t) / 1000));
  if (sec < 60) return `${sec}s ago`;
  if (sec < 3600) return `${Math.floor(sec / 60)}m ago`;
  if (sec < 86400) return `${Math.floor(sec / 3600)}h ago`;
  return `${Math.floor(sec / 86400)}d ago`;
}

/**
 * Try to extract a short suffix from a long resource name so a
 * `vmss000000..009` summary stays readable. Returns the full name when no
 * obvious common prefix exists.
 */
function nameTail(name: string, prefixLen: number): string {
  if (name.length <= prefixLen) return name;
  return name.slice(prefixLen);
}

function commonPrefix(strings: string[]): string {
  if (strings.length === 0) return "";
  let prefix = strings[0];
  for (let i = 1; i < strings.length; i += 1) {
    while (prefix && !strings[i].startsWith(prefix)) {
      prefix = prefix.slice(0, -1);
    }
    if (!prefix) break;
  }
  return prefix;
}

function summarizeNames(names: string[]): string {
  const unique = Array.from(new Set(names));
  if (unique.length === 0) return "";
  if (unique.length === 1) return unique[0];
  unique.sort();
  const pre = commonPrefix(unique);
  if (pre.length >= 3 && unique.length >= 3) {
    const first = nameTail(unique[0], pre.length);
    const last = nameTail(unique[unique.length - 1], pre.length);
    if (first && last) return `${pre}${first}..${last}`;
  }
  // Fall back to a comma-joined head + ellipsis.
  if (unique.length <= 3) return unique.join(", ");
  return `${unique.slice(0, 2).join(", ")}, +${unique.length - 2}`;
}

/**
 * Format a single grouped row's message. Includes namespace prefix when
 * not in the standard `default`/`kube-system` set so the operator can tell
 * BLAST job churn apart from kubelet noise.
 */
function formatMessage(
  reason: string,
  kind: string,
  names: string[],
  namespace: string,
  sampleMessage: string,
  groupCount: number,
  /** Maximum line width in characters. */
  cap = 140,
): string {
  const reasonTag = reason ? `[${reason}]` : "";
  const kindLow = (kind || "").toLowerCase();
  const summary = summarizeNames(names);
  const obj = kindLow && summary ? `${kindLow}/${summary}` : summary;
  const ns =
    namespace && namespace !== "default" && namespace !== "kube-system"
      ? `ns/${namespace}`
      : "";
  const countTag = groupCount > 1 ? `(${groupCount})` : "";
  const head = [reasonTag, obj, countTag, ns].filter(Boolean).join(" ");
  // For a single event keep the original message; for groups skip it
  // because the names already carry the per-row identity.
  if (groupCount === 1 && sampleMessage) {
    const room = Math.max(20, cap - head.length - 3);
    const msg =
      sampleMessage.length > room
        ? sampleMessage.slice(0, room - 1) + "…"
        : sampleMessage;
    return [head, msg].filter(Boolean).join(" · ");
  }
  return head;
}

interface GroupBucket {
  reason: string;
  kind: EventKind;
  involvedKind: string;
  namespace: string;
  names: string[];
  /** Most-recent timestamp in the bucket — drives ordering + "Tago". */
  lastTs: number;
  sampleMessage: string;
  /** Sum of `Event.count` (k8s aggregates repeated firings on the same Event). */
  totalFires: number;
  /** Number of distinct events folded in. */
  members: number;
}

/**
 * Bucket window in ms. Events of the same `(reason, involvedKind)` whose
 * `last_timestamp` falls inside the same window collapse into one row.
 */
const GROUP_WINDOW_MS = 90 * 1000;

/**
 * Collapse `events` into at most `limit` grouped lines. Input is assumed
 * to be in any order; output is sorted newest-first.
 */
export function groupEvents(events: K8sEvent[], limit = 12): EventLineView[] {
  const buckets = new Map<string, GroupBucket>();
  for (const ev of events) {
    const ts = Date.parse(ev.last_timestamp);
    if (!Number.isFinite(ts)) continue;
    const bucketTs = Math.floor(ts / GROUP_WINDOW_MS);
    const key = `${ev.reason}|${ev.involved_kind}|${bucketTs}`;
    const existing = buckets.get(key);
    if (existing) {
      existing.names.push(ev.involved_name || ev.name);
      existing.totalFires += Math.max(1, ev.count || 1);
      existing.members += 1;
      if (ts > existing.lastTs) {
        existing.lastTs = ts;
        existing.sampleMessage = ev.message || existing.sampleMessage;
      }
    } else {
      buckets.set(key, {
        reason: ev.reason,
        kind: classifyEvent(ev),
        involvedKind: ev.involved_kind,
        namespace: ev.namespace,
        names: [ev.involved_name || ev.name],
        lastTs: ts,
        sampleMessage: ev.message,
        totalFires: Math.max(1, ev.count || 1),
        members: 1,
      });
    }
  }
  const groups = Array.from(buckets.values()).sort((a, b) => b.lastTs - a.lastTs);
  return groups.slice(0, limit).map((g, i) => ({
    key: `${g.reason}|${g.involvedKind}|${g.lastTs}|${i}`,
    kind: g.kind,
    message: formatMessage(
      g.reason,
      g.involvedKind,
      g.names,
      g.namespace,
      g.sampleMessage,
      g.members,
    ),
    time: relativeTime(new Date(g.lastTs).toISOString()),
    count: g.members,
  }));
}

/**
 * Single-event variant kept for back-compat (callers that still want
 * one row per event without grouping).
 */
export function toEventLineView(ev: K8sEvent, index: number): EventLineView {
  return {
    key: `${ev.namespace}/${ev.name}/${index}`,
    kind: classifyEvent(ev),
    message: formatMessage(
      ev.reason,
      ev.involved_kind,
      [ev.involved_name || ev.name],
      ev.namespace,
      ev.message,
      1,
    ),
    time: relativeTime(ev.last_timestamp),
    count: 1,
  };
}
