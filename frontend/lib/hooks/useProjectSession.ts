"use client";

import { useEffect, useState } from "react";

import {
  exportDxfUrl,
  generatePlan,
  getPlan,
  getProject,
  listPlans,
  uploadProject,
  type GeoJSONFeature,
  type PlanOut,
  type PlanSummary,
  type PlantingType,
  type ProjectOut,
  type ScoringMode,
} from "@/lib/api";
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
  const [plan, setPlan] = useState<PlanOut | null>(null);
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [planRevision, setPlanRevision] = useState(0);
  const [generating, setGenerating] = useState(false);
  const [restoring, setRestoring] = useState(true);

  function applyPlan(next: PlanOut | null) {
    setPlan(next);
    setPlanRevision((r) => r + 1);
  }

  function applyLocalEdit(effect: LocalEditEffect) {
    setPlan((prev) => (prev ? { ...prev, features: { ...prev.features, features: applyEffectToFeatures(prev.features.features, effect) } } : prev));
    setPlanRevision((r) => r + 1);
  }

  /** Forces a remount of the plan layer from whatever `plan` already holds
   * -- no local mutation, no refetch. Used after a rejected edit: nothing
   * changed locally (the action never got to applyLocalEdit), so this only
   * needs to undo a live drag-preview's raw DOM position (see
   * MapView.tsx::VirtualizedMarkers), not fix any actual data. */
  function bumpPlanRevision() {
    setPlanRevision((r) => r + 1);
  }

  function patchPlanItemCount(forPlanId: string, itemCount: number) {
    setPlans((prev) => prev.map((p) => (p.plan_id === forPlanId ? { ...p, item_count: itemCount } : p)));
  }

  async function refreshPlans(projectId: string) {
    setPlans(await listPlans(projectId));
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

  async function handleUpload(file: File, name: string, sourceCrs: string) {
    const { project_id } = await uploadProject(name, file, sourceCrs || undefined);
    const fetched = await getProject(project_id);
    setProject(fetched);
    applyPlan(null);
    setPlans([]);
    saveStoredSession({ projectId: project_id, planId: null });
  }

  async function handleGenerate(plantingTypes: PlantingType[], scoringMode: ScoringMode) {
    if (!project) return;
    setGenerating(true);
    try {
      const generated = await generatePlan(project.id, plantingTypes, scoringMode);
      applyPlan(generated);
      await refreshPlans(project.id);
      saveStoredSession({ projectId: project.id, planId: generated.plan_id });
    } finally {
      setGenerating(false);
    }
  }

  async function handleSelectPlan(nextPlanId: string) {
    if (!project) return;
    applyPlan(await getPlan(project.id, nextPlanId));
    saveStoredSession({ projectId: project.id, planId: nextPlanId });
  }

  function handleClearAll() {
    saveStoredSession(null);
    clearAllUndoHistory();
    setProject(null);
    applyPlan(null);
    setPlans([]);
  }

  return {
    project,
    plan,
    plans,
    planRevision,
    generating,
    restoring,
    applyPlan,
    applyLocalEdit,
    bumpPlanRevision,
    patchPlanItemCount,
    refreshPlans,
    handleUpload,
    handleGenerate,
    handleSelectPlan,
    handleClearAll,
    exportHref: project && plan ? exportDxfUrl(project.id, plan.plan_id) : undefined,
  };
}
