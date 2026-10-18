import type { GeoJSONFeatureCollection, GeoJSONGeometry } from "@/lib/api";

type Ring = [number, number][];

/** Метров в градусе широты/долготы на широте `lat` -- equirectangular, той
 * же точности, что plan3d.ts::makeProjector: на масштабе одного участка
 * геодезическая точность не нужна. */
function degreeScale(lat: number): [number, number] {
  return [111_320 * Math.cos((lat * Math.PI) / 180), 110_540];
}

function ringArea(ring: Ring, scale: [number, number]): number {
  let sum = 0;
  for (let i = 0, j = ring.length - 1; i < ring.length; j = i++) {
    sum += ring[j][0] * scale[0] * ring[i][1] * scale[1] - ring[i][0] * scale[0] * ring[j][1] * scale[1];
  }
  return Math.abs(sum) / 2;
}

function polygonArea(rings: Ring[], geographic: boolean): number {
  if (!rings.length || !rings[0].length) return 0;
  const scale: [number, number] = geographic ? degreeScale(rings[0][0][1]) : [1, 1];
  const [outer, ...holes] = rings;
  return Math.max(0, ringArea(outer, scale) - holes.reduce((s, h) => s + ringArea(h, scale), 0));
}

/** Площадь полигона газона в м². Координаты плана -- локальные метры чертежа,
 * если CRS не подтверждена (MapView рисует их в CRS.Simple, см.
 * geo_io.py::display_crs), иначе WGS84-градусы. */
export function geometryAreaM2(geometry: GeoJSONGeometry, geographic: boolean): number {
  if (geometry.type === "Polygon") return polygonArea(geometry.coordinates as Ring[], geographic);
  if (geometry.type === "MultiPolygon") {
    return (geometry.coordinates as Ring[][]).reduce((s, rings) => s + polygonArea(rings, geographic), 0);
  }
  return 0;
}

export interface PlanStats {
  trees: number;
  shrubs: number;
  lawnAreaM2: number;
  speciesCount: number;
  /** Сумма площадей крон деревьев по диаметру кроны породы (species.yaml),
   * м², без вычета перекрытий — оценка сверху. */
  canopyM2: number;
  /** Доля деревьев, посаженных по схеме (ряд, группа, солитер), а не россыпью. */
  designedShare: number | null;
  /** Доля самой частой породы среди деревьев и кустарника. */
  topSpeciesShare: number | null;
}

// Приставки обоснования, по которым видна схема посадки — те же строки, что
// geo_engine/planner.py (ROW/TREE_GROUP/SOLITAIRE_RATIONALE_PREFIX).
const DESIGNED_PREFIXES = ["Рядовая посадка", "Групповая посадка деревьев", "Солитер"];

/** Итог плана для сводки поверх карты -- всегда из текущего plan.features,
 * который useProjectSession держит актуальным на каждой правке. */
export function computePlanStats(features: GeoJSONFeatureCollection, geographic: boolean): PlanStats {
  let trees = 0;
  let shrubs = 0;
  let lawnAreaM2 = 0;
  let canopyM2 = 0;
  let designed = 0;
  const species = new Map<string, number>();
  for (const feature of features.features) {
    const props = feature.properties as { planting_type?: string; species?: string; crown_m?: number | null; rationale?: string };
    if (props.planting_type === "tree") {
      trees += 1;
      if (props.crown_m) canopyM2 += (Math.PI * props.crown_m * props.crown_m) / 4;
      if (props.rationale && DESIGNED_PREFIXES.some((prefix) => props.rationale!.includes(prefix))) designed += 1;
    } else if (props.planting_type === "shrub") shrubs += 1;
    else if (props.planting_type === "lawn") lawnAreaM2 += geometryAreaM2(feature.geometry, geographic);
    // «default» — заглушка «порода не назначена», а не порода.
    if (props.planting_type !== "lawn" && props.species && props.species !== "default") {
      species.set(props.species, (species.get(props.species) ?? 0) + 1);
    }
  }
  const planted = [...species.values()].reduce((sum, n) => sum + n, 0);
  const top = species.size ? Math.max(...species.values()) : 0;
  return {
    trees,
    shrubs,
    lawnAreaM2,
    speciesCount: species.size,
    canopyM2,
    designedShare: trees ? designed / trees : null,
    topSpeciesShare: planted ? top / planted : null,
  };
}
