/**
 * notifications — typed client for the `/api/notifications` endpoints.
 *
 * Backs the header notification bell. The feed is a derived view over terminal
 * BLAST jobs (completed/failed/cancelled); `unread` is computed server-side from
 * per-user "last seen" and "cleared before" markers. `markSeen` advances the
 * seen marker; `clear` hides the current feed without deleting job history.
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
  error_detail?: string;
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

export interface ClearNotificationsResponse {
  cleared_before_at: string;
  unread_count: number;
}

export const notificationsApi = {
  list: (limit = 50) =>
    api.get<NotificationsResponse>(`/notifications?limit=${encodeURIComponent(limit)}`),
  markSeen: () => api.post<MarkSeenResponse>("/notifications/seen", {}),
  clear: () => api.post<ClearNotificationsResponse>("/notifications/clear", {}),
};
