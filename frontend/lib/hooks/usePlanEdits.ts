"use client";

import { useEffect, useMemo, useRef, useState } from "react";

import {
  deleteItems,
  moveItems,
  restoreItems,
  retypeItems,
  validateItems,
  validatePlan,
  type LngLat,
  type PlanOut,
  type PlantingType,
  type ProjectOut,
  type ValidationViolation,
} from "@/lib/api";
import { countLabel, OBJECT_FORMS, PLANTING_TYPE_LABELS } from "@/lib/format";
import type { PlanIndex } from "@/lib/planIndex";
import { errorMessage, toast } from "@/lib/toast";
import { entryItemIds, pushCapped, type UndoEntry } from "@/lib/undoStack";

import type { LocalEditEffect } from "@/lib/hooks/useProjectSession";
import { useUndoRedo } from "@/lib/hooks/useUndoRedo";

interface EditContext {
  projectId: string;
  planId: string;
}

/** Merges a partial revalidation result (what validateItems returned for
 * `touchedIds`) into the existing violation list -- the setback check is
 * per-item and independent of every other planting, so an edit can only
 * ever change violation status for the items it actually touched; anything
 * else in `prev` is still correct and left alone. */
function mergeViolations(prev: ValidationViolation[] | null, touchedIds: string[], fresh: ValidationViolation[]): ValidationViolation[] {
  const touched = new Set(touchedIds);
  const kept = (prev ?? []).filter((v) => !touched.has(v.item_id));
  return [...kept, ...fresh];
}

/** Everything about editing the *currently open* plan: normative
 * validation, and the batch move/retype/delete actions plus their
 * undo/redo (see lib/hooks/useUndoRedo.ts). Deliberately one cohesive hook
 * rather than several tiny ones -- undo/redo replays exactly the same
 * mutation+validation path a fresh edit takes, so splitting them apart
 * would just mean passing the same handful of callbacks back and forth.
 *
 * Doesn't own selection state itself (see lib/hooks/useSelection.ts):
 * `handleDeleteSelection`/`handleRetypeSelection` take the ids to act on as
 * plain arguments instead of reading a selection hook's state directly, and
 * `setSelection` is the one thing this hook calls back into it with (to
 * clear a selection after deleting it, or restore it after an undo/redo).
 */
export function usePlanEdits(
  project: ProjectOut | null,
  plan: PlanOut | null,
  applyLocalEdit: (effect: LocalEditEffect) => void,
  bumpPlanRevision: () => void,
  patchPlanItemCount: (planId: string, itemCount: number) => void,
  setSelection: (ids: string[]) => void
) {
  const [violations, setViolations] = useState<ValidationViolation[] | null>(null);
  const [validating, setValidating] = useState(false);
  const [editBusy, setEditBusy] = useState(false);
  const editBusyRef = useRef(false);

  const planId = plan?.plan_id ?? null;
  const planIdRef = useRef(planId);
  planIdRef.current = planId;

  const { history, update: updateHistory, record: recordEdit, canUndo, canRedo } = useUndoRedo(planId);

  // `stepHistory` closes over `history`, which is a plain value snapshot
  // from *this* render -- fine for callers invoked synchronously (the undo
  // button, a keyboard shortcut), but the delete toast's "Отменить" button
  // stores its onClick in lib/toast.ts's own module-level store, outside
  // React entirely, and keeps whatever closure it was given no matter how
  // many renders happen before the user actually clicks it. Without this
  // ref indirection, that stored closure's `history` is stale the moment
  // the delete that created the toast pushes its own entry (a re-render
  // that happens *after* the toast is already built) -- the click would
  // silently look at a `history.undo` that doesn't have the new entry yet
  // and no-op. Always read through the ref instead of calling stepHistory
  // directly from anything that outlives a single render.
  const stepHistoryRef = useRef<(direction: "undo" | "redo", expected?: UndoEntry) => void>(() => {});

  const violationIds = useMemo<ReadonlySet<string>>(() => new Set((violations ?? []).map((v) => v.item_id)), [violations]);

  // --- validation ---

  const validateSeqRef = useRef(0);

  /** Full re-check -- runs whenever a *different* plan opens (nothing to
   * diff against yet); reactive on plan_id rather than called explicitly by
   * every place that can open a plan (restore/generate/switch), so none of
   * them have to remember to trigger it. */
  useEffect(() => {
    setViolations(null);
    if (!project || !plan) return;
    const seq = ++validateSeqRef.current;
    const forPlanId = plan.plan_id;
    setValidating(true);
    validatePlan(project.id, forPlanId)
      .then((result) => {
        if (seq === validateSeqRef.current && planIdRef.current === forPlanId) setViolations(result.violations);
      })
      .catch((e) => {
        if (seq === validateSeqRef.current) toast.error(`Не удалось проверить нормативы: ${errorMessage(e)}`);
      })
      .finally(() => {
        if (seq === validateSeqRef.current) setValidating(false);
      });
    // Only re-run for a genuinely different plan -- an in-place edit reuses
    // the same plan_id (see useProjectSession::applyLocalEdit), and that
    // path revalidates only the ids it touched (revalidateTouched below).
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [plan?.plan_id]);

  /** Rechecks only what an edit touched and merges the result -- avoids a
   * full plan's worth of checks after every single edit. Never throws: a
   * failed recheck shouldn't make the edit that triggered it look failed. */
  async function revalidateTouched(projectId: string, forPlanId: string, ids: string[]) {
    if (ids.length === 0) return;
    try {
      const { violations: fresh } = await validateItems(projectId, forPlanId, ids);
      if (planIdRef.current === forPlanId) setViolations((prev) => mergeViolations(prev, ids, fresh));
    } catch (e) {
      toast.error(`Не удалось проверить нормативы: ${errorMessage(e)}`);
    }
  }

  // --- edits ---

  /** Runs one edit at a time. On success the action itself applies its own
   * effect to local state (see LocalEditEffect) -- no plan refetch. On
   * failure, a toast reports it and the plan layer is simply remounted from
   * still-correct local state (planRevision bump): nothing was mutated
   * locally before success, so there's nothing to undo except whatever
   * Leaflet's own optimistic drag preview drew (MapView.tsx), which a
   * remount redraws from the real (unchanged) feature data. */
  async function runEdit(action: (ctx: EditContext) => Promise<void>) {
    if (!project || !plan || editBusyRef.current) return;
    const ctx: EditContext = { projectId: project.id, planId: plan.plan_id };
    editBusyRef.current = true;
    setEditBusy(true);
    try {
      await action(ctx);
    } catch (e) {
      toast.error(errorMessage(e));
      bumpPlanRevision();
    } finally {
      editBusyRef.current = false;
      setEditBusy(false);
    }
  }

  function handleMoveItems(ids: string[], from: LngLat, to: LngLat) {
    void runEdit(async ({ projectId, planId: forPlanId }) => {
      const items = await moveItems(projectId, forPlanId, ids, from, to);
      applyLocalEdit({ kind: "move", items });
      recordEdit(forPlanId, { type: "move_items", ids, from, to });
      await revalidateTouched(projectId, forPlanId, ids);
    });
  }

  function handleRetypeSelection(selectedIds: ReadonlySet<string>, planIndex: PlanIndex | undefined, type: "tree" | "shrub") {
    if (!planIndex || selectedIds.size === 0) return;
    const changes: { id: string; planting_type: PlantingType }[] = [];
    let areas = 0;
    selectedIds.forEach((id) => {
      const item = planIndex.get(id);
      if (!item) return;
      if (!item.isPoint) areas += 1;
      else if (item.type !== type) changes.push({ id, planting_type: type });
    });
    const label = PLANTING_TYPE_LABELS[type];
    if (areas > 0) toast.info(`Газон — площадной объект, в «${label}» его не превратить. Пропущено: ${countLabel(areas, OBJECT_FORMS)}.`);
    if (changes.length === 0) {
      if (areas === 0) toast.info(`Выделенные объекты уже «${label}».`);
      return;
    }
    void runEdit(async ({ projectId, planId: forPlanId }) => {
      const result = await retypeItems(projectId, forPlanId, changes);
      const changedIds = result.previous_items.map((f) => String(f.properties.id));
      if (changedIds.length === 0) return;
      applyLocalEdit({ kind: "retype", changes: changedIds.map((id) => ({ id, type })) });
      const previous = result.previous_items.map((f) => ({ id: String(f.properties.id), type: f.properties.planting_type as PlantingType }));
      recordEdit(forPlanId, { type: "retype_items", to: type, previous });
      await revalidateTouched(projectId, forPlanId, changedIds);
    });
  }

  function handleDeleteSelection(selectedIds: ReadonlySet<string>) {
    const ids = [...selectedIds];
    if (ids.length === 0) return;
    void runEdit(async ({ projectId, planId: forPlanId }) => {
      const result = await deleteItems(projectId, forPlanId, ids);
      applyLocalEdit({ kind: "delete", ids });
      patchPlanItemCount(forPlanId, result.item_count);
      const idSet = new Set(ids);
      setViolations((prev) => (prev ? prev.filter((v) => !idSet.has(v.item_id)) : prev));
      const entry: UndoEntry = { type: "delete_items", items: result.deleted_items };
      recordEdit(forPlanId, entry);
      setSelection([]);
      toast.success(`Удалено: ${countLabel(result.deleted_items.length, OBJECT_FORMS)}.`, {
        label: "Отменить",
        onClick: () => stepHistoryRef.current("undo", entry),
      });
    });
  }

  /** Applies one history entry: `undo` reverses it, `redo` re-applies it. */
  async function applyHistoryEntry(entry: UndoEntry, direction: "undo" | "redo", { projectId, planId: forPlanId }: EditContext) {
    switch (entry.type) {
      case "move_items": {
        const [from, to] = direction === "undo" ? [entry.to, entry.from] : [entry.from, entry.to];
        const items = await moveItems(projectId, forPlanId, entry.ids, from, to);
        applyLocalEdit({ kind: "move", items });
        await revalidateTouched(projectId, forPlanId, entry.ids);
        break;
      }
      case "retype_items": {
        const changes = entry.previous.map((p) => ({ id: p.id, planting_type: direction === "undo" ? p.type : entry.to }));
        await retypeItems(projectId, forPlanId, changes);
        applyLocalEdit({ kind: "retype", changes: changes.map((c) => ({ id: c.id, type: c.planting_type })) });
        await revalidateTouched(projectId, forPlanId, changes.map((c) => c.id));
        break;
      }
      case "delete_items": {
        if (direction === "undo") {
          const result = await restoreItems(projectId, forPlanId, entry.items);
          applyLocalEdit({ kind: "restore", items: entry.items });
          patchPlanItemCount(forPlanId, result.item_count);
          await revalidateTouched(projectId, forPlanId, entryItemIds(entry));
        } else {
          const ids = entryItemIds(entry);
          const result = await deleteItems(projectId, forPlanId, ids);
          applyLocalEdit({ kind: "delete", ids });
          patchPlanItemCount(forPlanId, result.item_count);
          const idSet = new Set(ids);
          setViolations((prev) => (prev ? prev.filter((v) => !idSet.has(v.item_id)) : prev));
        }
        break;
      }
    }
  }

  function stepHistory(direction: "undo" | "redo", expected?: UndoEntry) {
    const stack = direction === "undo" ? history.undo : history.redo;
    const entry = stack[stack.length - 1];
    if (!entry) return;
    if (expected && entry !== expected) {
      toast.info("После этого были другие правки — отменяйте по шагам через Ctrl+Z.");
      return;
    }
    void runEdit(async (ctx) => {
      const pop = (s: UndoEntry[]) => (s[s.length - 1] === entry ? s.slice(0, -1) : s);
      updateHistory(ctx.planId, (h) => (direction === "undo" ? { undo: pop(h.undo), redo: h.redo } : { undo: h.undo, redo: pop(h.redo) }));
      try {
        await applyHistoryEntry(entry, direction, ctx);
      } catch (e) {
        // The entry is already off the stack -- it no longer applies to the
        // plan as it is now, so keeping it would just fail the same way again.
        throw new Error(`${direction === "undo" ? "Отменить" : "Повторить"} не получилось: ${errorMessage(e)} Эта правка убрана из истории.`);
      }
      updateHistory(ctx.planId, (h) =>
        direction === "undo" ? { undo: h.undo, redo: pushCapped(h.redo, entry) } : { undo: pushCapped(h.undo, entry), redo: h.redo }
      );
      const itemsGone = entry.type === "delete_items" && direction === "redo";
      setSelection(itemsGone ? [] : entryItemIds(entry));
    });
  }
  stepHistoryRef.current = stepHistory;

  return {
    violations,
    violationIds,
    validating,
    editBusy,
    canUndo,
    canRedo,
    handleMoveItems,
    handleRetypeSelection,
    handleDeleteSelection,
    undo: (expected?: UndoEntry) => stepHistory("undo", expected),
    redo: () => stepHistory("redo"),
  };
}
