/** Shared color tables. Source-layer colors/grouping used to live here
 * (read by both the map and the panel legend) but that whole vocabulary
 * moved server-side to backend/app/services/layer_raster.py once the map
 * started rendering layers as rasters instead of GeoJSON features -- see
 * its docstring. `ZONE_COLORS` survives here only because ThreeDView.tsx's
 * building extrusion still needs a color and 3D was deliberately left on
 * the vector path (see CLAUDE.md's 3D section) -- keep it in sync with
 * layer_raster.py's own `_ZONE_COLORS` by hand if either changes. */

export const ZONE_COLORS: Record<string, string> = {
  building: "#78716c",
  road: "#57534e",
  territory: "#0ea5e9",
  // Teal, not tree-green -- kept in sync with layer_raster.py's own
  // _ZONE_COLORS.existing_greenery, see its comment for why (real existing
  // trees were camouflaged as more of the plan's own proposed-tree color).
  existing_greenery: "#0d9488",
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

/** 3D-only (ThreeDView.tsx) seasonal palettes -- purely cosmetic, swaps
 * material colors on a full scene rebuild (see ThreeDView.tsx's comment on
 * why season is a rebuild dependency, not a separate live-update path).
 * `summer` intentionally reuses PLANTING_COLORS so the 3D "default" season
 * still matches the 2D map's tree/shrub/lawn colors exactly. */
export type Season = "spring" | "summer" | "autumn" | "winter";

export const SEASON_LABELS: Record<Season, string> = { spring: "Весна", summer: "Лето", autumn: "Осень", winter: "Зима" };

export interface SeasonPalette {
  tree: string;
  shrub: string;
  lawn: string;
  skyTop: string;
  skyBottom: string;
}

export const SEASON_PALETTES: Record<Season, SeasonPalette> = {
  spring: { tree: "#4ade80", shrub: "#84cc16", lawn: "#bbf7d0", skyTop: "#7dd3fc", skyBottom: "#e0f2fe" },
  summer: { tree: PLANTING_COLORS.tree, shrub: PLANTING_COLORS.shrub, lawn: PLANTING_COLORS.lawn, skyTop: "#38bdf8", skyBottom: "#cbd5e1" },
  autumn: { tree: "#c2410c", shrub: "#a16207", lawn: "#d6d3a3", skyTop: "#94a3b8", skyBottom: "#fde68a" },
  winter: { tree: "#e5e7eb", shrub: "#d1d5db", lawn: "#f8fafc", skyTop: "#64748b", skyBottom: "#e2e8f0" },
};

/** The shape ControlPanel.tsx's legend renders one row from -- now filled by
 * layer_raster.py's own grouping (api.ts::LayersRasterGroup, a structural
 * superset of this with a `url` field) rather than built here from
 * GeoJSON features. Kept as a separate type so the panel doesn't couple its
 * prop to the raster-specific `url` field it never reads. */
export interface LayerLegendEntry {
  key: string;
  label: string;
  color: string;
  count: number;
}
