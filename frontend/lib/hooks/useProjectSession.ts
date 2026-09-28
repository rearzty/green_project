"use client";

import { useEffect, useRef, useState } from "react";

import {
  deletePlan,
  exportDxf,
  generatePlan,
  getLayersRaster,
  getPlan,
  getProject,
  listPlans,
  uploadProject,
  type GeoJSONFeature,
  type GenerateJobStatus,
  type LayersRaster,
  type PlanOut,
  type PlanSummary,
  type PlantingType,
  type ProjectOut,
  type ProjectUploadJobStatus,
  type ScoringMode,
} from "@/lib/api";
import { buildPlanIndex, patchPlanIndex, type PlanIndex } from "@/lib/planIndex";
import { clearAllUndoHistory } from "@/lib/undoStack";

// Remembers which project/plan was open so a page refresh doesn't lose it --
// browser-local only (no login/accounts in this app), and never anything
// more than these two ids: re-fetched fresh from the backend on restore, not
// cached client-side, so it can't go stale the way caching the actual
// project/plan data here would.
const SESSION_STORAGE_KEY = "greenproject:session";

interface StoredSession {
  projectId: string;
  planId: string | null;
}

function loadStoredSession(): StoredSession | null {
  try {
    const raw = localStorage.getItem(SESSION_STORAGE_KEY);
    return raw ? (JSON.parse(raw) as StoredSession) : null;
  } catch {
    return null; // private-mode/storage-disabled browsers throw on access, not just return null
  }
}

function saveStoredSession(session: StoredSession | null) {
  try {
    if (session) localStorage.setItem(SESSION_STORAGE_KEY, JSON.stringify(session));
    else localStorage.removeItem(SESSION_STORAGE_KEY);
  } catch {
    // ignore -- losing the "remember me" convenience isn't worth surfacing an error for
  }
}

// What a successful edit changed in the currently-open plan -- applied
// straight to local state instead of re-fetching the whole plan (which, for
// a real-scale plan, is the single most expensive thing this app does; see
// CLAUDE.md's GeoJSON-building performance notes). Every one of these is
// built from what the edit's own response already returned, not a guess.
export type LocalEditEffect =
  | { kind: "move"; items: GeoJSONFeature[] }
  | { kind: "retype"; changes: { id: string; type: PlantingType }[] }
  | { kind: "delete"; ids: string[] }
  | { kind: "restore"; items: GeoJSONFeature[] };

function applyEffectToFeatures(features: GeoJSONFeature[], effect: LocalEditEffect): GeoJSONFeature[] {
  switch (effect.kind) {
    case "move": {
      const byId = new Map(effect.items.map((f) => [String(f.properties.id), f]));
      return features.map((f) => byId.get(String(f.properties.id)) ?? f);
    }
    case "retype": {
      const byId = new Map(effect.changes.map((c) => [c.id, c.type]));
      return features.map((f) => {
        const type = byId.get(String(f.properties.id));
        return type ? { ...f, properties: { ...f.properties, planting_type: type } } : f;
      });
    }
    case "delete": {
      const ids = new Set(effect.ids);
      return features.filter((f) => !ids.has(String(f.properties.id)));
    }
    case "restore":
      return [...features, ...effect.items];
  }
}

/** Best-effort progress for a long-running background job -- shared shape for
 * both the upload and generate polling loops (see api.ts's
 * ProjectUploadJobStatus/GenerateJobStatus, which this is derived from).
 * `progress` null means "no countable total yet", not "0%" -- callers should
 * render an indeterminate bar in that case rather than a stuck empty one. */
export interface JobProgress {
  stage: string;
  progress: number | null;
}

/** Owns the currently-open project/plan and the actions that change *which*
 * plan is open (upload, generate, switch, clear) -- session persistence,
 * the plan-history sidebar list, and the two primitives everything else
 * (selection, undo/redo, edits -- see usePlanEdits) builds on:
 * `applyPlan` swaps to a different plan entirely, `applyLocalEdit` patches
 * the *current* one in place from an edit's own response. Both bump
 * `planRevision`, the signal MapView's plan layer remounts on (react-leaflet
 * doesn't diff its `data` prop -- see CLAUDE.md).
 */
export function useProjectSession() {
  const [project, setProject] = useState<ProjectOut | null>(null);
  // The map's source-layer backdrop -- fetched once per project (layers are
  // immutable after upload, see layer_raster.py), separately from `project`
  // itself so a failed/slow raster fetch can't block restoring the session.
  const [layersRaster, setLayersRaster] = useState<LayersRaster | null>(null);
  const [plan, setPlan] = useState<PlanOut | null>(null);
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [planRevision, setPlanRevision] = useState(0);
  const [generating, setGenerating] = useState(false);
  const [uploadProgress, setUploadProgress] = useState<JobProgress | null>(null);
  const [generateProgress, setGenerateProgress] = useState<JobProgress | null>(null);
  const [exporting, setExporting] = useState(false);
  const [restoring, setRestoring] = useState(true);

  // Not React state on purpose: a full rebuild on every edit (the natural
  // useMemo-on-plan approach) measured at up to ~1.6s on a real-scale plan
  // (see CLAUDE.md), almost all of it useless work re-deriving entries that
  // didn't change. Mutated in place by patchPlanIndex and read fresh on
  // every render instead -- consumers already re-render on planRevision, so
  // no separate change-signal is needed for this ref.
  const planIndexRef = useRef<PlanIndex | undefined>(undefined);

  function applyPlan(next: PlanOut | null) {
    planIndexRef.current = next ? buildPlanIndex(next.features) : undefined;
    setPlan(next);
    setPlanRevision((r) => r + 1);
  }

  function applyLocalEdit(effect: LocalEditEffect) {
    if (planIndexRef.current) patchPlanIndex(planIndexRef.current, effect);
    setPlan((prev) => (prev ? { ...prev, features: { ...prev.features, features: applyEffectToFeatures(prev.features.features, effect) } } : prev));
    setPlanRevision((r) => r + 1);
  }

  /** Forces a remount of the plan layer from whatever `plan` already holds
   * -- no local mutation, no refetch. Used after a rejected edit: nothing
   * changed locally (the action never got to applyLocalEdit), so this only
   * needs to undo a live drag-preview's raw DOM position (see
   * MapView.tsx::ClusteredMarkers), not fix any actual data. */
  function bumpPlanRevision() {
    setPlanRevision((r) => r + 1);
  }

  function patchPlanItemCount(forPlanId: string, itemCount: number) {
    setPlans((prev) => prev.map((p) => (p.plan_id === forPlanId ? { ...p, item_count: itemCount } : p)));
  }

  async function refreshPlans(projectId: string) {
    setPlans(await listPlans(projectId));
  }

  /** Own try/catch, not folded into the callers' -- a raster failure (a
   * transient network hiccup, a project with layers PIL chokes on) shouldn't
   * take down the whole restore/upload flow the way a project fetch failure
   * should. The map just shows no backdrop instead. */
  async function refreshLayersRaster(projectId: string) {
    try {
      setLayersRaster(await getLayersRaster(projectId));
    } catch {
      setLayersRaster(null);
    }
  }

  // Runs once on mount, client-side only (localStorage isn't available
  // during SSR) -- restores whatever project/plan was open on the last
  // visit. A stale/deleted id (e.g. the dev DB got truncated) just fails the
  // fetch silently and forgets it, rather than leaving the page stuck.
  useEffect(() => {
    const stored = loadStoredSession();
    if (!stored) {
      setRestoring(false);
      return;
    }
    (async () => {
      try {
        const fetchedProject = await getProject(stored.projectId);
        setProject(fetchedProject);
        await refreshLayersRaster(stored.projectId);
        await refreshPlans(stored.projectId);
        if (stored.planId) applyPlan(await getPlan(stored.projectId, stored.planId));
      } catch {
        saveStoredSession(null);
      } finally {
        setRestoring(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function progressFromStatus(status: ProjectUploadJobStatus | GenerateJobStatus): JobProgress | null {
    return status.stage ? { stage: status.stage, progress: status.progress } : null;
  }

  async function handleUpload(file: File, name: string, sourceCrs: string) {
    setUploadProgress(null);
    try {
      const { project_id } = await uploadProject(name, file, sourceCrs || undefined, {
        onPoll: (status) => setUploadProgress(progressFromStatus(status)),
      });
      const fetched = await getProject(project_id);
      setProject(fetched);
      await refreshLayersRaster(project_id);
      applyPlan(null);
      setPlans([]);
      saveStoredSession({ projectId: project_id, planId: null });
    } finally {
      setUploadProgress(null);
    }
  }

  async function handleGenerate(
    plantingTypes: PlantingType[],
    scoringMode: ScoringMode,
    spacing?: { treeSpacingM?: number; shrubSpacingM?: number }
  ) {
    if (!project) return;
    setGenerating(true);
    setGenerateProgress(null);
    try {
      const generated = await generatePlan(project.id, plantingTypes, scoringMode, {
        ...spacing,
        onPoll: (status) => setGenerateProgress(progressFromStatus(status)),
      });
      applyPlan(generated);
      await refreshPlans(project.id);
      saveStoredSession({ projectId: project.id, planId: generated.plan_id });
    } finally {
      setGenerating(false);
      setGenerateProgress(null);
    }
  }

  /** Exports the current plan to DXF -- runs as a background job
   * server-side (see lib/api.ts::exportDxf), so this just waits for the
   * download URL and then triggers a plain browser download from it. Not a
   * simple <a href> anymore (the previous design): writing a real-scale
   * plan can take minutes, too long for a link the browser might time out
   * waiting on. */
  async function handleExportDxf() {
    if (!project || !plan) return;
    setExporting(true);
    try {
      const downloadUrl = await exportDxf(project.id, plan.plan_id);
      const link = document.createElement("a");
      link.href = downloadUrl;
      document.body.appendChild(link);
      link.click();
      link.remove();
    } finally {
      setExporting(false);
    }
  }

  async function handleSelectPlan(nextPlanId: string) {
    if (!project) return;
    applyPlan(await getPlan(project.id, nextPlanId));
    saveStoredSession({ projectId: project.id, planId: nextPlanId });
  }

  /** Permanently removes a plan from history (no undo, unlike item-level
   * edits) -- the backend itself refuses to delete the project's current
   * plan (409), so this never has to reason about "what becomes current
   * instead". If the plan being deleted is the one currently open (a past,
   * non-current plan the user had clicked into from history), clears the
   * view rather than leaving stale state pointing at a plan that no longer
   * exists. */
  async function handleDeletePlan(planId: string) {
    if (!project) return;
    await deletePlan(project.id, planId);
    setPlans((prev) => prev.filter((p) => p.plan_id !== planId));
    if (plan?.plan_id === planId) {
      applyPlan(null);
      saveStoredSession({ projectId: project.id, planId: null });
    }
  }

  function handleClearAll() {
    saveStoredSession(null);
    clearAllUndoHistory();
    setProject(null);
    setLayersRaster(null);
    applyPlan(null);
    setPlans([]);
  }

  return {
    project,
    layersRaster,
    plan,
    plans,
    planRevision,
    // Read fresh every render, not memoized -- planIndexRef.current is
    // already updated (by applyPlan/applyLocalEdit) before the setState
    // calls that cause this render, so there's nothing to recompute here.
    planIndex: planIndexRef.current,
    generating,
    uploadProgress,
    generateProgress,
    exporting,
    restoring,
    applyPlan,
    applyLocalEdit,
    bumpPlanRevision,
    patchPlanItemCount,
    refreshPlans,
    handleUpload,
    handleGenerate,
    handleExportDxf,
    handleSelectPlan,
    handleDeletePlan,
    handleClearAll,
  };
}
