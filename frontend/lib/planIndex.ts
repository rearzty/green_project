/** A per-plan lookup built once when a plan (re)loads: every item's type and
 * WGS84 bounding box by id. The selection tool answers "what's inside this
 * rectangle", "how big is the selection", "what types are selected" from
 * here instead of re-walking GeoJSON coordinates on every gesture. */

import type { GeoJSONFeatureCollection, PlantingType } from "@/lib/api";

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

function extendWithCoordinates(box: LatLngBox, coordinates: unknown) {
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

export function buildPlanIndex(collection: GeoJSONFeatureCollection): PlanIndex {
  const index: PlanIndex = new Map();
  for (const feature of collection.features) {
    const id = String(feature.properties.id);
    const box: LatLngBox = { minLat: Infinity, minLng: Infinity, maxLat: -Infinity, maxLng: -Infinity };
    extendWithCoordinates(box, feature.geometry.coordinates);
    if (!Number.isFinite(box.minLat)) continue;
    index.set(id, {
      id,
      type: feature.properties.planting_type as PlantingType,
      isPoint: feature.geometry.type === "Point",
      score: Number(feature.properties.score),
      ...box,
    });
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
