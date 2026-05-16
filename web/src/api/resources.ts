import { api } from "@/api/client";

export interface EnsureRgRequest {
  subscription_id: string;
  resource_group: string;
  region: string;
}

export interface EnsureStorageRequest {
  subscription_id: string;
  resource_group: string;
  account_name: string;
  region: string;
}

export interface EnsureAcrRequest {
  subscription_id: string;
  resource_group: string;
  registry_name: string;
  region: string;
}

export const resourceApi = {
  ensureRg: (req: EnsureRgRequest) =>
    api.post<{ resource_group: string; status: string }>("/resources/ensure-rg", req),

  ensureStorage: (req: EnsureStorageRequest) =>
    api.post<{ account_name: string; status: string }>("/resources/ensure-storage", req),

  ensureAcr: (req: EnsureAcrRequest) =>
    api.post<{ registry_name: string; status: string }>("/resources/ensure-acr", req),
};