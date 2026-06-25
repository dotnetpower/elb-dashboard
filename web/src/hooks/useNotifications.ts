/**
 * useNotifications — polling hook for the header notification bell.
 *
 * Polls `/api/notifications` (terminal-job feed + unread count) and exposes a
 * `markSeen` mutation that advances the server-side seen marker. Polling is
 * gated by `enabled` so an unauthenticated shell never spams 401s.
 */
import { useCallback } from "react";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";

import { notificationsApi, type NotificationsResponse } from "@/api/notifications";

const QUERY_KEY = ["notifications"] as const;
const POLL_INTERVAL_MS = 30_000;

const EMPTY: NotificationsResponse = { items: [], unread_count: 0, last_seen_at: "" };

export function useNotifications(enabled: boolean) {
  const queryClient = useQueryClient();

  const query = useQuery<NotificationsResponse>({
    queryKey: QUERY_KEY,
    queryFn: () => notificationsApi.list(50),
    enabled,
    refetchInterval: enabled ? POLL_INTERVAL_MS : false,
    staleTime: 15_000,
    retry: false,
    refetchOnWindowFocus: true,
  });

  const markSeenMutation = useMutation({
    mutationFn: () => notificationsApi.markSeen(),
    onSuccess: () => {
      // Optimistically clear unread, then refetch the authoritative feed.
      queryClient.setQueryData<NotificationsResponse>(QUERY_KEY, (prev) =>
        prev
          ? {
              ...prev,
              unread_count: 0,
              items: prev.items.map((item) => ({ ...item, unread: false })),
            }
          : prev,
      );
      void queryClient.invalidateQueries({ queryKey: QUERY_KEY });
    },
  });

  const markSeen = useCallback(() => {
    if (markSeenMutation.isPending) return;
    markSeenMutation.mutate();
  }, [markSeenMutation]);

  const data = query.data ?? EMPTY;

  return {
    items: data.items,
    unreadCount: data.unread_count,
    isLoading: query.isLoading,
    isError: query.isError,
    markSeen,
  };
}
