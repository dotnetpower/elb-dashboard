// Re-export shim — preserves the historical "@/pages/tools/ToolTabs" import path.
// Each tab now lives in its own file under ./tabs/ for SRP.
export {
  CostEstimatorTab,
  PreprocessorTab,
  PrimerDesignTab,
  TaxonomyTab,
  SchedulesTab,
  DbVersionsTab,
  AuditTrailTab,
} from "./tabs";
