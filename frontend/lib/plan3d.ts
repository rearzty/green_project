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
  canopyRadius: number;
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
      (plantingType === "tree" ? trees : shrubs).push({
        x,
        y,
        canopyRadius: plantingType === "tree" ? treeCanopyRadius : shrubCanopyRadius,
      });
    }
  }

  let bounds: Scene3DBounds | null = null;
  const allOuterRings = [...territory, ...buildings, ...roads, ...existingGreenery, ...lawns].map((p) => p.outer);
  const allPoints = [...trees, ...shrubs].map((p): [number, number] => [p.x, p.y]);
  if (allOuterRings.some((ring) => ring.length > 0) || allPoints.length > 0) {
    bounds = { minX: Infinity, maxX: -Infinity, minY: Infinity, maxY: -Infinity, maxBuildingHeight: 0 };
    allOuterRings.forEach((ring) => extendBounds(bounds!, ring));
    extendBounds(bounds, allPoints);
    bounds.maxBuildingHeight = buildings.reduce((max, b) => Math.max(max, b.height), 0);
  }

  return {
    territory,
    buildings,
    roads,
    existingGreenery,
    lawns,
    trees,
    shrubs,
    totalItemCount: lawns.length + trees.length + shrubs.length,
    bounds,
  };
}
