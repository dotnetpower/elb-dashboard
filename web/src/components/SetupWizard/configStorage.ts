import type { ResourceConfig } from "./types";
import { STORAGE_KEY } from "./types";

export function loadSavedConfig(): ResourceConfig | null {
  try {
    const raw = localStorage.getItem(STORAGE_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as ResourceConfig;
    if (!parsed.subscriptionId || !parsed.workloadResourceGroup) return null;
    return parsed;
  } catch {
    return null;
  }
}

export function saveConfig(config: ResourceConfig): void {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(config));
}

export function clearConfig(): void {
  localStorage.removeItem(STORAGE_KEY);
}
