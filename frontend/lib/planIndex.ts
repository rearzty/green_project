/** A per-plan lookup built once when a plan (re)loads: every item's type and
 * WGS84 bounding box by id. The selection tool answers "what's inside this
 * rectangle", "how big is the selection", "what types are selected" from
 * here instead of re-walking GeoJSON coordinates on every gesture. */

import type { GeoJSONFeature, GeoJSONFeatureCollection, PlantingType } from "@/lib/api";
import type { LocalEditEffect } from "@/lib/hooks/useProjectSession";

export interface PlanItemInfo {
  id: string;
  type: PlantingType;
  /** tree/shrub are points, lawn is an area -- see the planting_type <-> geometry contract in CLAUDE.md. */
  isPoint: boolean;
  score: number;
  minLat: number;
  minLng: number;
  maxLat: number;
  maxLng: number;
}

export type PlanIndex = Map<string, PlanItemInfo>;

export interface LatLngBox {
  minLat: number;
  minLng: number;
  maxLat: number;
  maxLng: number;
}

/** Recursively widens `box` to cover every [lng, lat] pair in a GeoJSON
 * coordinates array, whatever its nesting depth (Point/LineString/Polygon/
 * Multi*) -- exported for reuse by anything that needs a feature's bounding
 * box, not just plan items (see MapView.tsx's VirtualizedLayers). */
export function extendWithCoordinates(box: LatLngBox, coordinates: unknown) {
  if (!Array.isArray(coordinates) || coordinates.length === 0) return;
  if (typeof coordinates[0] === "number") {
    const [lng, lat] = coordinates as number[];
    box.minLat = Math.min(box.minLat, lat);
    box.maxLat = Math.max(box.maxLat, lat);
    box.minLng = Math.min(box.minLng, lng);
    box.maxLng = Math.max(box.maxLng, lng);
    return;
  }
  for (const nested of coordinates) extendWithCoordinates(box, nested);
}

function planItemInfo(feature: GeoJSONFeature): PlanItemInfo | undefined {
  const id = String(feature.properties.id);
  const box: LatLngBox = { minLat: Infinity, minLng: Infinity, maxLat: -Infinity, maxLng: -Infinity };
  extendWithCoordinates(box, feature.geometry.coordinates);
  if (!Number.isFinite(box.minLat)) return undefined;
  return {
    id,
    type: feature.properties.planting_type as PlantingType,
    isPoint: feature.geometry.type === "Point",
    score: Number(feature.properties.score),
    ...box,
  };
}

export function buildPlanIndex(collection: GeoJSONFeatureCollection): PlanIndex {
  const index: PlanIndex = new Map();
  for (const feature of collection.features) {
    const info = planItemInfo(feature);
    if (info) index.set(info.id, info);
  }
  return index;
}

/** Updates only the ids an edit actually touched, in place, instead of
 * rebuilding the whole index -- on a real-scale plan (hundreds of thousands
 * of items) a full rebuild on every single move/retype/delete measured at
 * up to ~1.6s (see CLAUDE.md's edit-cost benchmark), almost all of it this
 * function; patched, the same edit costs about as much as the edit itself.
 * Mutates and returns the same Map -- callers that need to know an index
 * *changed* watch planRevision instead of this reference (see
 * useProjectSession.ts::applyLocalEdit). */
export function patchPlanIndex(index: PlanIndex, effect: LocalEditEffect): PlanIndex {
  switch (effect.kind) {
    case "move":
    case "restore":
      for (const feature of effect.items) {
        const info = planItemInfo(feature);
        if (info) index.set(info.id, info);
      }
      break;
    case "retype":
      for (const { id, type } of effect.changes) {
        const existing = index.get(id);
        if (existing) index.set(id, { ...existing, type });
      }
      break;
    case "delete":
      for (const id of effect.ids) index.delete(id);
      break;
  }
  return index;
}

/** Points inside the box; area items only when they fit entirely inside it
 * (grazing the edge of a big lawn while boxing a few trees shouldn't grab the lawn). */
export function itemsInBox(index: PlanIndex, box: LatLngBox): string[] {
  const ids: string[] = [];
  index.forEach((item) => {
    if (item.minLat >= box.minLat && item.maxLat <= box.maxLat && item.minLng >= box.minLng && item.maxLng <= box.maxLng) {
      ids.push(item.id);
    }
  });
  return ids;
}

export function boundsOfItems(index: PlanIndex, ids: Iterable<string>): LatLngBox | null {
  const box: LatLngBox = { minLat: Infinity, minLng: Infinity, maxLat: -Infinity, maxLng: -Infinity };
  for (const id of ids) {
    const item = index.get(id);
    if (!item) continue;
    box.minLat = Math.min(box.minLat, item.minLat);
    box.maxLat = Math.max(box.maxLat, item.maxLat);
    box.minLng = Math.min(box.minLng, item.minLng);
    box.maxLng = Math.max(box.maxLng, item.maxLng);
  }
  return Number.isFinite(box.minLat) ? box : null;
}
