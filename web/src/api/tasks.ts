/**
 * tasks — typed client for the Celery task status endpoint.
 *
 * Used by the cluster provision flow (and other long-running task triggers)
 * to detect failures the cluster-list poller can't see — e.g. the worker
 * couldn't reach Azure ARM at all, or the task raised an exception before
 * the ARM PUT was issued. Without this poll the UI would show a forever
 * "Provisioning..." spinner whenever the backend silently failed.
 */
import { api } from "@/api/client";

export type CeleryTaskStatus =
  | "PENDING"
  | "STARTED"
  | "SUCCESS"
  | "FAILURE"
  | "RETRY"
  | "REVOKED";

export interface TaskStatusResponse {
  task_id: string;
  status: CeleryTaskStatus;
  ready: boolean;
  /** Result payload when status === "SUCCESS". */
  result?: unknown;
  /** Stringified exception when status === "FAILURE". */
  error?: string;
  /** Free-form progress dict published by the task via update_state(meta=…). */
  progress?: Record<string, unknown>;
}

export const tasksApi = {
  /** GET /api/tasks/{task_id} — current Celery state for a task. */
  status: (taskId: string) =>
    api.get<TaskStatusResponse>(`/tasks/${encodeURIComponent(taskId)}`),
};
