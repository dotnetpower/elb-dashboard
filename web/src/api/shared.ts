export interface OrchestrationStatus<TOutput = unknown> {
  instance_id: string;
  runtime_status: string;
  custom_status: unknown;
  created_time: string;
  last_updated_time: string;
  output: TOutput | null;
}