import type { GeoJSONFeatureCollection } from "@/lib/api";

/** Насколько близко (м) посадка новой версии к посадке старой, чтобы считать её
 * «той же». id между генерациями разные, поэтому сопоставление — по положению:
 * тот же тип и расстояние не больше допуска. Полметра — задание (п. 5); это
 * меньше любого интервала между посадками, так что две соседние не перепутаются. */
export const MATCH_TOLERANCE_M = 0.5;

export interface PlanDiff {
  /** Есть в новой версии, но не было в прежней — координаты карты [lat, lng]. */
  added: [number, number][];
  /** Было в прежней версии, в новой нет. */
  removed: [number, number][];
  unchanged: number;
}

interface Point {
  /** Координаты карты: lat = y, lng = x (как у Leaflet и у CRS.Simple). */
  lat: number;
  lng: number;
  /** Метры на плоскости — для расстояний. */
  mx: number;
  my: number;
  type: string;
}

function collectPoints(collection: GeoJSONFeatureCollection, toMetres: (lng: number, lat: number) => [number, number]): Point[] {
  const points: Point[] = [];
  for (const feature of collection.features) {
    if (feature.geometry.type !== "Point") continue;
    const [lng, lat] = feature.geometry.coordinates as [number, number];
    const [mx, my] = toMetres(lng, lat);
    points.push({ lat, lng, mx, my, type: String(feature.properties.planting_type ?? "") });
  }
  return points;
}

/** Что изменилось между версиями плана: добавлено, убрано, без изменений.
 * Только точки (деревья и кустарник) — газон полигон, у него «то же место» не
 * определено, а площадь сравнивается в сводке. Сопоставление через сетку
 * ячеек размером с допуск: каждая точка новой версии смотрит только в девять
 * соседних ячеек, так что план в сотни тысяч точек — это линейный проход, а не
 * перебор всех пар. Каждая прежняя посадка достаётся не более чем одной новой
 * (жадно, ближайшей): иначе два новых дерева рядом «оба совпали» бы с одним старым.
 *
 * `geographic` — координаты в градусах (проверенная СК): переводятся в метры
 * на плоскости около центра плана; иначе они уже метры чертежа. */
export function diffPlans(base: GeoJSONFeatureCollection, next: GeoJSONFeatureCollection, geographic: boolean): PlanDiff {
  let toMetres: (lng: number, lat: number) => [number, number] = (lng, lat) => [lng, lat];
  if (geographic) {
    const sample = next.features.find((f) => f.geometry.type === "Point") ?? base.features.find((f) => f.geometry.type === "Point");
    const lat0 = sample ? (sample.geometry.coordinates as [number, number])[1] : 55.75;
    const kx = 111_320 * Math.cos((lat0 * Math.PI) / 180);
    toMetres = (lng, lat) => [lng * kx, lat * 110_540];
  }
  const before = collectPoints(base, toMetres);
  const after = collectPoints(next, toMetres);

  const cell = (v: number) => Math.floor(v / MATCH_TOLERANCE_M);
  const grid = new Map<string, number[]>();
  before.forEach((p, i) => {
    const key = `${p.type}|${cell(p.mx)}|${cell(p.my)}`;
    const bucket = grid.get(key);
    if (bucket) bucket.push(i);
    else grid.set(key, [i]);
  });

  const taken = new Uint8Array(before.length);
  const added: [number, number][] = [];
  let unchanged = 0;
  for (const p of after) {
    const cx = cell(p.mx);
    const cy = cell(p.my);
    let best = -1;
    let bestDistance = MATCH_TOLERANCE_M;
    for (let dx = -1; dx <= 1; dx++) {
      for (let dy = -1; dy <= 1; dy++) {
        for (const i of grid.get(`${p.type}|${cx + dx}|${cy + dy}`) ?? []) {
          if (taken[i]) continue;
          const distance = Math.hypot(before[i].mx - p.mx, before[i].my - p.my);
          if (distance <= bestDistance) {
            best = i;
            bestDistance = distance;
          }
        }
      }
    }
    if (best >= 0) {
      taken[best] = 1;
      unchanged += 1;
    } else {
      added.push([p.lat, p.lng]);
    }
  }
  const removed: [number, number][] = [];
  before.forEach((p, i) => {
    if (!taken[i]) removed.push([p.lat, p.lng]);
  });
  return { added, removed, unchanged };
}
