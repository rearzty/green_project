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
  /** True only when source_crs came from real evidence (auto-detected
   * geographic coordinates in the uploaded file) -- never for a value the
   * user typed in, even a plausible one, and never for DXF/DWG/ZIP, which
   * carries no CRS metadata at all. See backend's Project.crs_verified. */
  crs_verified: boolean;
  created_at: string;
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

interface ProjectUploadJobStatus {
  status: "pending" | "done" | "error";
  project_id: string | null;
  error: string | null;
}

function startUpload(name: string, file: File, sourceCrs?: string): Promise<{ job_id: string }> {
  const form = new FormData();
  form.append("name", name);
  form.append("file", file);
  if (sourceCrs) form.append("source_crs", sourceCrs);
  return request("/api/projects", { method: "POST", body: form });
}

function getUploadStatus(jobId: string): Promise<ProjectUploadJobStatus> {
  return request(`/api/projects/upload/${jobId}`);
}

const UPLOAD_POLL_INTERVAL_MS = 700;

/** Upload runs as a background job server-side -- DWG conversion + bundle
 * parsing can take well over a hundred seconds on a real multi-file ZIP (see
 * CLAUDE.md), too long to hold one HTTP request open for. This starts the
 * job (the file's own bytes still go up in this call -- that part is
 * genuinely tied to the request), polls status until it finishes, and
 * resolves to the same `{ project_id }` shape the old synchronous endpoint
 * returned -- same signature/return shape as before, so call sites don't
 * need to change, mirroring generatePlan()'s own polling-behind-one-Promise
 * pattern. */
export async function uploadProject(name: string, file: File, sourceCrs?: string): Promise<{ project_id: string }> {
  const { job_id } = await startUpload(name, file, sourceCrs);
  for (;;) {
    const status = await getUploadStatus(job_id);
    if (status.status === "error") throw new ApiError(status.error ?? "Не удалось загрузить проект.", 0);
    if (status.status === "done" && status.project_id) return { project_id: status.project_id };
    await new Promise((resolve) => setTimeout(resolve, UPLOAD_POLL_INTERVAL_MS));
  }
}

export async function getProject(projectId: string): Promise<ProjectOut> {
  return request(`/api/projects/${projectId}`);
}

/** The full vector layer geometry -- split out of ProjectOut and fetched
 * lazily (only by ThreeDView, on the first toggle into 3D) rather than on
 * every project open. See backend/app/schemas/project.py::ProjectOut's own
 * docstring: on a real 371,685-layer project this alone is a 170MB, 60+
 * second response, and most sessions never open 3D. */
export async function getProjectLayers(projectId: string): Promise<GeoJSONFeatureCollection> {
  return request(`/api/projects/${projectId}/layers`);
}

export interface LayersRasterGroup {
  key: string;
  label: string;
  color: string;
  count: number;
  /** Built client-side (project id + group key are all the backend route
   * needs) rather than returned by the API -- one less thing the response
   * shape has to carry per group. */
  url: string;
}

export interface LayersRaster {
  /** [[south, west], [north, east]] -- react-leaflet's <ImageOverlay
   * bounds=.../> shape directly. null for a project with no layers loaded. */
  bounds: [[number, number], [number, number]] | null;
  groups: LayersRasterGroup[];
}

/** The loaded source drawing, rendered server-side as one PNG per legend
 * group instead of GeoJSON features -- see backend/app/services/layer_raster.py's
 * docstring for why: raw layers were never interactive on the map (only
 * plan.features is), so paying a DOM node per object bought nothing and
 * broke down completely at real scale (371K+ objects on one street). */
export async function getLayersRaster(projectId: string): Promise<LayersRaster> {
  const raw = await request<{ bounds: [[number, number], [number, number]] | null; groups: Omit<LayersRasterGroup, "url">[] }>(
    `/api/projects/${projectId}/layers-raster`
  );
  return {
    bounds: raw.bounds,
    groups: raw.groups.map((g) => ({
      ...g,
      url: `${API_URL}/api/projects/${projectId}/layers-raster/image?group=${encodeURIComponent(g.key)}`,
    })),
  };
}

export interface GenerateJobStatus {
  status: "pending" | "done" | "error";
  plan_id: string | null;
  error: string | null;
}

function startGenerate(
  projectId: string,
  plantingTypes: PlantingType[],
  scoringMode: ScoringMode,
  treeSpacingM?: number,
  shrubSpacingM?: number
): Promise<{ job_id: string }> {
  // tree_spacing_m/shrub_spacing_m left undefined when not overridden --
  // JSON.stringify drops undefined object properties, so the field is simply
  // absent from the request body and the backend uses planting_norms.yaml's
  // own default, same as before this option existed.
  return postJson(`/api/projects/${projectId}/generate`, {
    planting_types: plantingTypes,
    scoring_mode: scoringMode,
    tree_spacing_m: treeSpacingM,
    shrub_spacing_m: shrubSpacingM,
  });
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
  options?: { signal?: AbortSignal; onPoll?: () => void; treeSpacingM?: number; shrubSpacingM?: number }
): Promise<PlanOut> {
  const { job_id } = await startGenerate(projectId, plantingTypes, scoringMode, options?.treeSpacingM, options?.shrubSpacingM);
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

/** Permanently removes one plan from history -- no undo (unlike the
 * item-level edits, which all have a restore path). The backend refuses
 * (409) to delete the project's current plan -- see
 * pipeline_service.py::CurrentPlanDeletionError. */
export async function deletePlan(projectId: string, planId: string): Promise<void> {
  return request(`/api/projects/${projectId}/plans/${planId}`, { method: "DELETE" });
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

interface ExportJobStatus {
  status: "pending" | "done" | "error";
  error: string | null;
}

function startDxfExport(projectId: string, planId: string): Promise<{ job_id: string }> {
  return request(`/api/projects/${projectId}/plans/${planId}/export-dxf`, { method: "POST" });
}

function getDxfExportStatus(projectId: string, planId: string, jobId: string): Promise<ExportJobStatus> {
  return request(`/api/projects/${projectId}/plans/${planId}/export-dxf/${jobId}`);
}

const EXPORT_POLL_INTERVAL_MS = 700;

/** DXF export runs as a background job server-side -- writing a real-scale
 * plan (hundreds of thousands of items) measured up to ~3 minutes (see
 * CLAUDE.md's export benchmark), too long to hold one HTTP request open
 * for. This starts the job and polls status until it finishes, then
 * resolves to the download URL. Deliberately doesn't fetch() the file
 * itself into JS memory -- the caller triggers a plain browser download
 * from the URL (see useProjectSession.ts::handleExportDxf) so a
 * multi-hundred-MB file streams straight to disk instead of through a JS
 * blob. */
export async function exportDxf(projectId: string, planId: string, options?: { signal?: AbortSignal }): Promise<string> {
  const { job_id } = await startDxfExport(projectId, planId);
  for (;;) {
    if (options?.signal?.aborted) throw new ApiError("Экспорт отменён.", 0);
    const status = await getDxfExportStatus(projectId, planId, job_id);
    if (status.status === "error") throw new ApiError(status.error ?? "Не удалось экспортировать DXF.", 0);
    if (status.status === "done") return `${API_URL}/api/projects/${projectId}/plans/${planId}/export-dxf/${job_id}/download`;
    await new Promise((resolve) => setTimeout(resolve, EXPORT_POLL_INTERVAL_MS));
  }
}

export interface SpeciesSpacing {
  min_distance_m: number;
  canopy_radius_m: number;
}

/** Mirrors geo_engine/norms.py::PlantingNorms — the full config, not a
 * subset (routes_config.py's `get_planting_norms` returns `model_dump()`
 * as-is). `species_spacing`'s `tree_default`/`shrub_default` entries are
 * what the 3D viewer sizes tree/shrub meshes from (lib/plan3d.ts) instead
 * of guessing canopy sizes independently. */
export interface PlantingNorms {
  setbacks_m: Record<string, Partial<Record<PlantingType, number>>>;
  species_spacing: Record<string, SpeciesSpacing>;
  min_candidate_area_m2: Record<PlantingType, number>;
  zoning_suitability: Record<string, number>;
}

export async function getPlantingNorms(): Promise<PlantingNorms> {
  return request("/api/config/planting-norms");
}

// Обоснование по нормативам (geo_engine/compliance.py via
// backend/app/api/routes_compliance.py) -- "почему здесь можно сажать" со
// ссылкой на акт и пункт, требование ТЗ, не бонус. Синхронные эндпоинты
// (не job/poll, как export-dxf): explain_items -- один векторизованный
// STRtree-проход, а не многоминутная запись DXF.

export interface ComplianceCheck {
  object_type: string;
  required_m: number;
  actual_m: number;
  satisfied: boolean;
  citation: string;
  table_row: string;
  verified: boolean;
}

export interface ItemCompliance {
  item_id: string;
  planting_type: PlantingType;
  species: string;
  compliant: boolean;
  binding_constraint: string | null;
  summary: string;
  checks: ComplianceCheck[];
}

/** Обоснование для конкретных посадок -- то, что панель "Почему здесь?"
 * запрашивает для одной выделенной точки, а не для всего плана целиком. */
export async function getItemsCompliance(projectId: string, planId: string, ids: string[]): Promise<ItemCompliance[]> {
  const { items } = await postJson<{ items: ItemCompliance[] }>(
    `/api/projects/${projectId}/plans/${planId}/compliance/items`,
    { ids }
  );
  return items;
}

/** Прямая ссылка на скачивание полного отчёта по плану -- вызывающий код
 * просто кликает по ней (тот же приём, что и у exportDxf), файл льётся из
 * ответа сервера напрямую в браузер, не через JS. */
export function complianceReportUrl(projectId: string, planId: string, format: "json" | "csv"): string {
  return `${API_URL}/api/projects/${projectId}/plans/${planId}/compliance-report.${format}`;
}

// Юна -- the in-app chat assistant (components/AssistantChat.tsx). The
// backend is a stateless proxy to an LLM (see assistant_service.py): it
// holds no conversation of its own, so the full chat log travels on every
// message, same as `settings` (the panel's current generation recipe) --
// Юна reasons about new absolute values relative to what's actually set,
// not blind deltas.

export type AssistantChatMessage = { role: "user" | "assistant"; content: string };

export interface AssistantGenerationSettings {
  planting_types: PlantingType[];
  tree_spacing_m?: number;
  shrub_spacing_m?: number;
}

/** Present only when Юна decided the message asked for a settings change --
 * the caller applies it via the same generatePlan() flow the control panel's
 * own "Сгенерировать план" button already uses. */
export interface AssistantAction {
  planting_types: PlantingType[];
  tree_spacing_m?: number;
  shrub_spacing_m?: number;
}

export interface AssistantMessageResult {
  reply: string;
  action: AssistantAction | null;
}

export async function askAssistant(
  projectId: string,
  message: string,
  history: AssistantChatMessage[],
  currentSettings: AssistantGenerationSettings
): Promise<AssistantMessageResult> {
  return postJson(`/api/projects/${projectId}/assistant/message`, { message, history, settings: currentSettings });
}
