// Public surface of the cluster-diagnostics drawer.
// The only consumer (`ClusterDetailModal`) imports `ClusterModalKubectl`
// from `@/components/ClusterDiagnostics`; this index keeps that path
// working after the file → directory split.
export { ClusterModalKubectl } from "./ClusterModalKubectl";
export type { ClusterModalKubectlProps } from "./ClusterModalKubectl";
