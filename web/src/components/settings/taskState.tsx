import { useEffect } from "react";

import { formatApiError } from "@/api/client";
import { tasksApi, type TaskStatusResponse } from "@/api/tasks";
import { StatusLine } from "@/components/settings/primitives";

/**
 * Shared Celery task-polling state + status line for the Settings panel.
 *
 * Extracted verbatim from `SettingsPanel.tsx` (issue #24 SRP split). Several
 * sections (Telemetry, AKS Observability, Public HTTPS) drive a background
 * task through the same `{ taskId, status, message, step, totalSteps }` shape,
 * poll it via `usePollTask`, gate buttons with `isRunningTask`, and render
 * progress with `TaskStatusLine`. Keeping these together lets each section
 * import them without re-importing the monolith.
 */

export type TaskState = {
  taskId: string;
  status: TaskStatusResponse["status"];
  message?: string;
  step?: number;
  totalSteps?: number;
};

export function isRunningTask(task: TaskState | null): boolean {
  return Boolean(task && task.status !== "SUCCESS" && task.status !== "FAILURE");
}

export function usePollTask(
  task: TaskState | null,
  setTask: React.Dispatch<React.SetStateAction<TaskState | null>>,
  onUpdate?: (status: TaskStatusResponse) => void,
) {
  useEffect(() => {
    if (!task || task.status === "SUCCESS" || task.status === "FAILURE") return;
    // setInterval does not wait for async callbacks, so a slow `/tasks/status`
    // response (slower than the 4s cadence) would fire overlapping requests and
    // let a stale response clobber a fresh one. Guard with a re-entry flag so at
    // most one poll is in flight at a time.
    let pending = false;
    const id = window.setInterval(async () => {
      if (pending) return;
      pending = true;
      try {
        const status = await tasksApi.status(task.taskId);
        const progress = status.progress as
          | { message?: string; step?: number; total_steps?: number }
          | undefined;
        setTask({
          taskId: task.taskId,
          status: status.status,
          message: progress?.message ?? status.error,
          step: progress?.step,
          totalSteps: progress?.total_steps,
        });
        onUpdate?.(status);
      } catch (err) {
        setTask({ taskId: task.taskId, status: "FAILURE", message: formatApiError(err) });
      } finally {
        pending = false;
      }
    }, 4000);
    return () => window.clearInterval(id);
  }, [onUpdate, setTask, task]);
}

export function TaskStatusLine({ task }: { task: TaskState }) {
  const kind = task.status === "SUCCESS" ? "success" : task.status === "FAILURE" ? "error" : "loading";
  const showProgress =
    task.status !== "SUCCESS" &&
    task.status !== "FAILURE" &&
    typeof task.step === "number" &&
    typeof task.totalSteps === "number" &&
    task.totalSteps > 0;
  return (
    <div>
      <StatusLine kind={kind}>
        Task <code>{task.taskId.slice(0, 8)}...</code> · {task.status}
        {showProgress ? ` · step ${task.step}/${task.totalSteps}` : ""}
        {task.message ? ` — ${task.message}` : ""}
      </StatusLine>
      {showProgress && (
        <div
          role="progressbar"
          aria-valuemin={0}
          aria-valuemax={task.totalSteps ?? 0}
          aria-valuenow={task.step ?? 0}
          aria-label="Task progress"
          style={{
            height: 4,
            borderRadius: 999,
            background: "var(--bg-tertiary)",
            overflow: "hidden",
            border: "1px solid var(--border-weak)",
            marginTop: 6,
          }}
        >
          <div
            style={{
              width: `${Math.min(100, Math.round(((task.step ?? 0) / (task.totalSteps ?? 1)) * 100))}%`,
              height: "100%",
              background: "var(--accent)",
              transition: "width 200ms ease-out",
            }}
          />
        </div>
      )}
    </div>
  );
}
