"use client";

import dynamic from "next/dynamic";
import { useEffect, useState } from "react";

import { ControlPanel } from "@/components/ControlPanel";
import {
  applyStructuredEdit,
  deleteItem,
  exportDxfUrl,
  generatePlan,
  getPlan,
  getProject,
  listPlans,
  patchItem,
  uploadProject,
  validatePlan,
  type EditMode,
  type PlanOut,
  type PlanSummary,
  type PlantingType,
  type ProjectOut,
  type ScoringMode,
  type ValidationViolation,
} from "@/lib/api";

// Leaflet touches `window` on import, so the map must never render on the server.
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

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

export default function Home() {
  const [project, setProject] = useState<ProjectOut | null>(null);
  const [plan, setPlan] = useState<PlanOut | null>(null);
  const [plans, setPlans] = useState<PlanSummary[]>([]);
  const [violations, setViolations] = useState<ValidationViolation[] | null>(null);
  const [showLayers, setShowLayers] = useState(true);
  const [showPlan, setShowPlan] = useState(true);
  const [restoring, setRestoring] = useState(true);
  // react-leaflet's <GeoJSON> only reads its `data` prop once, at layer
  // creation -- it does not reactively re-diff on prop changes (same
  // limitation layersKey/planKey below already work around for a full plan
  // swap). An item-level edit (drag/retype/delete/structured op) reuses the
  // *same* plan_id with different `features`, so plan_id alone isn't enough
  // to force a remount -- bump this alongside every setPlan() so planKey
  // changes even when plan_id doesn't, and the map actually shows the edit.
  const [planRevision, setPlanRevision] = useState(0);

  function applyPlan(next: PlanOut | null) {
    setPlan(next);
    setPlanRevision((r) => r + 1);
  }

  const [editMode, setEditMode] = useState<EditMode>("none");
  const [editCenter, setEditCenter] = useState<[number, number] | null>(null);
  const [editRadiusM, setEditRadiusM] = useState(10);
  const [editPolygon, setEditPolygon] = useState<[number, number][]>([]);
  const [editTargetType, setEditTargetType] = useState<PlantingType>("tree");

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
        if (stored.planId) {
          const fetchedPlan = await getPlan(stored.projectId, stored.planId);
          applyPlan(fetchedPlan);
        }
      } catch {
        saveStoredSession(null);
      } finally {
        setRestoring(false);
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  async function refreshPlans(projectId: string) {
    setPlans(await listPlans(projectId));
  }

  async function handleUpload(file: File, name: string, sourceCrs: string) {
    const { project_id } = await uploadProject(name, file, sourceCrs || undefined);
    const fetched = await getProject(project_id);
    setProject(fetched);
    applyPlan(null);
    setPlans([]);
    setViolations(null);
    resetEdit();
    saveStoredSession({ projectId: project_id, planId: null });
  }

  async function handleGenerate(plantingTypes: PlantingType[], scoringMode: ScoringMode) {
    if (!project) return;
    const generated = await generatePlan(project.id, plantingTypes, scoringMode);
    applyPlan(generated);
    setViolations(null);
    await refreshPlans(project.id);
    saveStoredSession({ projectId: project.id, planId: generated.plan_id });
  }

  async function handleSelectPlan(planId: string) {
    if (!project) return;
    const selected = await getPlan(project.id, planId);
    applyPlan(selected);
    setViolations(null);
    saveStoredSession({ projectId: project.id, planId });
  }

  function handleClearAll() {
    saveStoredSession(null);
    setProject(null);
    applyPlan(null);
    setPlans([]);
    setViolations(null);
    resetEdit();
  }

  /** Re-fetches the current plan + history after a click-a-tree edit
   * (move/retype/delete) — same refresh `handleApplyEdit` already does for
   * the area-based structured edits, so plan.item_count/history/violations
   * stay in sync regardless of which editing path was used. */
  async function refreshAfterItemEdit() {
    if (!project || !plan) return;
    const refreshed = await getPlan(project.id, plan.plan_id);
    applyPlan(refreshed);
    setViolations(null);
    await refreshPlans(project.id);
  }

  async function handleMoveItem(itemId: string, lat: number, lng: number) {
    if (!project || !plan) return;
    try {
      await patchItem(project.id, plan.plan_id, itemId, { geometry: { type: "Point", coordinates: [lng, lat] } });
      await refreshAfterItemEdit();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleRetypeItem(itemId: string, type: PlantingType) {
    if (!project || !plan) return;
    try {
      await patchItem(project.id, plan.plan_id, itemId, { planting_type: type });
      await refreshAfterItemEdit();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleDeleteItem(itemId: string) {
    if (!project || !plan) return;
    try {
      await deleteItem(project.id, plan.plan_id, itemId);
      await refreshAfterItemEdit();
    } catch (e) {
      alert(e instanceof Error ? e.message : String(e));
    }
  }

  async function handleValidate() {
    if (!project || !plan) return;
    const result = await validatePlan(project.id, plan.plan_id);
    setViolations(result.violations);
  }

  function resetEdit() {
    setEditMode("none");
    setEditCenter(null);
    setEditPolygon([]);
  }

  function handleSetEditMode(mode: EditMode) {
    setEditMode(mode);
    setEditCenter(null);
    setEditPolygon([]);
  }

  function handleMapClick(lat: number, lng: number) {
    if (editMode === "remove_within_radius") {
      setEditCenter([lat, lng]);
    } else if (editMode === "exclude_polygon" || editMode === "replace_type_in_zone") {
      setEditPolygon((prev) => [...prev, [lat, lng]]);
    }
  }

  const canApplyEdit =
    (editMode === "remove_within_radius" && editCenter !== null) ||
    ((editMode === "exclude_polygon" || editMode === "replace_type_in_zone") && editPolygon.length >= 3);

  async function handleApplyEdit() {
    if (!project || !plan || editMode === "none") return;

    if (editMode === "remove_within_radius" && editCenter) {
      const [lat, lng] = editCenter;
      await applyStructuredEdit(project.id, plan.plan_id, editMode, { x: lng, y: lat, radius_m: editRadiusM });
    } else if ((editMode === "exclude_polygon" || editMode === "replace_type_in_zone") && editPolygon.length >= 3) {
      // GeoJSON ring order is [lng, lat], closed (first point repeated last).
      const ring = editPolygon.map(([lat, lng]) => [lng, lat]);
      ring.push(ring[0]);
      const params: Record<string, unknown> = { polygon: { type: "Polygon", coordinates: [ring] } };
      if (editMode === "replace_type_in_zone") params.planting_type = editTargetType;
      await applyStructuredEdit(project.id, plan.plan_id, editMode, params);
    }

    const refreshed = await getPlan(project.id, plan.plan_id);
    applyPlan(refreshed);
    setViolations(null);
    resetEdit();
    await refreshPlans(project.id);
  }

  return (
    <main className="flex h-full w-full">
      <ControlPanel
        hasProject={project !== null}
        hasPlan={plan !== null}
        restoring={restoring}
        violations={violations}
        onUpload={handleUpload}
        onGenerate={handleGenerate}
        onValidate={handleValidate}
        onClearAll={handleClearAll}
        exportHref={project && plan ? exportDxfUrl(project.id, plan.plan_id) : undefined}
        showLayers={showLayers}
        showPlan={showPlan}
        onToggleShowLayers={setShowLayers}
        onToggleShowPlan={setShowPlan}
        plans={plans}
        currentPlanId={plan?.plan_id}
        onSelectPlan={handleSelectPlan}
        editMode={editMode}
        onSetEditMode={handleSetEditMode}
        editRadiusM={editRadiusM}
        onSetEditRadiusM={setEditRadiusM}
        editTargetType={editTargetType}
        onSetEditTargetType={setEditTargetType}
        canApplyEdit={canApplyEdit}
        onApplyEdit={handleApplyEdit}
        onCancelEdit={resetEdit}
      />
      <div className="flex-1">
        <MapView
          layers={project?.layers}
          plan={plan?.features}
          layersKey={project?.id}
          planKey={plan ? `${plan.plan_id}:${planRevision}` : undefined}
          showLayers={showLayers}
          showPlan={showPlan}
          editMode={editMode}
          editCenter={editCenter}
          editRadiusM={editRadiusM}
          editPolygon={editPolygon}
          onMapClick={handleMapClick}
          onMoveItem={handleMoveItem}
          onRetypeItem={handleRetypeItem}
          onDeleteItem={handleDeleteItem}
        />
      </div>
    </main>
  );
}
