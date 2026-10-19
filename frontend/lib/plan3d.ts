/** Pure geometry prep for the 3D plan viewer (components/ThreeDView.tsx) --
 * turns the same GeoJSON the 2D map already renders (project.layers +
 * plan.features) into plain numeric structures a renderer can turn into
 * meshes. No `THREE.*` types here on purpose: this module doesn't know or
 * care which 3D engine draws the result, same separation as mapStyle.ts
 * (color/label logic) staying independent of react-leaflet.
 *
 * Coordinates are local flat meters around the territory's own centroid, not
 * WGS84 degrees -- `x` is eastward, `y` is northward. A full geodetic
 * projection (proj4js) would be overkill for a single human-scale plot; the
 * small-area equirectangular approximation in `makeProjector` is accurate to
 * well under a meter over a ~1.5km site, which is what everything here is
 * built at (see CLAUDE.md's real-scale numbers).
 */

import type { GeoJSONFeatureCollection, GeoJSONGeometry, PlantingNorms } from "@/lib/api";

const METERS_PER_DEGREE_LAT = 111_320;

/** Above this many trees+shrubs+lawn polygons, ThreeDView shows a message
 * instead of building the scene. Generous compared to the 2D map's own
 * per-viewport-cell cluster cap (MapView.tsx::CLUSTER_CELL_PX -- clustering
 * bounds the *rendered* marker count, not how much of the plan is loaded) --
 * GPU instancing (see ThreeDView.tsx) doesn't pay a per-object DOM cost, so
 * it comfortably handles far more; this is a safety valve for pathological
 * input, not a realistic ceiling (CLAUDE.md's real-scale benchmarks go up to
 * ~1M items). */
export const MAX_3D_ITEMS = 300_000;

/** Сколько окружения оставляем вокруг участка, м. Топоплан снимает весь квартал, а
 * не только участок работ: за границей лежат тысячи кроп деревьев, чужие дворы и
 * дороги. На 3D они выглядели как россыпь точек вдали от карты, а ещё раздували
 * рамку сцены и отодвигали камеру. Полоса оставляет соседние дома и дорогу рядом
 * (контекст), а дальнее убирает. */
export const SURROUNDINGS_BAND_M = 40;
const SURROUNDINGS_CELL_M = 8;
const SURROUNDINGS_MAX_CELLS_PER_AXIS = 2000;
const SURROUNDINGS_SAMPLES_PER_POLYGON = 24;
const SURROUNDINGS_MAX_EDGE_STEPS = 400;

/** Default tree_default/shrub_default.canopy_radius_m from
 * geo_engine/config/planting_norms.yaml, used only if the /api/config
 * fetch hasn't resolved yet or a species_spacing key is missing. */
const DEFAULT_TREE_CANOPY_RADIUS_M = 2.5;
const DEFAULT_SHRUB_CANOPY_RADIUS_M = 1.5;

/** Placeholder building height range -- neither data.mos.ru (id 60562) nor
 * OSM building tags around the real courtyard samples reliably carry
 * height/floor counts (see data/README.md), so there is no real value to
 * read. ~3-8 floors at 3m/floor, picked per-building by a deterministic hash
 * of its footprint so the same plan always renders the same way, not
 * because it means anything real -- same spirit as planting_norms.yaml's
 * own draft values (CLAUDE.md's "Плейсхолдеры" section). */
const BUILDING_MIN_HEIGHT_M = 9;
const BUILDING_MAX_HEIGHT_M = 24;

type Ring = [number, number][]; // raw GeoJSON [lon, lat] pairs, unprojected

export interface Projector {
  project(lon: number, lat: number): [number, number];
}

export function makeProjector(refLon: number, refLat: number): Projector {
  const metersPerDegreeLon = METERS_PER_DEGREE_LAT * Math.cos((refLat * Math.PI) / 180);
  return {
    project(lon, lat) {
      return [(lon - refLon) * metersPerDegreeLon, (lat - refLat) * METERS_PER_DEGREE_LAT];
    },
  };
}

/** Counterpart to makeProjector for an unverified project's CRS (see
 * backend's geo_io.py::display_crs) -- there, `layers`/`plan`'s "lon/lat"
 * are never real WGS84 degrees to begin with, just the drawing's own raw
 * local metres passed straight through. Running those through the
 * equirectangular approximation above would multiply them by a bogus
 * cos(latitude) factor derived from numbers that were never a latitude --
 * this just recenters on the reference point without any unit conversion,
 * since they're already metres. */
export function makeLocalProjector(refX: number, refY: number): Projector {
  return {
    project(x, y) {
      return [x - refX, y - refY];
    },
  };
}

/** A polygon's outer ring plus any holes, in local meters. Mirrors
 * THREE.Shape's own outer/holes split so the renderer can build one
 * directly without re-deriving it. */
export interface Polygon3D {
  outer: [number, number][];
  holes: [number, number][][];
}

export interface Building3D extends Polygon3D {
  height: number;
  /** Same deterministic centroid hash `height` derives from, in [0, 1) --
   * exposed separately so ThreeDView.tsx's window-texture tint can reuse it
   * instead of hashing the footprint a second time with a different seed. */
  hash01: number;
}

export interface PlantingPoint3D {
  x: number;
  y: number;
  /** Радиус кроны, м: из диаметра кроны породы (`crown_m` посадки), а если его
   * нет — норматив типа посадки (planting_norms.yaml). */
  canopyRadius: number;
  /** Порода из плана — по ней ThreeDView подбирает форму и цвет (lib/plantLook.ts). */
  species: string | null;
  /** Класс кроны дерева (wide/medium/narrow); у куста null. */
  crownClass: string | null;
}

export interface Scene3DBounds {
  minX: number;
  maxX: number;
  minY: number;
  maxY: number;
  maxBuildingHeight: number;
}

export interface Scene3DData {
  territory: Polygon3D[];
  buildings: Building3D[];
  roads: Polygon3D[];
  existingGreenery: Polygon3D[];
  lawns: Polygon3D[];
  trees: PlantingPoint3D[];
  shrubs: PlantingPoint3D[];
  totalItemCount: number;
  bounds: Scene3DBounds | null;
}

/** Проверка «лежит ли объект не дальше `bandM` от участка» — для окружения в 3D.
 * Участок кладётся на грубую сетку (клетки по 8 м: сама граница отрезками и
 * внутренность построчной заливкой), сетка расширяется кругом радиуса `bandM`, и
 * объект остаётся, если хоть одна из его вершин (до 24, равномерно) попала в
 * помеченную клетку. Точное расстояние до многоугольника здесь не нужно: полоса —
 * вопрос вкуса, а проход по каждой из сотен тысяч вершин окружения точной
 * геометрией был бы заметно дороже самой сборки сцены. Участка нет — остаётся всё. */
export function nearTerritoryFilter(territory: Polygon3D[], bandM: number = SURROUNDINGS_BAND_M): (polygon: Polygon3D) => boolean {
  const rings = territory.flatMap((p) => [p.outer, ...p.holes]).filter((r) => r.length >= 3);
  if (rings.length === 0) return () => true;
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  for (const ring of rings) {
    for (const [x, y] of ring) {
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
    }
  }
  // Клетка растёт, если участок огромен, — сетка не должна превращаться в гигабайт.
  const cell = Math.max(SURROUNDINGS_CELL_M, (maxX - minX + 2 * bandM) / SURROUNDINGS_MAX_CELLS_PER_AXIS, (maxY - minY + 2 * bandM) / SURROUNDINGS_MAX_CELLS_PER_AXIS);
  const x0 = minX - bandM - cell;
  const y0 = minY - bandM - cell;
  const cols = Math.ceil((maxX - minX + 2 * bandM) / cell) + 3;
  const rowsN = Math.ceil((maxY - minY + 2 * bandM) / cell) + 3;
  const solid = new Uint8Array(cols * rowsN);
  const col = (x: number) => Math.floor((x - x0) / cell);
  const row = (y: number) => Math.floor((y - y0) / cell);

  // Граница: клетки вдоль каждого ребра.
  for (const ring of rings) {
    for (let i = 0; i < ring.length; i++) {
      const [ax, ay] = ring[i];
      const [bx, by] = ring[(i + 1) % ring.length];
      const steps = Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / (cell / 2)));
      for (let k = 0; k <= steps; k++) {
        const c = col(ax + ((bx - ax) * k) / steps);
        const r = row(ay + ((by - ay) * k) / steps);
        if (c >= 0 && c < cols && r >= 0 && r < rowsN) solid[r * cols + c] = 1;
      }
    }
  }
  // Внутренность: построчно, чётно-нечётное правило по всем кольцам сразу (дыры вычитаются сами).
  for (let r = 0; r < rowsN; r++) {
    const y = y0 + (r + 0.5) * cell;
    const crossings: number[] = [];
    for (const ring of rings) {
      for (let i = 0; i < ring.length; i++) {
        const [ax, ay] = ring[i];
        const [bx, by] = ring[(i + 1) % ring.length];
        if (ay > y !== by > y) crossings.push(ax + ((y - ay) * (bx - ax)) / (by - ay));
      }
    }
    crossings.sort((a, b) => a - b);
    for (let k = 0; k + 1 < crossings.length; k += 2) {
      const from = Math.max(0, col(crossings[k]));
      const to = Math.min(cols - 1, col(crossings[k + 1]));
      for (let c = from; c <= to; c++) solid[r * cols + c] = 1;
    }
  }
  // Полоса: круг радиуса bandM вокруг каждой занятой клетки.
  const reach = Math.ceil(bandM / cell);
  const near = new Uint8Array(cols * rowsN);
  for (let r = 0; r < rowsN; r++) {
    for (let c = 0; c < cols; c++) {
      if (!solid[r * cols + c]) continue;
      for (let dr = -reach; dr <= reach; dr++) {
        const rr = r + dr;
        if (rr < 0 || rr >= rowsN) continue;
        for (let dc = -reach; dc <= reach; dc++) {
          const cc = c + dc;
          if (cc >= 0 && cc < cols && Math.hypot(dr, dc) * cell <= bandM + cell) near[rr * cols + cc] = 1;
        }
      }
    }
  }
  const isNear = (x: number, y: number) => {
    const c = col(x);
    const r = row(y);
    return c >= 0 && c < cols && r >= 0 && r < rowsN && near[r * cols + c] === 1;
  };
  return (polygon) => {
    const ring = polygon.outer;
    if (ring.length === 0) return false;
    // Не только вершины: длинная прямая дорога может состоять из четырёх вершин
    // далеко по краям и при этом пересекать участок посередине — точки берутся и
    // вдоль рёбер, с шагом в клетку.
    const stride = Math.max(1, Math.floor(ring.length / SURROUNDINGS_SAMPLES_PER_POLYGON));
    for (let i = 0; i < ring.length; i += stride) {
      const [ax, ay] = ring[i];
      const [bx, by] = ring[(i + 1) % ring.length];
      const steps = Math.min(SURROUNDINGS_MAX_EDGE_STEPS, Math.max(1, Math.ceil(Math.hypot(bx - ax, by - ay) / cell)));
      for (let k = 0; k < steps; k++) {
        if (isNear(ax + ((bx - ax) * k) / steps, ay + ((by - ay) * k) / steps)) return true;
      }
    }
    return false;
  };
}

/** Raw (lon, lat) rings for every polygon in a Polygon or MultiPolygon
 * geometry -- one entry per polygon, each entry's first ring is the outer
 * boundary and any further rings are holes. Anything else (Point, no
 * geometry) yields no polygons. `coordinates` is typed `unknown` in
 * GeoJSONGeometry (api.ts) since the backend's contract is deliberately
 * loose here -- same pragmatic cast react-leaflet call sites already use at
 * this boundary (see MapView.tsx's `as any` on GeoJSON `data`). */
function polygonRingsLonLat(geometry: GeoJSONGeometry): Ring[][] {
  if (geometry.type === "Polygon") return [geometry.coordinates as Ring[]];
  if (geometry.type === "MultiPolygon") return geometry.coordinates as Ring[][];
  return [];
}

function pointLonLat(geometry: GeoJSONGeometry): [number, number] | null {
  if (geometry.type !== "Point") return null;
  return geometry.coordinates as [number, number];
}

function projectRing(ring: Ring, projector: Projector): [number, number][] {
  return ring.map(([lon, lat]) => projector.project(lon, lat));
}

function projectPolygon(rings: Ring[], projector: Projector): Polygon3D {
  const [outer, ...holes] = rings.map((ring) => projectRing(ring, projector));
  return { outer: outer ?? [], holes };
}

function averageLonLat(ring: Ring): [number, number] {
  let sumLon = 0;
  let sumLat = 0;
  for (const [lon, lat] of ring) {
    sumLon += lon;
    sumLat += lat;
  }
  return [sumLon / ring.length, sumLat / ring.length];
}

/** Reference point for the local projection: the territory's own centroid
 * if one is loaded (it always should be -- zone_type="territory" is a
 * required cross-module contract, see CLAUDE.md), else the first available
 * coordinate from layers/plan, else the origin as a last resort. */
function pickReferenceLonLat(layers: GeoJSONFeatureCollection | undefined, plan: GeoJSONFeatureCollection | undefined): [number, number] {
  const territory = layers?.features.find((f) => (f.properties as Record<string, unknown>).object_type === "territory");
  const territoryRings = territory ? polygonRingsLonLat(territory.geometry) : [];
  if (territoryRings[0]?.[0]) return averageLonLat(territoryRings[0][0]);

  for (const feature of layers?.features ?? []) {
    const rings = polygonRingsLonLat(feature.geometry);
    if (rings[0]?.[0]) return averageLonLat(rings[0][0]);
    const point = pointLonLat(feature.geometry);
    if (point) return point;
  }
  for (const feature of plan?.features ?? []) {
    const rings = polygonRingsLonLat(feature.geometry);
    if (rings[0]?.[0]) return averageLonLat(rings[0][0]);
    const point = pointLonLat(feature.geometry);
    if (point) return point;
  }
  return [0, 0];
}

/** Cheap deterministic pseudo-random value in [0, 1) from a numeric seed --
 * classic GLSL-style hash. Good enough for "give each building a slightly
 * different, stable placeholder height", not a real RNG. Exported so
 * ThreeDView.tsx can reuse the same formula for per-tree/shrub canopy
 * shape/size/rotation variety, keyed off each plant's own (x, y) -- same
 * "stable across rebuilds" property as buildingHash01 below, no second,
 * unrelated hash needed. */
export function hashToUnit(seed: number): number {
  const x = Math.sin(seed * 12.9898) * 43758.5453;
  return x - Math.floor(x);
}

function buildingHash01(footprintOuter: [number, number][]): number {
  if (footprintOuter.length === 0) return 0;
  let sumX = 0;
  let sumY = 0;
  for (const [x, y] of footprintOuter) {
    sumX += x;
    sumY += y;
  }
  const seed = (sumX / footprintOuter.length) * 0.1013 + (sumY / footprintOuter.length) * 0.0721;
  return hashToUnit(seed);
}

function placeholderBuildingHeight(hash01: number): number {
  return BUILDING_MIN_HEIGHT_M + hash01 * (BUILDING_MAX_HEIGHT_M - BUILDING_MIN_HEIGHT_M);
}

function extendBounds(bounds: Scene3DBounds, points: [number, number][]): void {
  for (const [x, y] of points) {
    if (x < bounds.minX) bounds.minX = x;
    if (x > bounds.maxX) bounds.maxX = x;
    if (y < bounds.minY) bounds.minY = y;
    if (y > bounds.maxY) bounds.maxY = y;
  }
}

export function buildScene3DData(
  layers: GeoJSONFeatureCollection | undefined,
  plan: GeoJSONFeatureCollection | undefined,
  norms: PlantingNorms | undefined,
  // Defaults to true (the old, only behaviour) so any call site that hasn't
  // been updated to pass the project's real crs_verified keeps working
  // exactly as before -- see makeLocalProjector for what changes when false.
  crsVerified = true
): Scene3DData {
  const [refLon, refLat] = pickReferenceLonLat(layers, plan);
  const projector = crsVerified ? makeProjector(refLon, refLat) : makeLocalProjector(refLon, refLat);

  const treeCanopyRadius = norms?.species_spacing?.tree_default?.canopy_radius_m ?? DEFAULT_TREE_CANOPY_RADIUS_M;
  const shrubCanopyRadius = norms?.species_spacing?.shrub_default?.canopy_radius_m ?? DEFAULT_SHRUB_CANOPY_RADIUS_M;

  const territory: Polygon3D[] = [];
  const buildings: Building3D[] = [];
  const roads: Polygon3D[] = [];
  const existingGreenery: Polygon3D[] = [];

  for (const feature of layers?.features ?? []) {
    const props = feature.properties as Record<string, unknown>;
    // Utilities are buried, and zoning is a soft scoring factor rather than
    // a physical thing (see CLAUDE.md) -- neither belongs in a 3D scene, and
    // zoning's real-world extent can span a whole city block (its geometry
    // is deliberately left unclipped, unlike road/building), which would
    // otherwise blow the scene bounds out far past the actual site.
    if (props.kind === "utility" || props.object_type === "zoning") continue;
    const polygons = polygonRingsLonLat(feature.geometry).map((rings) => projectPolygon(rings, projector));
    if (props.object_type === "territory") territory.push(...polygons);
    else if (props.object_type === "building")
      buildings.push(
        ...polygons.map((p) => {
          const hash01 = buildingHash01(p.outer);
          return { ...p, hash01, height: placeholderBuildingHeight(hash01) };
        })
      );
    else if (props.object_type === "road") roads.push(...polygons);
    else if (props.object_type === "existing_greenery") existingGreenery.push(...polygons);
  }

  // Окружение — только в полосе вокруг участка (см. SURROUNDINGS_BAND_M).
  const inBand = nearTerritoryFilter(territory);
  const nearBuildings = buildings.filter(inBand);
  const nearRoads = roads.filter(inBand);
  const nearGreenery = existingGreenery.filter(inBand);

  const lawns: Polygon3D[] = [];
  const trees: PlantingPoint3D[] = [];
  const shrubs: PlantingPoint3D[] = [];

  for (const feature of plan?.features ?? []) {
    const plantingType = (feature.properties as Record<string, unknown>).planting_type;
    if (plantingType === "lawn") {
      lawns.push(...polygonRingsLonLat(feature.geometry).map((rings) => projectPolygon(rings, projector)));
    } else if (plantingType === "tree" || plantingType === "shrub") {
      const point = pointLonLat(feature.geometry);
      if (!point) continue;
      const [x, y] = projector.project(point[0], point[1]);
      const props = feature.properties as { species?: unknown; crown?: unknown; crown_m?: unknown };
      // Реальный размер кроны породы, а не один норматив на всех: липа (8 м) и
      // рябина (4 м) в плане занимают разное место, и 3D обязан это показывать.
      const crownDiameter = typeof props.crown_m === "number" && props.crown_m > 0 ? props.crown_m : null;
      (plantingType === "tree" ? trees : shrubs).push({
        x,
        y,
        canopyRadius: crownDiameter !== null ? crownDiameter / 2 : plantingType === "tree" ? treeCanopyRadius : shrubCanopyRadius,
        species: typeof props.species === "string" && props.species !== "default" ? props.species : null,
        crownClass: typeof props.crown === "string" ? props.crown : null,
      });
    }
  }

  let bounds: Scene3DBounds | null = null;
  const allOuterRings = [...territory, ...nearBuildings, ...nearRoads, ...nearGreenery, ...lawns].map((p) => p.outer);
  const allPoints = [...trees, ...shrubs].map((p): [number, number] => [p.x, p.y]);
  if (allOuterRings.some((ring) => ring.length > 0) || allPoints.length > 0) {
    bounds = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity, maxBuildingHeight: 0 };
    allOuterRings.forEach((ring) => extendBounds(bounds!, ring));
    extendBounds(bounds, allPoints);
    bounds.maxBuildingHeight = nearBuildings.reduce((max, b) => Math.max(max, b.height), 0);
  }

  return {
    territory,
    buildings: nearBuildings,
    roads: nearRoads,
    existingGreenery: nearGreenery,
    lawns,
    trees,
    shrubs,
    totalItemCount: lawns.length + trees.length + shrubs.length,
    bounds,
  };
}

/** Главная ось облака точек (посадки вдоль улицы): центр, направление и
 * протяжённость по ней. Нужна для ракурса «вровень с землёй» — идти надо вдоль
 * улицы, а не вдоль осей экрана (улица редко лежит ровно с запада на восток).
 * Метод главных компонент для двух координат: угол оси — ½·atan2(2·sxy, sxx−syy),
 * без разложения матрицы. Точек не больше `MAX_AXIS_SAMPLE` (равномерно по списку):
 * на ось плана в сотни тысяч посадок хватает пяти тысяч. */
const MAX_AXIS_SAMPLE = 5000;

export interface PrincipalAxis {
  mean: [number, number];
  /** Единичный вектор оси в тех же координатах (x — на восток, y — на север). */
  direction: [number, number];
  /** Положение крайних точек вдоль оси относительно центра: tMin ≤ 0 ≤ tMax. */
  tMin: number;
  tMax: number;
}

export function principalAxis(points: { x: number; y: number }[]): PrincipalAxis | null {
  if (points.length < 2) return null;
  const step = Math.max(1, Math.floor(points.length / MAX_AXIS_SAMPLE));
  const sample: { x: number; y: number }[] = [];
  for (let i = 0; i < points.length; i += step) sample.push(points[i]);
  const n = sample.length;
  const meanX = sample.reduce((sum, p) => sum + p.x, 0) / n;
  const meanY = sample.reduce((sum, p) => sum + p.y, 0) / n;
  let sxx = 0;
  let syy = 0;
  let sxy = 0;
  for (const p of sample) {
    const dx = p.x - meanX;
    const dy = p.y - meanY;
    sxx += dx * dx;
    syy += dy * dy;
    sxy += dx * dy;
  }
  const angle = 0.5 * Math.atan2(2 * sxy, sxx - syy);
  const direction: [number, number] = [Math.cos(angle), Math.sin(angle)];
  let tMin = 0;
  let tMax = 0;
  for (const p of sample) {
    const t = (p.x - meanX) * direction[0] + (p.y - meanY) * direction[1];
    if (t < tMin) tMin = t;
    if (t > tMax) tMax = t;
  }
  return { mean: [meanX, meanY], direction, tMin, tMax };
}

/** Остановка «обхода»: участок посадок, к которому камера подлетает вплотную. */
export interface WalkStop {
  /** Центр посадок участка, метры сцены. */
  x: number;
  y: number;
  /** Куда смотреть: единичный вектор в сторону следующего участка. */
  heading: [number, number];
  count: number;
}

/** Размер участка обхода, м: ряд деревьев на таком отрезке виден целиком и
 * достаточно крупно, чтобы различить породы и кусты. */
export const WALK_CELL_M = 40;
/** Участок, где меньше стольких посадок, — не остановка, а случайный куст. */
const WALK_MIN_PLANTS = 3;
/** Дальше этого (в размерах участка) до соседа — это уже другая улица, а не
 * продолжение пути: смотреть «вперёд» туда нечего. */
const WALK_CONNECTED_CELLS = 2.2;

/** Остановки обхода в порядке движения: посадки делятся на участки сеткой,
 * путь идёт от конца улицы по ближайшему непосещённому участку. Сетка, а не
 * ломаная по оси улицы: улицы бывают Г-образными и разветвлёнными, и одна ось
 * (principalAxis) на них проходит мимо самих посадок. Для ветвления «ближайший
 * непосещённый» вернётся в ответвление позже — путь не оптимальный, но каждый
 * участок с посадками встречается ровно один раз. */
export function walkStops(points: { x: number; y: number }[], cellM: number = WALK_CELL_M): WalkStop[] {
  const cells = new Map<string, { sx: number; sy: number; n: number }>();
  for (const p of points) {
    const key = `${Math.floor(p.x / cellM)}:${Math.floor(p.y / cellM)}`;
    const cell = cells.get(key);
    if (cell) {
      cell.sx += p.x;
      cell.sy += p.y;
      cell.n += 1;
    } else {
      cells.set(key, { sx: p.x, sy: p.y, n: 1 });
    }
  }
  const remaining = [...cells.values()].filter((cell) => cell.n >= WALK_MIN_PLANTS).map((cell) => ({ x: cell.sx / cell.n, y: cell.sy / cell.n, count: cell.n }));
  if (remaining.length === 0) return [];

  // Начало — конец улицы по главной оси посадок.
  const axis = principalAxis(points);
  let startIndex = 0;
  if (axis) {
    let best = Infinity;
    remaining.forEach((cell, index) => {
      const t = (cell.x - axis.mean[0]) * axis.direction[0] + (cell.y - axis.mean[1]) * axis.direction[1];
      if (t < best) {
        best = t;
        startIndex = index;
      }
    });
  }
  const ordered: { x: number; y: number; count: number }[] = [remaining.splice(startIndex, 1)[0]];
  while (remaining.length > 0) {
    const last = ordered[ordered.length - 1];
    let nearest = 0;
    let nearestDistance = Infinity;
    remaining.forEach((cell, index) => {
      const distance = Math.hypot(cell.x - last.x, cell.y - last.y);
      if (distance < nearestDistance) {
        nearestDistance = distance;
        nearest = index;
      }
    });
    ordered.push(remaining.splice(nearest, 1)[0]);
  }

  const unit = (dx: number, dy: number): [number, number] | null => {
    const length = Math.hypot(dx, dy);
    return length > 1e-6 ? [dx / length, dy / length] : null;
  };
  const fallback: [number, number] = axis ? axis.direction : [0, 1];
  return ordered.map((cell, index) => {
    const next = ordered[index + 1];
    const previous = ordered[index - 1];
    let heading: [number, number] | null = null;
    if (next && Math.hypot(next.x - cell.x, next.y - cell.y) <= cellM * WALK_CONNECTED_CELLS) heading = unit(next.x - cell.x, next.y - cell.y);
    if (!heading && previous && Math.hypot(cell.x - previous.x, cell.y - previous.y) <= cellM * WALK_CONNECTED_CELLS) heading = unit(cell.x - previous.x, cell.y - previous.y);
    return { x: cell.x, y: cell.y, heading: heading ?? fallback, count: cell.count };
  });
}

/** Точка внутри кольца — чётно-нечётное правило (луч вправо). */
export function pointInRing(x: number, y: number, ring: [number, number][]): boolean {
  let inside = false;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    const [xi, yi] = ring[i];
    const [xj, yj] = ring[j];
    if (yi > y !== yj > y && x < ((xj - xi) * (y - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}
