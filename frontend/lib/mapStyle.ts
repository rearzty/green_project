/** Shared source-layer color/label table -- one place both the map
 * (MapView.tsx) and the panel legend (ControlPanel.tsx) read from, so a
 * swatch in the legend always matches what's actually drawn on the map. */

import type { GeoJSONFeature, GeoJSONFeatureCollection } from "@/lib/api";

export const UTILITY_COLOR = "#b91c1c"; // red -- exclusion-driving constraints
export const ZONE_COLORS: Record<string, string> = {
  building: "#78716c",
  road: "#57534e",
  territory: "#0ea5e9",
  existing_greenery: "#16a34a",
};

/** Planting-item colors by planting_type -- used by both MapView.tsx's 2D
 * markers/lawn fills and plan3d.ts's 3D tree/shrub/lawn meshes, so a tree
 * looks the same color whichever view you're in. */
export const PLANTING_COLORS: Record<string, string> = {
  tree: "#15803d",
  shrub: "#65a30d",
  lawn: "#a3e635",
};

export function plantingColor(type: string): string {
  return PLANTING_COLORS[type] ?? "#22c55e";
}

export const LAYER_TYPE_LABELS: Record<string, string> = {
  territory: "Территория",
  building: "Здания",
  road: "Дороги",
  existing_greenery: "Существующая зелень",
  utility: "Инженерные сети",
};

// zoning is a soft scoring factor (ml_scoring's zoning_suitability), not an
// obstacle -- but its whole point is that different categories carry
// different suitability (planting_norms.yaml: recreational 1.0 down to
// transport 0.1), so painting every category the same purple defeated the
// purpose of showing it at all (a user couldn't tell "residential" from
// "industrial" on the map). Split into its own color/label per
// zoning_category instead of one shared "zoning" entry.
const ZONING_CATEGORY_COLORS: Record<string, string> = {
  residential: "#a78bfa",
  recreational: "#2dd4bf",
  public: "#818cf8",
  industrial: "#f97316",
  transport: "#64748b",
};
const ZONING_CATEGORY_LABELS: Record<string, string> = {
  residential: "Зонирование: жилая",
  recreational: "Зонирование: рекреационная",
  public: "Зонирование: общественная",
  industrial: "Зонирование: промышленная",
  transport: "Зонирование: транспортная",
};
const ZONING_KEY_PREFIX = "zoning:";

/** Every utility sub-type (heat/water/gas/cable/...) groups under one
 * "utility" key -- see CLAUDE.md, the user chose one shared legend entry
 * over splitting by network type. Zoning goes the opposite way: split by
 * `zoning_category` (the same attribute geo_engine/candidates.py::_zoning_at()
 * reads) instead of grouped under one "zoning" key. */
export function isZoningFeature(feature: GeoJSONFeature): boolean {
  return (feature.properties as Record<string, unknown>).object_type === "zoning";
}

export function layerGroupKey(feature: GeoJSONFeature): string {
  const props = feature.properties as Record<string, unknown>;
  if (props.kind === "utility") return "utility";
  if (isZoningFeature(feature)) return `${ZONING_KEY_PREFIX}${String(props.zoning_category ?? "unknown")}`;
  return String(props.object_type ?? "unknown");
}

export function layerGroupColor(key: string): string {
  if (key === "utility") return UTILITY_COLOR;
  if (key.startsWith(ZONING_KEY_PREFIX)) return ZONING_CATEGORY_COLORS[key.slice(ZONING_KEY_PREFIX.length)] ?? "#9ca3af";
  return ZONE_COLORS[key] ?? "#9ca3af";
}

export function layerGroupLabel(key: string): string {
  if (key.startsWith(ZONING_KEY_PREFIX)) {
    const category = key.slice(ZONING_KEY_PREFIX.length);
    return ZONING_CATEGORY_LABELS[category] ?? `Зонирование: ${category}`;
  }
  return LAYER_TYPE_LABELS[key] ?? key;
}

export interface LayerLegendEntry {
  key: string;
  label: string;
  color: string;
  count: number;
}

/** What's actually present in the loaded layers, not a fixed list -- a
 * project with no utilities/roads shouldn't show empty toggles for them. */
export function buildLayerLegend(layers: GeoJSONFeatureCollection | undefined): LayerLegendEntry[] {
  if (!layers) return [];
  const counts = new Map<string, number>();
  for (const feature of layers.features) {
    const key = layerGroupKey(feature);
    counts.set(key, (counts.get(key) ?? 0) + 1);
  }
  return [...counts.entries()]
    .map(([key, count]) => ({ key, label: layerGroupLabel(key), color: layerGroupColor(key), count }))
    .sort((a, b) => a.label.localeCompare(b.label, "ru"));
}
