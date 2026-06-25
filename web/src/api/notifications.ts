/**
 * notifications — typed client for the `/api/notifications` endpoints.
 *
 * Backs the header notification bell. The feed is a derived view over terminal
 * BLAST jobs (completed/failed/cancelled); `unread` is computed server-side from
 * a per-user "last seen" marker. `markSeen` advances that marker so the unread
 * badge clears.
 */
import { api } from "@/api/client";

export type NotificationStatus = "completed" | "failed" | "cancelled" | string;

export interface NotificationItem {
  job_id: string;
  status: NotificationStatus;
  title: string;
  program: string;
  db: string;
  updated_at: string;
  error_code: string;
  unread: boolean;
}

export interface NotificationsResponse {
  items: NotificationItem[];
  unread_count: number;
  last_seen_at: string;
}

export interface MarkSeenResponse {
  last_seen_at: string;
  unread_count: number;
}

export const notificationsApi = {
  list: (limit = 50) =>
    api.get<NotificationsResponse>(`/api/notifications?limit=${encodeURIComponent(limit)}`),
  markSeen: () => api.post<MarkSeenResponse>("/api/notifications/seen", {}),
};
