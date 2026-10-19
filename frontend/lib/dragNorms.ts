/** Расстояния от точки до охранных зон при перетаскивании (п. 2 редизайна).
 *
 * Геометрию ограничений вокруг точки сервер отдаёт один раз в начале жеста
 * (`getNearbyConstraints`), а подпись «0,9 м · нужно 2 м» обновляется на каждом
 * кадре — запросом на кадр её не сделать. Поэтому здесь чистая геометрия на
 * плоскости в метрах, без библиотек: ровно два действия — расстояние от точки
 * до ломаной/кольца и «точка внутри полигона». Это то, что в задании названо
 * turf.js (`pointToLineDistance`, `booleanPointInPolygon`); отдельная
 * зависимость ради двух функций на сто строк не окупается.
 *
 * Координаты в градусах (проверенная СК) переводятся в метры на плоскости около
 * центра жеста — на масштабе пары десятков метров искажение пренебрежимо; для
 * чертежа без СК (CRS.Simple) координаты уже метры.
 */

import type { GeoJSONGeometry, NearbyConstraint } from "@/lib/api";

export type MetreProjector = (lng: number, lat: number) => [number, number];

export function makeMetreProjector(centreLat: number, centreLng: number, geographic: boolean): MetreProjector {
  if (!geographic) return (lng, lat) => [lng, lat];
  const kx = 111_320 * Math.cos((centreLat * Math.PI) / 180);
  const ky = 110_540;
  return (lng, lat) => [(lng - centreLng) * kx, (lat - centreLat) * ky];
}

export type Ring = Float64Array; // [x0, y0, x1, y1, …]

export interface PreparedConstraint {
  index: number;
  kind: "constraint" | "neighbour";
  /** Только у соседа: чтобы не считать ограничением ту же группу, что тянут. */
  id?: string;
  type: string;
  label: string;
  requiredM: number;
  /** Линии и границы колец — расстояние до них. */
  lines: Ring[];
  /** Полигоны: внутри — расстояние 0. */
  polygons: { outer: Ring; holes: Ring[] }[];
  points: [number, number][];
}

function toRing(coords: unknown, project: MetreProjector): Ring {
  const list = coords as number[][];
  const out = new Float64Array(list.length * 2);
  list.forEach((c, i) => {
    const [x, y] = project(c[0], c[1]);
    out[i * 2] = x;
    out[i * 2 + 1] = y;
  });
  return out;
}

function collect(geometry: GeoJSONGeometry, project: MetreProjector, into: PreparedConstraint) {
  const c = geometry.coordinates;
  switch (geometry.type) {
    case "Point": {
      const [x, y] = project((c as number[])[0], (c as number[])[1]);
      into.points.push([x, y]);
      break;
    }
    case "MultiPoint":
      (c as number[][]).forEach((p) => collect({ type: "Point", coordinates: p }, project, into));
      break;
    case "LineString":
      into.lines.push(toRing(c, project));
      break;
    case "MultiLineString":
      (c as number[][][]).forEach((line) => into.lines.push(toRing(line, project)));
      break;
    case "Polygon": {
      const rings = (c as number[][][]).map((r) => toRing(r, project));
      if (rings.length === 0) break;
      into.polygons.push({ outer: rings[0], holes: rings.slice(1) });
      rings.forEach((r) => into.lines.push(r));
      break;
    }
    case "MultiPolygon":
      (c as number[][][][]).forEach((poly) => collect({ type: "Polygon", coordinates: poly }, project, into));
      break;
    default:
      break;
  }
}

/** Ограничения от сервера → плоская геометрия в метрах, один раз за жест. */
export function prepareConstraints(features: NearbyConstraint[], project: MetreProjector): PreparedConstraint[] {
  return features.map((f, index) => {
    const prepared: PreparedConstraint = {
      index,
      kind: f.kind,
      id: f.id ?? undefined,
      type: f.object_type,
      label: f.object_label,
      requiredM: f.required_m,
      lines: [],
      polygons: [],
      points: [],
    };
    if (f.geometry) collect(f.geometry, project, prepared);
    return prepared;
  });
}

function distanceToSegment(px: number, py: number, ax: number, ay: number, bx: number, by: number): number {
  const dx = bx - ax;
  const dy = by - ay;
  const lengthSq = dx * dx + dy * dy;
  const t = lengthSq === 0 ? 0 : Math.max(0, Math.min(1, ((px - ax) * dx + (py - ay) * dy) / lengthSq));
  return Math.hypot(px - (ax + t * dx), py - (ay + t * dy));
}

function distanceToPath(px: number, py: number, path: Ring): number {
  let best = Infinity;
  for (let i = 0; i + 3 < path.length; i += 2) {
    const d = distanceToSegment(px, py, path[i], path[i + 1], path[i + 2], path[i + 3]);
    if (d < best) best = d;
  }
  if (path.length === 2) best = Math.hypot(px - path[0], py - path[1]);
  return best;
}

/** Чётно-нечётное правило: луч вправо от точки, число пересечений рёбер кольца. */
export function insideRing(px: number, py: number, ring: Ring): boolean {
  let inside = false;
  const n = ring.length / 2;
  for (let i = 0, j = n - 1; i < n; j = i++) {
    const xi = ring[i * 2];
    const yi = ring[i * 2 + 1];
    const xj = ring[j * 2];
    const yj = ring[j * 2 + 1];
    if (yi > py !== yj > py && px < ((xj - xi) * (py - yi)) / (yj - yi) + xi) inside = !inside;
  }
  return inside;
}

/** Расстояние от точки до ограничения, м; 0 — точка внутри полигона (стена
 * здания, полоса отступа) — в запретной зоне при любом требуемом отступе. */
export function distanceToConstraint(c: PreparedConstraint, px: number, py: number): number {
  for (const polygon of c.polygons) {
    if (insideRing(px, py, polygon.outer) && !polygon.holes.some((hole) => insideRing(px, py, hole))) return 0;
  }
  let best = Infinity;
  for (const line of c.lines) {
    const d = distanceToPath(px, py, line);
    if (d < best) best = d;
  }
  for (const [x, y] of c.points) {
    const d = Math.hypot(px - x, py - y);
    if (d < best) best = d;
  }
  return best;
}

export interface ConstraintReading {
  constraint: PreparedConstraint;
  distanceM: number;
  /** Запас до нормы, м: отрицательный — нарушение. */
  slackM: number;
}

/** Самое нарушенное (а если нарушений нет — самое тесное) ограничение для
 * набора точек. `skipIds` — деревья, которые тянут сами: соседями они не считаются. */
export function worstReading(constraints: PreparedConstraint[], points: [number, number][], skipIds?: ReadonlySet<string>): ConstraintReading | null {
  let worst: ConstraintReading | null = null;
  for (const constraint of constraints) {
    if (constraint.kind === "neighbour" && constraint.id && skipIds?.has(constraint.id)) continue;
    for (const [x, y] of points) {
      const distanceM = distanceToConstraint(constraint, x, y);
      const slackM = distanceM - constraint.requiredM;
      if (worst === null || slackM < worst.slackM) worst = { constraint, distanceM, slackM };
    }
  }
  return worst;
}

/** Не больше стольких точек группы проверяется на кадре: на выделении в
 * тысячи посадок расстояние до сотен отрезков для каждой было бы тяжелее
 * самого жеста. Берутся равномерно по списку — группа ровная, как и ряд. */
export const MAX_SAMPLED_POINTS = 40;

export function samplePoints<T>(points: T[]): T[] {
  if (points.length <= MAX_SAMPLED_POINTS) return points;
  const step = points.length / MAX_SAMPLED_POINTS;
  return Array.from({ length: MAX_SAMPLED_POINTS }, (_, i) => points[Math.floor(i * step)]);
}

/** Цвета зон по типу ограничения — те же, что у слоя «где можно сажать»
 * (backend/app/services/zones_service.py::_BLOCKER_RGBA): одна и та же теплосеть
 * лиловая и там, и здесь. Держать в синхроне руками. */
const ZONE_COLORS: Record<string, string> = {
  heat_network: "#A994E8",
  cable_line: "#6EB4F0",
  building: "#F0B45A",
  gas_pipe: "#E8C48A",
  water_pipe: "#5AAADC",
  sewer: "#B48C64",
  road: "#A9B1AB",
  sidewalk: "#A9B1AB",
  sidewalk_kerb: "#A9B1AB",
  lighting_pole: "#C8B68A",
  power_line_corridor: "#E87878",
  neighbour_tree: "#A9B1AB",
};

export function dragZoneColor(type: string): string {
  return ZONE_COLORS[type] ?? "#8C9691";
}
