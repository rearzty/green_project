"use client";

import dynamic from "next/dynamic";
import { useCallback, useEffect, useMemo, useState } from "react";
import { CheckCheck, Redo2, Shrub, SquareDashedMousePointer, Trash2, TreeDeciduous, Undo2, X } from "lucide-react";

import { ContextMenu, type ContextMenuEntry } from "@/components/ContextMenu";
import { ControlPanel } from "@/components/ControlPanel";
import { Toaster } from "@/components/Toaster";
import { useKeyboardShortcuts } from "@/lib/hooks/useKeyboardShortcuts";
import { usePlanEdits } from "@/lib/hooks/usePlanEdits";
import { useProjectSession } from "@/lib/hooks/useProjectSession";
import { useSelection } from "@/lib/hooks/useSelection";
import { getPlantingNorms, type PlantingNorms } from "@/lib/api";
import { countLabel, OBJECT_FORMS } from "@/lib/format";
import { buildLayerLegend } from "@/lib/mapStyle";
import { toast } from "@/lib/toast";

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
  const [contextMenu, setContextMenu] = useState<{ x: number; y: number } | null>(null);
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
      <ControlPanel
        hasProject={project !== null}
        hasPlan={plan !== null}
        restoring={session.restoring}
        generating={session.generating}
        exporting={session.exporting}
        onUpload={session.handleUpload}
        onGenerate={session.handleGenerate}
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
        plans={plans}
        currentPlanId={plan?.plan_id}
        onSelectPlan={session.handleSelectPlan}
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
      <div className="relative min-w-0 flex-1">
        {view3d ? (
          <ThreeDView layers={project?.layers} plan={plan?.features} />
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
          />
        )}
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
    </main>
  );
}
