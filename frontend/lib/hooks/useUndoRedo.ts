"use client";

import { useEffect, useState } from "react";

import { EMPTY_HISTORY, loadUndoHistory, pushCapped, saveUndoHistory, type UndoEntry, type UndoHistory } from "@/lib/undoStack";

/** Keeps the plan id alongside the stacks in one state value -- a separate
 * save effect keyed only on `planId` could otherwise write the *old* plan's
 * stack under the *new* plan's storage key during the render where they
 * cross over. */
type PlanHistory = UndoHistory & { planId: string | null };
const NO_HISTORY: PlanHistory = { planId: null, ...EMPTY_HISTORY };

/** One plan's undo/redo stack -- loads/saves it to sessionStorage keyed by
 * plan id (see lib/undoStack.ts) and resets to empty whenever `planId`
 * changes. Doesn't know how to *apply* an entry (that's edit-domain logic,
 * different for every edit kind, see usePlanEdits) -- callers drive that
 * themselves and use `update`/`record` to keep the stacks in sync with
 * whatever actually happened.
 */
export function useUndoRedo(planId: string | null) {
  const [history, setHistory] = useState<PlanHistory>(NO_HISTORY);

  useEffect(() => {
    setHistory(planId ? { planId, ...loadUndoHistory(planId) } : NO_HISTORY);
  }, [planId]);

  useEffect(() => {
    if (history.planId) saveUndoHistory(history.planId, { undo: history.undo, redo: history.redo });
  }, [history]);

  /** Applies `updater` only if `forPlanId` still matches the currently
   * loaded plan -- guards against a stray async continuation (e.g. an
   * in-flight undo) landing after the user has already switched plans. */
  function update(forPlanId: string, updater: (h: UndoHistory) => UndoHistory) {
    setHistory((h) => (h.planId === forPlanId ? { planId: forPlanId, ...updater(h) } : h));
  }

  function record(forPlanId: string, entry: UndoEntry) {
    update(forPlanId, (h) => ({ undo: pushCapped(h.undo, entry), redo: [] }));
  }

  return { history, update, record, canUndo: history.undo.length > 0, canRedo: history.redo.length > 0 };
}
