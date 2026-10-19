/** Shared color tables. Source-layer colors/grouping used to live here
 * (read by both the map and the panel legend) but that whole vocabulary
 * moved server-side to backend/app/services/layer_raster.py once the map
 * started rendering layers as rasters instead of GeoJSON features -- see
 * its docstring. The 3D view no longer borrows the map's zone colors either:
 * ground/road/greenery there are per-season surface colors in
 * `SEASON_PALETTES` below. */

/** Planting-item colors by planting_type -- used by both MapView.tsx's 2D
 * markers/lawn fills and plan3d.ts's 3D tree/shrub/lawn meshes, so a tree
 * looks the same color whichever view you're in. */
export const PLANTING_COLORS: Record<string, string> = {
  tree: "#7BD389",
  shrub: "#C9DE6A",
  // Основа газона rgb(140,200,110): заливка 16%, обводка 45% — MapView.tsx::areaStyle.
  lawn: "#8CC86E",
};

export function plantingColor(type: string): string {
  return PLANTING_COLORS[type] ?? "#7BD389";
}

/** Нарушение норматива и выделение на карте: те же цвета, что у токенов
 * gp-violation / gp-selection (tailwind.config.ts), — для мест, где Tailwind-
 * класс недоступен (стили Leaflet, inline-стили иконок). */
export const VIOLATION_COLOR = "#FF9351";
export const SELECTION_COLOR = "#8AB4FF";
/** Цвет фона карты (gp-map): обводка точек «отрывает» их от соседей. */
export const MAP_BG_COLOR = "#2A302C";

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
  /** Земля участка, покрытие дорог, существующая зелень и «пол» вокруг участка в
   * 3D — раньше брались из ZONE_COLORS карты (участок — ярко-синим, как его
   * граница на 2D), и земля в 3D выходила синей. Здесь — реальные цвета
   * поверхностей, зимой — под снегом. */
  ground: string;
  road: string;
  greenery: string;
  pad: string;
}

export const SEASON_PALETTES: Record<Season, SeasonPalette> = {
  spring: {
    tree: "#4ade80", shrub: "#84cc16", lawn: "#93cf80", skyTop: "#7cc4ec", skyBottom: "#e6f2f8",
    ground: "#8d9282", road: "#4a4d4b", greenery: "#6fa267", pad: "#a9ae9a",
  },
  summer: {
    tree: PLANTING_COLORS.tree, shrub: PLANTING_COLORS.shrub, lawn: "#6fa653", skyTop: "#5fb0e4", skyBottom: "#dbe7f0",
    ground: "#8b8d84", road: "#464947", greenery: "#4f8a52", pad: "#a7ad9d",
  },
  autumn: {
    tree: "#c2410c", shrub: "#a16207", lawn: "#b2ae6e", skyTop: "#8fa3b8", skyBottom: "#efdfc2",
    ground: "#8a8478", road: "#474846", greenery: "#9a8a45", pad: "#b1aa9b",
  },
  winter: {
    tree: "#e5e7eb", shrub: "#d1d5db", lawn: "#f8fafc", skyTop: "#8b9bb0", skyBottom: "#e6ecf2",
    ground: "#c9ced3", road: "#7c8086", greenery: "#cfd6d4", pad: "#e3e8ed",
  },
};

/** Оформление сцены 3D: «День» — светлое небо и поверхности по сезону (как было),
 * «Графит» — тёмная сцена под графитовый интерфейс. Сезон при этом задаёт то
 * же, что и раньше — цвета растений и газона, — а тема лишь пересаживает сцену
 * на тёмный фон: небо, землю, дороги и пол. */
export type SceneTheme = "day" | "graphite";

export const SCENE_THEME_LABELS: Record<SceneTheme, string> = { day: "День", graphite: "Графит" };

function channel(hex: string, from: number): number {
  return parseInt(hex.slice(from, from + 2), 16);
}

/** Смесь двух #rrggbb: 0 — первый цвет, 1 — второй. */
export function mixHex(a: string, b: string, t: number): string {
  const mixed = [1, 3, 5].map((from) => Math.round(channel(a, from) + (channel(b, from) - channel(a, from)) * t));
  return `#${mixed.map((v) => v.toString(16).padStart(2, "0")).join("")}`;
}

const GRAPHITE = { skyTop: "#141816", skyBottom: "#2a302c", ground: "#444b45", road: "#262a28", pad: "#2a302c", dim: "#1e2220" };

/** Палитра сезона в выбранной теме. Растения не трогаем (их цвет — из
 * lib/plantLook.ts), а газон и зелень чуть приглушаем: на тёмной земле они
 * иначе светятся ярче самих деревьев. */
export function applySceneTheme(palette: SeasonPalette, theme: SceneTheme): SeasonPalette {
  if (theme === "day") return palette;
  return {
    ...palette,
    skyTop: GRAPHITE.skyTop,
    skyBottom: GRAPHITE.skyBottom,
    ground: GRAPHITE.ground,
    road: GRAPHITE.road,
    pad: GRAPHITE.pad,
    lawn: mixHex(palette.lawn, GRAPHITE.dim, 0.3),
    greenery: mixHex(palette.greenery, GRAPHITE.dim, 0.35),
  };
}

/** The shape the "Слои" popover (components/LayersPopover.tsx) renders one row from -- now filled by
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
