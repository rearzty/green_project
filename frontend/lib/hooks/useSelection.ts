"use client";

import { useCallback, useEffect, useMemo, useState } from "react";

import type { PlantingType } from "@/lib/api";
import type { PlanIndex } from "@/lib/planIndex";

const NO_SELECTION: ReadonlySet<string> = new Set();

export interface SelectionSummary {
  total: number;
  counts: Record<PlantingType, number>;
  /** Details of the one selected item, when exactly one is selected. */
  single?: { type: PlantingType; score: number; violation: boolean };
}

/** Selection state for the map's selection tool: which item ids are
 * selected, whether selection mode is on, and the derived summary the
 * panel/context menu show. Doesn't know about edits -- callers act on the
 * ids this hook hands back themselves (see usePlanEdits) and report the
 * outcome back in via `setSelection`/`clear`.
 */
export function useSelection(planIndex: PlanIndex | undefined, planRevision: number, violationIds: ReadonlySet<string>) {
  const [selectMode, setSelectMode] = useState(false);
  const [selectedIds, setSelectedIds] = useState<ReadonlySet<string>>(NO_SELECTION);
  const [focusRequest, setFocusRequest] = useState<{ id: string; seq: number } | null>(null);

  // Forget selected ids that no longer exist after a plan switch (a brand
  // new planIndex object -- see useProjectSession.ts::applyPlan). A local
  // edit's deletions don't retrigger this: planIndex is patched in place
  // for those (same reference), and usePlanEdits.ts already sets selection
  // explicitly at every call site that can make an id disappear. Also drop
  // select mode itself once there's no plan to select from -- otherwise it
  // got stuck on with no plan around to turn it off against (e.g. Clear All
  // while select mode was active).
  useEffect(() => {
    if (!planIndex) {
      setSelectedIds((prev) => (prev.size === 0 ? prev : NO_SELECTION));
      setSelectMode(false);
      return;
    }
    setSelectedIds((prev) => {
      const kept = [...prev].filter((id) => planIndex.has(id));
      return kept.length === prev.size ? prev : new Set(kept);
    });
  }, [planIndex]);

  const setSelection = useCallback((ids: string[]) => {
    setSelectedIds((prev) => (ids.length === 0 && prev.size === 0 ? prev : new Set(ids)));
  }, []);

  const clear = useCallback(() => setSelectedIds(NO_SELECTION), []);

  function selectAll() {
    if (planIndex) setSelectedIds(new Set(planIndex.keys()));
  }

  function focusItem(itemId: string) {
    setSelectedIds(new Set([itemId]));
    setFocusRequest((prev) => ({ id: itemId, seq: (prev?.seq ?? 0) + 1 }));
  }

  const summary = useMemo<SelectionSummary | null>(() => {
    if (!planIndex || selectedIds.size === 0) return null;
    const counts: Record<PlantingType, number> = { tree: 0, shrub: 0, lawn: 0 };
    let total = 0;
    selectedIds.forEach((id) => {
      const item = planIndex.get(id);
      if (!item) return;
      counts[item.type] = (counts[item.type] ?? 0) + 1;
      total += 1;
    });
    if (total === 0) return null;
    const onlyId = total === 1 ? [...selectedIds].find((id) => planIndex.has(id)) : undefined;
    const only = onlyId ? planIndex.get(onlyId) : undefined;
    return { total, counts, single: only && onlyId ? { type: only.type, score: only.score, violation: violationIds.has(onlyId) } : undefined };
    // planRevision: planIndex is patched in place on a local edit (see
    // useProjectSession.ts), so a retype changes an entry's `type` without
    // changing planIndex's own reference -- this needs the extra signal to
    // notice.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planIndex, planRevision, selectedIds, violationIds]);

  return { selectMode, setSelectMode, selectedIds, setSelection, selectAll, clear, focusItem, focusRequest, summary };
}
