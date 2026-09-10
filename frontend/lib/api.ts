/** Typed client for the GreenProject backend (see docs/api_contract.md). */

const API_URL = process.env.NEXT_PUBLIC_API_URL ?? "http://localhost:8000";

export type GeoJSONGeometry = { type: string; coordinates: unknown };
export type GeoJSONFeature = { type: "Feature"; geometry: GeoJSONGeometry; properties: Record<string, unknown> };
export type GeoJSONFeatureCollection = { type: "FeatureCollection"; features: GeoJSONFeature[] };

export type ScoringMode = "heuristic" | "ml";
export type PlantingType = "tree" | "shrub" | "lawn";

export interface ProjectOut {
  id: string;
  name: string;
  source_crs: string | null;
  created_at: string;
  layers: GeoJSONFeatureCollection;
}

export interface PlanOut {
  plan_id: string;
  scoring_mode: ScoringMode;
  features: GeoJSONFeatureCollection;
}

export interface ValidationViolation {
  item_id: string;
  message: string;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(`${API_URL}${path}`, init);
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${init?.method ?? "GET"} ${path} failed: ${res.status} ${body}`);
  }
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

export async function uploadProject(name: string, file: File, sourceCrs?: string): Promise<{ project_id: string }> {
  const form = new FormData();
  form.append("name", name);
  form.append("file", file);
  if (sourceCrs) form.append("source_crs", sourceCrs);
  return request("/api/projects", { method: "POST", body: form });
}

export async function getProject(projectId: string): Promise<ProjectOut> {
  return request(`/api/projects/${projectId}`);
}

export async function generatePlan(
  projectId: string,
  plantingTypes: PlantingType[],
  scoringMode: ScoringMode
): Promise<PlanOut> {
  return request(`/api/projects/${projectId}/generate`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ planting_types: plantingTypes, scoring_mode: scoringMode }),
  });
}

export async function getPlan(projectId: string, planId: string): Promise<PlanOut> {
  return request(`/api/projects/${projectId}/plans/${planId}`);
}

export async function patchItem(
  projectId: string,
  planId: string,
  itemId: string,
  patch: { geometry?: GeoJSONGeometry; planting_type?: PlantingType; species?: string }
): Promise<GeoJSONFeature> {
  return request(`/api/projects/${projectId}/plans/${planId}/items/${itemId}`, {
    method: "PATCH",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(patch),
  });
}

export async function applyStructuredEdit(
  projectId: string,
  planId: string,
  operation: "remove_within_radius" | "replace_type_in_zone" | "exclude_polygon",
  params: Record<string, unknown>
): Promise<void> {
  return request(`/api/projects/${projectId}/plans/${planId}/edit-structured`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ operation, params }),
  });
}

export async function validatePlan(
  projectId: string,
  planId: string
): Promise<{ violations: ValidationViolation[] }> {
  return request(`/api/projects/${projectId}/plans/${planId}/validate`, { method: "POST" });
}

export function exportDxfUrl(projectId: string, planId: string): string {
  return `${API_URL}/api/projects/${projectId}/plans/${planId}/export.dxf`;
}

export async function getPlantingNorms(): Promise<Record<string, unknown>> {
  return request("/api/config/planting-norms");
}
