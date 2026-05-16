export interface SpecParam {
  name: string;
  in: string;
  required?: boolean;
  description?: string;
  schema?: { type?: string; default?: unknown };
}

export interface SpecEndpoint {
  method: string;
  path: string;
  summary?: string;
  description?: string;
  tags: string[];
  parameters: SpecParam[];
  requestBody?: {
    required?: boolean;
    content?: Record<
      string,
      {
        schema?: Record<string, unknown>;
        examples?: Record<
          string,
          { summary?: string; description?: string; value: unknown }
        >;
      }
    >;
  };
  responses?: Record<string, { description?: string }>;
}

export interface ParsedSpec {
  title: string;
  version: string;
  description: string;
  tags: { name: string; description?: string }[];
  endpoints: SpecEndpoint[];
  baseUrl: string;
}

export interface OpenApiProxyInfo {
  sub: string;
  rg: string;
  clusterName: string;
}