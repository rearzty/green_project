/** «Где можно поставить» при переносе посадки: полигон допустимого места от
 * сервера (`GET .../items/{id}/allowed-area`) и два вопроса к нему, которые
 * задаются на каждом кадре жеста — «можно ли здесь» и «куда ближайшее можно».
 *
 * Сервер сжимает область на 10 см внутрь (fix_service.ALLOWED_SHRINK_M), поэтому
 * любая точка области, включая её границу, допустима по построению — и
 * «примагничивание» к ближайшей точке границы ставит посадку туда, где проверка
 * плана её не отвергнет.
 *
 * Считается на плоскости в метрах около центра жеста (как lib/dragNorms.ts) —
 * запрос на кадр был бы медленнее самого жеста. Чистый модуль без Leaflet. */

import type { GeoJSONGeometry } from "@/lib/api";
import { insideRing, type MetreProjector, type Ring } from "./dragNorms";

interface RingData {
  /** [lng0, lat0, lng1, lat1, …] — чтобы вернуть ответ в координатах карты. */
  lnglat: Float64Array;
  /** Те же вершины в метрах. */
  xy: Ring;
}

interface PolygonData {
  /** rings[0] — внешнее кольцо, остальные — дыры. */
  rings: RingData[];
  minX: number;
  minY: number;
  maxX: number;
  maxY: number;
}

export interface NearestAllowedPoint {
  lng: number;
  lat: number;
  distanceM: number;
}

export interface PreparedAllowedArea {
  /** В окне допустимого места нет. */
  empty: boolean;
  /** Все кольца всех полигонов как [lat, lng] — для рисования в Leaflet (внешние и дыры вперемешку: заливка evenodd). */
  rings: [number, number][][];
  contains(x: number, y: number): boolean;
  /** Ближайшая точка границы области в пределах `maxM` м; null — дальше. */
  nearest(x: number, y: number, maxM: number): NearestAllowedPoint | null;
}

function ringData(coords: number[][], project: MetreProjector): RingData {
  const lnglat = new Float64Array(coords.length * 2);
  const xy = new Float64Array(coords.length * 2);
  coords.forEach((c, i) => {
    lnglat[i * 2] = c[0];
    lnglat[i * 2 + 1] = c[1];
    const [x, y] = project(c[0], c[1]);
    xy[i * 2] = x;
    xy[i * 2 + 1] = y;
  });
  return { lnglat, xy };
}

function polygonData(polygon: number[][][], project: MetreProjector): PolygonData | null {
  if (polygon.length === 0 || polygon[0].length < 4) return null;
  const rings = polygon.map((ring) => ringData(ring, project));
  let minX = Infinity;
  let minY = Infinity;
  let maxX = -Infinity;
  let maxY = -Infinity;
  const outer = rings[0].xy;
  for (let i = 0; i < outer.length; i += 2) {
    minX = Math.min(minX, outer[i]);
    maxX = Math.max(maxX, outer[i]);
    minY = Math.min(minY, outer[i + 1]);
    maxY = Math.max(maxY, outer[i + 1]);
  }
  return { rings, minX, minY, maxX, maxY };
}

const EMPTY: PreparedAllowedArea = { empty: true, rings: [], contains: () => false, nearest: () => null };

export function prepareAllowedArea(geometry: GeoJSONGeometry | null, project: MetreProjector): PreparedAllowedArea {
  if (!geometry) return EMPTY;
  const raw: number[][][][] =
    geometry.type === "MultiPolygon" ? (geometry.coordinates as number[][][][]) : geometry.type === "Polygon" ? [geometry.coordinates as number[][][]] : [];
  const polygons = raw.map((polygon) => polygonData(polygon, project)).filter((p): p is PolygonData => p !== null);
  if (polygons.length === 0) return EMPTY;

  return {
    empty: false,
    rings: polygons.flatMap((p) => p.rings.map((ring) => Array.from({ length: ring.lnglat.length / 2 }, (_, i) => [ring.lnglat[i * 2 + 1], ring.lnglat[i * 2]] as [number, number]))),
    contains(x, y) {
      for (const polygon of polygons) {
        if (x < polygon.minX || x > polygon.maxX || y < polygon.minY || y > polygon.maxY) continue;
        if (insideRing(x, y, polygon.rings[0].xy) && !polygon.rings.slice(1).some((hole) => insideRing(x, y, hole.xy))) return true;
      }
      return false;
    },
    nearest(x, y, maxM) {
      let best: NearestAllowedPoint | null = null;
      let bestDistance = maxM;
      for (const polygon of polygons) {
        // Полигон, до рамки которого уже дальше найденного, перебирать незачем.
        const dx = Math.max(polygon.minX - x, 0, x - polygon.maxX);
        const dy = Math.max(polygon.minY - y, 0, y - polygon.maxY);
        if (Math.hypot(dx, dy) > bestDistance) continue;
        for (const ring of polygon.rings) {
          const { xy, lnglat } = ring;
          for (let i = 0; i + 3 < xy.length; i += 2) {
            const ax = xy[i];
            const ay = xy[i + 1];
            const sx = xy[i + 2] - ax;
            const sy = xy[i + 3] - ay;
            const lengthSq = sx * sx + sy * sy;
            const t = lengthSq === 0 ? 0 : Math.max(0, Math.min(1, ((x - ax) * sx + (y - ay) * sy) / lengthSq));
            const d = Math.hypot(x - (ax + t * sx), y - (ay + t * sy));
            if (d < bestDistance) {
              bestDistance = d;
              best = {
                lng: lnglat[i] + t * (lnglat[i + 2] - lnglat[i]),
                lat: lnglat[i + 1] + t * (lnglat[i + 3] - lnglat[i + 1]),
                distanceM: d,
              };
            }
          }
        }
      }
      return best;
    },
  };
}

/** Квадрат `radiusM` вокруг центра как кольцо [lat, lng] — внешняя граница
 * закраски запретного: допустимое место вырезается из него дырами. Те же
 * коэффициенты перевода градусов в метры, что у `makeMetreProjector`. */
export function windowRing(centreLat: number, centreLng: number, radiusM: number, geographic: boolean): [number, number][] {
  const dLat = geographic ? radiusM / 110_540 : radiusM;
  const dLng = geographic ? radiusM / (111_320 * Math.cos((centreLat * Math.PI) / 180)) : radiusM;
  return [
    [centreLat - dLat, centreLng - dLng],
    [centreLat - dLat, centreLng + dLng],
    [centreLat + dLat, centreLng + dLng],
    [centreLat + dLat, centreLng - dLng],
  ];
}
