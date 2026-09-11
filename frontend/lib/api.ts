/** Typed client for the GreenProject backend (see docs/api_contract.md). */

// NEXT_PUBLIC_* vars are inlined at `next build` time, not read at
// container-start time -- setting one in docker-compose's `environment:`
// has no effect here (that only exists at `docker run`, after the image,
// and thus the bundle, is already built). Falls back to whatever host
// actually served this page, same port offset (+5000: 3000->8000) -- works
// unmodified for localhost, 127.0.0.1, or a LAN IP (e.g. a phone on the
// same wifi hitting http://192.168.1.43:3000), including across a DHCP
// lease renewal that changes that IP, since nothing here is hardcoded to it.
const API_URL =
  process.env.NEXT_PUBLIC_API_URL ?? (typeof window !== "undefined" ? `http://${window.location.hostname}:8000` : "http://localhost:8000");

export type GeoJSONGeometry = { type: string; coordinates: unknown };
export type GeoJSONFeature = { type: "Feature"; geometry: GeoJSONGeometry; properties: Record<string, unknown> };
export type GeoJSONFeatureCollection = { type: "FeatureCollection"; features: GeoJSONFeature[] };

export type ScoringMode = "heuristic" | "ml";
export type PlantingType = "tree" | "shrub" | "lawn";
/** WGS84 lng/lat, the order the backend's move endpoint takes. */
export type LngLat = { x: number; y: number };

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

export interface PlanSummary {
  plan_id: string;
  scoring_mode: ScoringMode;
  created_at: string;
  is_current: boolean;
  item_count: number;
}

export interface ValidationViolation {
  item_id: string;
  message: string;
}

export interface ItemsRetypeResult {
  /** Pre-change snapshots of the items that actually changed type. */
  previous_items: GeoJSONFeature[];
  /** Items whose geometry can't take the requested type (e.g. a lawn polygon). */
  skipped_ids: string[];
}

/** An API call that failed -- `message` is already user-facing text (the
 * backend's own `detail`, which edit/project errors write in Russian), so
 * callers can hand it straight to a toast. */
export class ApiError extends Error {
  constructor(
    message: string,
    readonly status: number
  ) {
    super(message);
    this.name = "ApiError";
  }
}

function fallbackMessage(status: number): string {
  if (status === 404) return "Объект не найден — возможно, он уже удалён.";
  if (status === 409) return "План изменился — обновите выделение и попробуйте снова.";
  if (status === 422) return "Сервер не принял запрос: некорректные данные.";
  if (status >= 500) return `Ошибка на сервере (${status}). Попробуйте ещё раз.`;
  return `Запрос не выполнен (${status}).`;
}

async function errorFromResponse(res: Response): Promise<ApiError> {
  const text = await res.text();
  try {
    const detail = (JSON.parse(text) as { detail?: unknown }).detail;
    // HTTPException -> a plain string; FastAPI's own 422 validation error -> a list of {msg, loc}.
    if (typeof detail === "string" && detail) return new ApiError(detail, res.status);
    if (Array.isArray(detail) && detail.length > 0) {
      return new ApiError(`${fallbackMessage(res.status)} ${String((detail[0] as { msg?: unknown }).msg ?? "")}`.trim(), res.status);
    }
  } catch {
    // non-JSON body (e.g. a proxy's HTML 502 page) -- fall through to the generic message
  }
  return new ApiError(fallbackMessage(res.status), res.status);
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${API_URL}${path}`, init);
  } catch {
    throw new ApiError("Нет связи с сервером — проверьте, что backend запущен и доступен.", 0);
  }
  if (!res.ok) throw await errorFromResponse(res);
  if (res.status === 204) return undefined as T;
  return res.json() as Promise<T>;
}

function postJson<T>(path: string, body: unknown): Promise<T> {
  return request(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
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

export interface GenerateJobStatus {
  status: "pending" | "done" | "error";
  plan_id: string | null;
  error: string | null;
}

function startGenerate(projectId: string, plantingTypes: PlantingType[], scoringMode: ScoringMode): Promise<{ job_id: string }> {
  return postJson(`/api/projects/${projectId}/generate`, { planting_types: plantingTypes, scoring_mode: scoringMode });
}

function getGenerateStatus(projectId: string, jobId: string): Promise<GenerateJobStatus> {
  return request(`/api/projects/${projectId}/generate/${jobId}`);
}

const GENERATE_POLL_INTERVAL_MS = 700;

/** Generation runs as a background job server-side -- candidate generation +
 * greedy placement can take tens of seconds on a real-scale territory (see
 * CLAUDE.md), too long to hold one HTTP request open for. This starts the
 * job, polls status until it finishes, then fetches the finished plan --
 * hides all of that behind one Promise so call sites don't need to change.
 * `onPoll` fires after every status check (e.g. to drive a progress
 * indicator); `signal` lets a caller abandon polling (e.g. the user
 * navigated away) -- the job itself still finishes server-side either way,
 * it's just that nothing here is left waiting for it. */
export async function generatePlan(
  projectId: string,
  plantingTypes: PlantingType[],
  scoringMode: ScoringMode,
  options?: { signal?: AbortSignal; onPoll?: () => void }
): Promise<PlanOut> {
  const { job_id } = await startGenerate(projectId, plantingTypes, scoringMode);
  for (;;) {
    if (options?.signal?.aborted) throw new ApiError("Генерация отменена.", 0);
    const status = await getGenerateStatus(projectId, job_id);
    options?.onPoll?.();
    if (status.status === "error") throw new ApiError(status.error ?? "Не удалось сгенерировать план.", 0);
    if (status.status === "done" && status.plan_id) return getPlan(projectId, status.plan_id);
    await new Promise((resolve) => setTimeout(resolve, GENERATE_POLL_INTERVAL_MS));
  }
}

export async function getPlan(projectId: string, planId: string): Promise<PlanOut> {
  return request(`/api/projects/${projectId}/plans/${planId}`);
}

/** Every plan ever generated for this project (heuristic and ml alike) —
 * lets the UI show which plan is actually on screen instead of only ever
 * tracking the most recently generated one. */
export async function listPlans(projectId: string): Promise<PlanSummary[]> {
  return request(`/api/projects/${projectId}/plans`);
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

export async function deleteItem(projectId: string, planId: string, itemId: string): Promise<void> {
  return request(`/api/projects/${projectId}/plans/${planId}/items/${itemId}`, { method: "DELETE" });
}

// Batch edits by explicit item id -- what the map's selection tool and the
// local undo/redo use. Re-running one of these later (e.g. a redo) acts on
// exactly the same items no matter what else has changed in the plan since.

export interface ItemsDeleteResult {
  /** Pre-delete snapshots, for undo via restoreItems. */
  deleted_items: GeoJSONFeature[];
  /** The plan's fresh total, so the plan-history sidebar's count can be
   * patched locally instead of a separate listPlans() round trip. */
  item_count: number;
}

export async function deleteItems(projectId: string, planId: string, ids: string[]): Promise<ItemsDeleteResult> {
  return postJson(`/api/projects/${projectId}/plans/${planId}/items/delete`, { ids });
}

export async function retypeItems(
  projectId: string,
  planId: string,
  changes: { id: string; planting_type: PlantingType }[]
): Promise<ItemsRetypeResult> {
  return postJson(`/api/projects/${projectId}/plans/${planId}/items/retype`, { changes });
}

/** Translates the items by the vector from `from` to `to` -- all of them or
 * none (e.g. when any would end up outside the territory). Returns the
 * items' post-move features: the exact stored position depends on a WGS84
 * round trip through the project's source_crs that the frontend's own
 * optimistic drag preview doesn't replicate exactly, so callers apply these
 * back rather than trusting their own preview matches what got stored. */
export async function moveItems(projectId: string, planId: string, ids: string[], from: LngLat, to: LngLat): Promise<GeoJSONFeature[]> {
  const result = await postJson<{ items: GeoJSONFeature[] }>(`/api/projects/${projectId}/plans/${planId}/items/move`, {
    ids,
    from_point: from,
    to_point: to,
  });
  return result.items;
}

/** Undo's counterpart to deletion -- recreates the exact items given (same
 * id, geometry, type, species, score, rationale). Returns only the plan's
 * fresh item count: the caller already has the exact features it just asked
 * to restore (it sent them). */
export async function restoreItems(projectId: string, planId: string, items: GeoJSONFeature[]): Promise<{ item_count: number }> {
  return postJson(`/api/projects/${projectId}/plans/${planId}/items/restore`, { items });
}

export async function validatePlan(
  projectId: string,
  planId: string
): Promise<{ violations: ValidationViolation[] }> {
  return request(`/api/projects/${projectId}/plans/${planId}/validate`, { method: "POST" });
}

/** Rechecks only these items against the setback rulebook -- what an edit
 * just touched -- instead of the whole plan. The setback check is per-item
 * and independent of every other planting, so an edit can only ever change
 * violation status for the items it actually touched; callers merge this
 * into their own violation set by id instead of replacing it wholesale. */
export async function validateItems(projectId: string, planId: string, ids: string[]): Promise<{ violations: ValidationViolation[] }> {
  return postJson(`/api/projects/${projectId}/plans/${planId}/validate/items`, { ids });
}

export function exportDxfUrl(projectId: string, planId: string): string {
  return `${API_URL}/api/projects/${projectId}/plans/${planId}/export.dxf`;
}

export async function getPlantingNorms(): Promise<Record<string, unknown>> {
  return request("/api/config/planting-norms");
}
