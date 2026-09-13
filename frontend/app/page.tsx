"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState } from "react";
import {
  CheckCheck,
  PanelLeftClose,
  PanelLeftOpen,
  Redo2,
  Shrub,
  SquareDashedMousePointer,
  Trash2,
  TreeDeciduous,
  Undo2,
  X,
} from "lucide-react";

import { ContextMenu, type ContextMenuEntry } from "@/components/ContextMenu";
import { ControlPanel } from "@/components/ControlPanel";
import { QualityBadge } from "@/components/QualityBadge";
import { Toaster } from "@/components/Toaster";
import { useKeyboardShortcuts } from "@/lib/hooks/useKeyboardShortcuts";
import { usePlanEdits } from "@/lib/hooks/usePlanEdits";
import { useProjectSession } from "@/lib/hooks/useProjectSession";
import { useSelection } from "@/lib/hooks/useSelection";
import { AssistantChat } from "@/components/AssistantChat";
import { getPlantingNorms, type PlantingNorms, type PlantingType, type ScoringMode } from "@/lib/api";
import { countLabel, OBJECT_FORMS } from "@/lib/format";
import { buildLayerLegend, type Season } from "@/lib/mapStyle";
import { errorMessage, toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

// Leaflet touches `window` on import, so the map must never render on the server.
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });
// Same reasoning as MapView -- three.js's WebGLRenderer needs a real DOM/GPU context.
const ThreeDView = dynamic(() => import("@/components/ThreeDView"), { ssr: false });

export default function Home() {
  const session = useProjectSession();
  const { project, plan, plans, planRevision, planIndex } = session;

  const [hiddenLayerTypes, setHiddenLayerTypes] = useState<Set<string>>(new Set());
  const [showPlan, setShowPlan] = useState(true);
  const [view3d, setView3d] = useState(false);
  const [season, setSeason] = useState<Season>("summer");
  // Lifted out of ControlPanel (used to be its own local state) so
  // AssistantChat can read and change the same generation recipe Юна talks
  // about -- see ControlPanel.tsx's own comment on why this moved.
  const [plantingTypes, setPlantingTypes] = useState<PlantingType[]>(["tree", "shrub", "lawn"]);
  const [scoringMode, setScoringMode] = useState<ScoringMode>("heuristic");
  // undefined = not overridden -- generate omits the field and the backend
  // falls back to planting_norms.yaml's own tree_default/shrub_default.
  const [treeSpacingM, setTreeSpacingM] = useState<number | undefined>(undefined);
  const [shrubSpacingM, setShrubSpacingM] = useState<number | undefined>(undefined);
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
  // Defaults open (matches the always-visible panel this replaces) and is
  // corrected once on mount for phones -- SSR has no window to check, so a
  // static default that's occasionally wrong for one frame beats guessing.
  const [sidebarOpen, setSidebarOpen] = useState(true);

  useEffect(() => {
    if (!window.matchMedia("(min-width: 768px)").matches) setSidebarOpen(false);
  }, []);
  // Global config, not per-project -- fetched once so ControlPanel can show
  // the interval inputs' real current default instead of a hardcoded guess.
  const [plantingNorms, setPlantingNorms] = useState<PlantingNorms | undefined>(undefined);

  useEffect(() => {
    let cancelled = false;
    getPlantingNorms()
      .then((norms) => {
        if (!cancelled) setPlantingNorms(norms);
      })
      .catch(() => {
        // Placeholder just falls back to a hardcoded number in ControlPanel -- not worth a toast.
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const layerLegend = useMemo(() => buildLayerLegend(project?.layers), [project]);

  function handleToggleLayerType(key: string) {
    setHiddenLayerTypes((prev) => {
      const next = new Set(prev);
      if (next.has(key)) next.delete(key);
      else next.add(key);
      return next;
    });
  }

  // editing needs selection.setSelection (to clear/restore selection around
  // an edit), selection needs editing.violationIds (to show a selected
  // item's violation status) -- passing a closure here instead of
  // `selection` itself sidesteps the chicken-and-egg ordering: the closure
  // isn't actually *called* until well after this render finishes (in
  // response to a later edit), by which point `selection` below is assigned.
  const editing = usePlanEdits(project, plan, session.applyLocalEdit, session.bumpPlanRevision, session.patchPlanItemCount, (ids) =>
    selection.setSelection(ids)
  );
  const selection = useSelection(planIndex, planRevision, editing.violationIds);

  function toggleView3d() {
    // Turning it off is always allowed, same reasoning as toggleSelectMode
    // below -- only turning it on needs a plan to actually show.
    if (view3d) {
      setView3d(false);
      return;
    }
    if (!plan) return;
    setView3d(true);
  }

  function handleToggleShowPlan(show: boolean) {
    setShowPlan(show);
    if (!show) {
      selection.setSelectMode(false);
      selection.clear();
    }
  }

  function toggleSelectMode() {
    // Turning it off is always allowed -- only turning it on needs a
    // visible plan. Otherwise a plan disappearing (Clear All, switching
    // projects) while select mode was on could leave it stuck with no way
    // to switch it back off.
    if (selection.selectMode) {
      selection.setSelectMode(false);
      setContextMenu(null);
      return;
    }
    if (!plan) return;
    if (!showPlan) {
      toast.info("Включите отображение плана, чтобы выделять объекты.");
      return;
    }
    selection.setSelectMode(true);
    setContextMenu(null);
  }

  /** Юна decided the chat message asked for a settings change -- applies it
   * through the exact same generate path the panel's own button uses (so the
   * plan-history refresh, session persistence, and the shared `generating`
   * indicator all just work), after first updating the lifted state so the
   * panel's own inputs immediately reflect what Юна just set. */
  async function handleAssistantAction(action: { planting_types: PlantingType[]; tree_spacing_m?: number; shrub_spacing_m?: number }) {
    setPlantingTypes(action.planting_types);
    setTreeSpacingM(action.tree_spacing_m);
    setShrubSpacingM(action.shrub_spacing_m);
    try {
      await session.handleGenerate(action.planting_types, scoringMode, {
        treeSpacingM: action.tree_spacing_m,
        shrubSpacingM: action.shrub_spacing_m,
      });
    } catch (e) {
      toast.error(errorMessage(e));
      throw e; // lets AssistantChat show the failure in the chat log too, not just a toast
    }
  }

  function handleEscape() {
    if (contextMenu) setContextMenu(null);
    else if (selection.selectedIds.size > 0) selection.clear();
    else if (selection.selectMode) selection.setSelectMode(false);
  }

  const closeContextMenu = useCallback(() => setContextMenu(null), []);
  const handleContextMenu = useCallback((x: number, y: number) => setContextMenu({ x, y }), []);

  useKeyboardShortcuts({
    undo: () => editing.undo(),
    redo: editing.redo,
    deleteSelection: () => editing.handleDeleteSelection(selection.selectedIds),
    retype: (type) => editing.handleRetypeSelection(selection.selectedIds, planIndex, type),
    toggleSelectMode,
    selectAll: selection.selectAll,
    escape: handleEscape,
    hasPlan: plan !== null,
    hasSelection: selection.selectedIds.size > 0,
  });

  const hasSelection = selection.selectedIds.size > 0;

  // Score already travels on every feature's properties (geo_io.py) and
  // useProjectSession keeps plan.features in sync through every edit path
  // (move/retype/delete/restore) -- so this is always derived fresh from
  // whatever's actually loaded, never a separately-stored, separately-stale
  // number. Clamped only for display -- HeuristicScorer's weighted sum isn't
  // hard-clamped to [0,1] the way MLScorer's predict_proba is.
  const qualityIndex = useMemo(() => {
    const features = plan?.features.features;
    if (!features?.length) return null;
    const scores = features.map((f) => Number((f.properties as Record<string, unknown>).score)).filter(Number.isFinite);
    if (!scores.length) return null;
    const avg = scores.reduce((s, v) => s + v, 0) / scores.length;
    return Math.min(100, Math.max(0, Math.round(avg * 100)));
  }, [plan]);

  const contextMenuEntries: ContextMenuEntry[] = [
    {
      label: "Сделать деревом",
      hotkey: "1",
      icon: TreeDeciduous,
      disabled: !hasSelection || editing.editBusy,
      onSelect: () => editing.handleRetypeSelection(selection.selectedIds, planIndex, "tree"),
    },
    {
      label: "Сделать кустарником",
      hotkey: "2",
      icon: Shrub,
      disabled: !hasSelection || editing.editBusy,
      onSelect: () => editing.handleRetypeSelection(selection.selectedIds, planIndex, "shrub"),
    },
    {
      label: "Удалить",
      hotkey: "Del",
      icon: Trash2,
      danger: true,
      disabled: !hasSelection || editing.editBusy,
      onSelect: () => editing.handleDeleteSelection(selection.selectedIds),
    },
    "separator",
    { label: "Выделить всё", hotkey: "Ctrl+A", icon: CheckCheck, disabled: !planIndex, onSelect: selection.selectAll },
    { label: "Снять выделение", hotkey: "Esc", icon: X, disabled: !hasSelection, onSelect: selection.clear },
    "separator",
    { label: "Отменить", hotkey: "Ctrl+Z", icon: Undo2, disabled: !editing.canUndo || editing.editBusy, onSelect: () => editing.undo() },
    { label: "Повторить", hotkey: "Ctrl+Y", icon: Redo2, disabled: !editing.canRedo || editing.editBusy, onSelect: editing.redo },
    "separator",
    {
      label: selection.selectMode ? "Выключить режим выделения" : "Включить режим выделения",
      hotkey: "S",
      icon: SquareDashedMousePointer,
      onSelect: toggleSelectMode,
    },
  ];

  return (
    <main className="flex h-full w-full">
      <div
        className={cn(
          "h-full flex-none overflow-hidden transition-all duration-200 ease-in-out",
          "fixed inset-y-0 left-0 z-40 md:static md:z-auto",
          sidebarOpen ? "w-80 translate-x-0" : "-translate-x-full md:w-0 md:translate-x-0"
        )}
      >
        <ControlPanel
          hasProject={project !== null}
          hasPlan={plan !== null}
          restoring={session.restoring}
          generating={session.generating}
          exporting={session.exporting}
          onUpload={session.handleUpload}
          onGenerate={session.handleGenerate}
          plantingTypes={plantingTypes}
          onPlantingTypesChange={setPlantingTypes}
          scoringMode={scoringMode}
          onScoringModeChange={setScoringMode}
          treeSpacingM={treeSpacingM}
          onTreeSpacingMChange={setTreeSpacingM}
          shrubSpacingM={shrubSpacingM}
          onShrubSpacingMChange={setShrubSpacingM}
          plantingNorms={plantingNorms}
          onExportDxf={session.handleExportDxf}
          onClearAll={session.handleClearAll}
          layerLegend={layerLegend}
          hiddenLayerTypes={hiddenLayerTypes}
          onToggleLayerType={handleToggleLayerType}
          showPlan={showPlan}
          onToggleShowPlan={handleToggleShowPlan}
          view3d={view3d}
          onToggleView3d={toggleView3d}
          season={season}
          onSeasonChange={setSeason}
          plans={plans}
          currentPlanId={plan?.plan_id}
          onSelectPlan={session.handleSelectPlan}
          onDeletePlan={session.handleDeletePlan}
          editBusy={editing.editBusy}
          canUndo={editing.canUndo}
          canRedo={editing.canRedo}
          onUndo={() => editing.undo()}
          onRedo={editing.redo}
          selectMode={selection.selectMode}
          onToggleSelectMode={toggleSelectMode}
          selection={selection.summary}
          onRetypeSelection={(type) => editing.handleRetypeSelection(selection.selectedIds, planIndex, type)}
          onDeleteSelection={() => editing.handleDeleteSelection(selection.selectedIds)}
          onClearSelection={selection.clear}
          onSelectAll={selection.selectAll}
          validating={editing.validating}
          violations={editing.violations}
          onFocusItem={selection.focusItem}
        />
      </div>
      {sidebarOpen && (
        <div className="fixed inset-0 z-30 bg-black/40 md:hidden" onClick={() => setSidebarOpen(false)} aria-hidden />
      )}
      <button
        onClick={() => setSidebarOpen((v) => !v)}
        title={sidebarOpen ? "Скрыть панель" : "Показать панель"}
        aria-pressed={sidebarOpen}
        className={cn(
          // top-24, not top-4 -- Leaflet's own zoom control sits at the
          // map's top-left corner (~10-74px from the top, confirmed live)
          // and renders above this button (leaflet's z-index 1000 > our
          // 500), silently eating clicks at a naive top-4 position.
          "fixed top-24 z-[500] rounded-md border border-stone-700 bg-stone-900 p-1.5 text-stone-300 shadow transition-[left] duration-200 ease-in-out hover:bg-stone-800",
          sidebarOpen ? "left-[336px]" : "left-4"
        )}
      >
        {sidebarOpen ? <PanelLeftClose className="h-4 w-4" aria-hidden /> : <PanelLeftOpen className="h-4 w-4" aria-hidden />}
      </button>
      <div className="relative min-w-0 flex-1">
        {view3d ? (
          <ThreeDView layers={project?.layers} plan={plan?.features} season={season} storageKey={project?.id} />
        ) : (
          <MapView
            layers={project?.layers}
            plan={plan?.features}
            planIndex={planIndex}
            layersKey={project?.id}
            planId={plan?.plan_id}
            planRevision={planRevision}
            hiddenLayerTypes={hiddenLayerTypes}
            showPlan={showPlan}
            selectMode={selection.selectMode}
            selectedIds={selection.selectedIds}
            violationIds={editing.violationIds}
            editsLocked={editing.editBusy}
            onSelectionChange={selection.setSelection}
            onMoveItems={editing.handleMoveItems}
            onContextMenu={handleContextMenu}
            onViewChange={closeContextMenu}
            focusRequest={selection.focusRequest}
            sidebarOpen={sidebarOpen}
          />
        )}
        <QualityBadge value={qualityIndex} />
      </div>
      {contextMenu && (
        <ContextMenu
          x={contextMenu.x}
          y={contextMenu.y}
          title={hasSelection ? `Выделено: ${countLabel(selection.selectedIds.size, OBJECT_FORMS)}` : "Ничего не выделено"}
          entries={contextMenuEntries}
          onClose={closeContextMenu}
        />
      )}
      <Toaster />
      <AssistantChat
        projectId={project?.id}
        generating={session.generating}
        settings={{ planting_types: plantingTypes, tree_spacing_m: treeSpacingM, shrub_spacing_m: shrubSpacingM }}
        onApplyAction={handleAssistantAction}
      />
    </main>
  );
}
