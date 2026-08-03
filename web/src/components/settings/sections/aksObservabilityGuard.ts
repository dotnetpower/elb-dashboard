/** Pure enable-button guard for AKS Container Insights settings. */
export interface ContainerInsightsEnableGate {
  canRead: boolean;
  appInsightsName: string;
  providerEnableAvailable: boolean | null;
  taskRunning: boolean;
  resolvingWorkspace: boolean;
}

export function isContainerInsightsEnableDisabled(
  gate: ContainerInsightsEnableGate,
): boolean {
  return (
    !gate.canRead ||
    !gate.appInsightsName.trim() ||
    gate.providerEnableAvailable !== true ||
    gate.taskRunning ||
    gate.resolvingWorkspace
  );
}
