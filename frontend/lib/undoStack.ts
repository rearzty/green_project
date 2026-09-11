/** Local undo/redo for manual plan edits -- diffs, not full-plan snapshots
 * (a real-scale plan can hold hundreds of thousands of items; keeping 5
 * full copies of that around just for undo would be the wrong tradeoff).
 *
 * Every entry names the exact items it touched by id. An earlier version
 * replayed area-based operations (e.g. "move everything inside this
 * rectangle"), and undoing a move re-used the original rectangle -- but the
 * items had already left it, so the inverse picked up the wrong items (or
 * none) and could push them outside the territory. Ids don't drift.
 *
 * Capped at 5 entries per direction and persisted to sessionStorage per plan
 * (survives a page refresh, not closing the tab).
 */

import type { GeoJSONFeature, LngLat, PlantingType } from "@/lib/api";

export const UNDO_STACK_LIMIT = 5;

export type UndoEntry =
  // Inverse: the same ids moved by the reverse vector (to -> from).
  | { type: "move_items"; ids: string[]; from: LngLat; to: LngLat }
  // Inverse: each item back to its own previous type.
  | { type: "retype_items"; to: PlantingType; previous: { id: string; type: PlantingType }[] }
  // Inverse: restore these exact pre-delete snapshots.
  | { type: "delete_items"; items: GeoJSONFeature[] };

export interface UndoHistory {
  undo: UndoEntry[];
  redo: UndoEntry[];
}

export const EMPTY_HISTORY: UndoHistory = { undo: [], redo: [] };

const KEY_PREFIX = "greenproject:undo:";

export function loadUndoHistory(planId: string): UndoHistory {
  try {
    const raw = sessionStorage.getItem(KEY_PREFIX + planId);
    if (!raw) return EMPTY_HISTORY;
    const parsed = JSON.parse(raw) as Partial<UndoHistory>;
    const valid = (entries: unknown) =>
      Array.isArray(entries) ? (entries as UndoEntry[]).filter((e) => ["move_items", "retype_items", "delete_items"].includes(e?.type)) : [];
    // Silently drops entries from the old area-based format, if a tab still has them.
    return { undo: valid(parsed.undo), redo: valid(parsed.redo) };
  } catch {
    return EMPTY_HISTORY; // private-mode/storage-disabled browsers, or corrupt JSON -- just start fresh
  }
}

/** Best effort: a very large delete's snapshots can exceed sessionStorage's
 * ~5 MB quota -- the history then just lives in memory for this page load
 * (still fully usable, only not refresh-proof). */
export function saveUndoHistory(planId: string, history: UndoHistory): void {
  try {
    if (history.undo.length === 0 && history.redo.length === 0) sessionStorage.removeItem(KEY_PREFIX + planId);
    else sessionStorage.setItem(KEY_PREFIX + planId, JSON.stringify(history));
  } catch {
    try {
      sessionStorage.removeItem(KEY_PREFIX + planId); // don't leave an older, now-wrong copy behind
    } catch {
      // storage unavailable altogether
    }
  }
}

export function clearAllUndoHistory(): void {
  try {
    Object.keys(sessionStorage)
      .filter((key) => key.startsWith(KEY_PREFIX))
      .forEach((key) => sessionStorage.removeItem(key));
  } catch {
    // ignore
  }
}

/** Push onto a capped stack, dropping the oldest entry once full. */
export function pushCapped<T>(stack: T[], entry: T, limit: number = UNDO_STACK_LIMIT): T[] {
  const next = [...stack, entry];
  return next.length > limit ? next.slice(next.length - limit) : next;
}

/** Ids an entry touched -- used to re-select them after undo/redo so the
 * user sees what just changed. */
export function entryItemIds(entry: UndoEntry): string[] {
  switch (entry.type) {
    case "move_items":
      return entry.ids;
    case "retype_items":
      return entry.previous.map((p) => p.id);
    case "delete_items":
      return entry.items.map((item) => String(item.properties.id));
  }
}
