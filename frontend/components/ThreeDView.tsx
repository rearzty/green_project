"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

import { getPlantingNorms, type GeoJSONFeatureCollection, type PlantingNorms } from "@/lib/api";
import { buildScene3DData, hashToUnit, MAX_3D_ITEMS, type Building3D, type PlantingPoint3D, type Polygon3D } from "@/lib/plan3d";
import { SEASON_PALETTES, ZONE_COLORS, type Season } from "@/lib/mapStyle";

export interface ThreeDViewProps {
  layers?: GeoJSONFeatureCollection;
  plan?: GeoJSONFeatureCollection;
  /** Cosmetic tree/shrub/lawn/sky tint -- see SEASON_PALETTES (mapStyle.ts).
   * A full scene rebuild is cheap enough (~130ms at 33,789 items, measured
   * this session's stress test) that season is just another dependency of
   * the main build effect below, not a separate live-material-update path. */
  season?: Season;
  /** Scopes camera-position persistence (see loadStoredCamera/saveStoredCamera
   * below) to one project -- the project id, not the plan id, since switching
   * between plans of the same project (edits, re-generates) shouldn't reset a
   * framing the user already set up, but a genuinely different project (a
   * different, differently-scaled territory) shouldn't inherit one either.
   * Persistence is simply skipped when this is undefined (no project open yet). */
  storageKey?: string;
}

// Ground stacked in thin slabs (territory, then road/greenery/lawn on top)
// purely to avoid z-fighting between coplanar surfaces. A gap of a few cm
// (the original value here) turned out not to be enough -- at the near/far
// camera range needed to frame a real site, the depth buffer's precision at
// typical viewing distance is coarser than that, so territory's blue flickered
// through the layers above it. 10-15cm is still invisible at tree/building
// scale but comfortably clears that precision floor (see the camera
// near/far and logarithmicDepthBuffer choices below, which address the same
// root cause from the other side).
const TERRITORY_DEPTH_M = 0.2;
const ROAD_Y = TERRITORY_DEPTH_M + 0.08;
const EXISTING_GREENERY_Y = TERRITORY_DEPTH_M + 0.14;
const LAWN_Y = TERRITORY_DEPTH_M + 0.2;
const PLANTING_BASE_Y = TERRITORY_DEPTH_M + 0.2;

const TRUNK_COLOR = "#7c5a3a";

// One shared canvas texture for every building's walls -- Texture.clone()
// (used per-building below) copies only repeat/wrapping settings, not
// pixels, so this stays a single small GPU upload regardless of how many
// buildings a site has (~25+ on this session's stress-test site).
let sharedWindowTexture: THREE.CanvasTexture | null = null;
function getWindowTexture(): THREE.CanvasTexture {
  if (sharedWindowTexture) return sharedWindowTexture;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d")!;
  // Higher contrast than a first attempt (light grey wall / near-white
  // windows) turned out to need -- at real building scale the pattern
  // tiled so finely it washed out to a flat grey blur from any normal
  // viewing distance. Darker "glass" squares against a lighter wall read
  // as windows even at a glance.
  ctx.fillStyle = "#d6d3d1";
  ctx.fillRect(0, 0, 64, 64);
  ctx.fillStyle = "#475569";
  for (let y = 6; y < 64; y += 16) {
    for (let x = 6; x < 64; x += 16) ctx.fillRect(x, y, 10, 10);
  }
  sharedWindowTexture = new THREE.CanvasTexture(canvas);
  sharedWindowTexture.wrapS = sharedWindowTexture.wrapT = THREE.RepeatWrapping;
  sharedWindowTexture.colorSpace = THREE.SRGBColorSpace;
  return sharedWindowTexture;
}

// Same shared-canvas-texture trick as the window texture above, but drawn in
// neutral white/grey (not pre-colored green) -- MeshStandardMaterial
// multiplies a diffuse map by `material.color`, so painting it grayscale lets
// SEASON_PALETTES' actual lawn color (spring pale green through winter
// near-white) tint the same texture correctly every season, rather than
// needing one texture per season.
let sharedGrassTexture: THREE.CanvasTexture | null = null;
function getGrassTexture(): THREE.CanvasTexture {
  if (sharedGrassTexture) return sharedGrassTexture;
  const canvas = document.createElement("canvas");
  canvas.width = canvas.height = 64;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#ffffff";
  ctx.fillRect(0, 0, 64, 64);
  ctx.fillStyle = "#d4d4d4";
  // A fixed linear-congruential sequence, not Math.random() -- this canvas is
  // built once per page load regardless, so determinism buys nothing
  // functional here, but it costs nothing either and matches this file's
  // existing preference for reproducible-looking output.
  let seed = 1;
  const nextRand = () => {
    seed = (seed * 16807) % 2147483647;
    return (seed - 1) / 2147483646;
  };
  for (let i = 0; i < 110; i++) ctx.fillRect(nextRand() * 64, nextRand() * 64, 1.5, 1.5);
  sharedGrassTexture = new THREE.CanvasTexture(canvas);
  sharedGrassTexture.wrapS = sharedGrassTexture.wrapT = THREE.RepeatWrapping;
  sharedGrassTexture.colorSpace = THREE.SRGBColorSpace;
  return sharedGrassTexture;
}

/** Lawn only (addFlat above still handles roads/existing_greenery with one
 * flat color, unchanged) -- same "tiles per metre" fix as buildingMesh's
 * wallTexture.repeat, since ShapeGeometry's default UV generator is the flat
 * (non-extruded) sibling of the ExtrudeGeometry side-wall UV bug already
 * found and fixed in this file: local meter coordinates, not normalized
 * [0,1]. Confirmed live rather than re-assumed from that unrelated fix. */
const GRASS_TILE_METRES = 1.5;
function lawnMesh(polygon: Polygon3D, color: string, y: number, disposables: Disposable[]): THREE.Mesh | null {
  const shape = shapeFromPolygon(polygon);
  if (!shape) return null;
  const geometry = new THREE.ShapeGeometry(shape);
  const texture = getGrassTexture().clone();
  texture.needsUpdate = true;
  texture.repeat.set(1 / GRASS_TILE_METRES, 1 / GRASS_TILE_METRES);
  const material = new THREE.MeshStandardMaterial({ color, map: texture, side: THREE.DoubleSide });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = y;
  disposables.push({ geometry, material, texture });
  return mesh;
}

/** Not a real THREE.Shape by itself -- a polygon can come back with too few
 * points from degenerate source geometry (a hole equal to its own outer
 * ring, a zero-area sliver); guard once here rather than in every caller. */
function shapeFromPolygon(polygon: Polygon3D): THREE.Shape | null {
  if (polygon.outer.length < 3) return null;
  const shape = new THREE.Shape(polygon.outer.map(([x, y]) => new THREE.Vector2(x, y)));
  for (const hole of polygon.holes) {
    if (hole.length < 3) continue;
    shape.holes.push(new THREE.Path(hole.map(([x, y]) => new THREE.Vector2(x, y))));
  }
  return shape;
}

/** A flat colored mesh laid in the XZ (ground) plane at world height `y`.
 * ShapeGeometry builds in the shape's own local XY plane; rotating -90° about
 * X maps local (x, y) -> world (x, 0, -y) and lets `position.y` place it at
 * the right ground height, same trick `extrudedMesh` uses for the vertical
 * axis. */
function flatMesh(polygon: Polygon3D, color: string, y: number): THREE.Mesh | null {
  const shape = shapeFromPolygon(polygon);
  if (!shape) return null;
  const geometry = new THREE.ShapeGeometry(shape);
  const material = new THREE.MeshStandardMaterial({ color, side: THREE.DoubleSide });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = y;
  return mesh;
}

/** An extruded mesh whose base sits at world height `baseY` and rises by
 * `height` -- ExtrudeGeometry extrudes along local +Z, which the same -90°
 * X rotation maps to world +Y. */
function extrudedMesh(polygon: Polygon3D, color: string, height: number, baseY: number): THREE.Mesh | null {
  const shape = shapeFromPolygon(polygon);
  if (!shape) return null;
  const geometry = new THREE.ExtrudeGeometry(shape, { depth: height, bevelEnabled: false });
  const material = new THREE.MeshStandardMaterial({ color });
  const mesh = new THREE.Mesh(geometry, material);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = baseY;
  return mesh;
}

/** Buildings only (territory/roads/etc. keep using extrudedMesh/flatMesh
 * above with one material) -- ExtrudeGeometry groups faces by material index
 * 0 = lid (top + bottom caps, from buildLidFaces) and 1 = extruded side
 * walls (from buildSideFaces), confirmed against the installed three@0.186.0
 * source rather than assumed, so a flat roof color and a textured wall are
 * two separate materials passed as an array, not one material with a
 * texture that would also smear across the roof. */
function buildingMesh(building: Building3D, capColor: string, baseY: number, disposables: Disposable[]): THREE.Mesh | null {
  const shape = shapeFromPolygon(building);
  if (!shape) return null;
  const geometry = new THREE.ExtrudeGeometry(shape, { depth: building.height, bevelEnabled: false });
  const capMaterial = new THREE.MeshStandardMaterial({ color: capColor });

  const wallTexture = getWindowTexture().clone();
  wallTexture.needsUpdate = true;
  // ExtrudeGeometry's default side-wall UV generator (verified by reading
  // three's own source, not assumed) does NOT normalize U/V to [0,1] per
  // face -- it uses the vertex's raw LOCAL METER coordinate directly (the
  // along-wall axis for U, extrusion depth for V). A repeat count meant for
  // "N tiles across the whole wall" was actually being read as "N tiles per
  // metre", tiling a real building's wall so finely (sub-centimetre) that it
  // averaged out to a flat grey blur -- found live, close-up screenshots
  // showed no visible pattern at all despite the material/texture/UVs all
  // checking out individually. Fixed by treating repeat as "tiles per
  // metre" directly: 1 tile roughly every 3m reads as a believable
  // window-bay rhythm at both real (tens of metres) and demo (~20m) scale.
  const TILE_METRES = 3;
  wallTexture.repeat.set(1 / TILE_METRES, 1 / TILE_METRES);
  const wallMaterial = new THREE.MeshStandardMaterial({
    map: wallTexture,
    // Same centroid hash `height` derives from -- a subtle per-building tint
    // so a row of buildings doesn't look like identical clones, without a
    // second, unrelated source of randomness.
    color: new THREE.Color().setHSL(building.hash01, 0.12, 0.55),
  });

  const mesh = new THREE.Mesh(geometry, [capMaterial, wallMaterial]);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = baseY;
  disposables.push({ geometry, material: capMaterial });
  disposables.push({ geometry, material: wallMaterial, texture: wallTexture });
  return mesh;
}

interface Disposable {
  geometry: THREE.BufferGeometry;
  material: THREE.Material;
  // Material.dispose() does not cascade-dispose a `map` texture -- a cloned
  // per-building window texture (see buildingMesh below) needs its own
  // explicit disposal or it leaks on every remount/season rebuild.
  texture?: THREE.Texture;
}

/** Per-plant look derived once from its own (x, y) via hashToUnit
 * (plan3d.ts) -- same plant always renders the same way across a full scene
 * rebuild (season change, ensure_materialized re-fetch), no separate
 * randomness source to keep in sync with anything. Three independent-looking
 * values come from one hash function by offsetting the seed (58.2/finding
 * three unrelated-looking outputs from one sine hash needs seeds spread
 * further apart than the hash's own short period of visible correlation --
 * these offsets were picked empirically, same spirit as buildingHash01's own
 * 0.1013/0.0721 weights). */
interface PlantInstanceStyle {
  /** ~0.82-1.18 -- scales trunk height/radius and canopy size together, so a
   * "taller" tree's canopy doesn't detach from a not-equally-taller trunk. */
  sizeJitter: number;
  rotationY: number;
}

function plantStyle(point: PlantingPoint3D, variantSalt: number): PlantInstanceStyle {
  const seed = point.x * 0.1013 + point.y * 0.0721 + variantSalt;
  return {
    sizeJitter: 0.82 + hashToUnit(seed + 31.7) * 0.36,
    rotationY: hashToUnit(seed + 58.2) * Math.PI * 2,
  };
}

/** One InstancedMesh trunk plus one of two canopy silhouettes (conical --
 * evergreen-ish, or rounded -- deciduous-ish) per tree, picked per-plant by
 * `plantStyle`'s hash so the same site always renders the same mix across
 * rebuilds. Still exactly 3 draw calls total for however many trees a plan
 * holds (trunk + 2 canopy buckets), not 3 per tree -- GPU instancing is what
 * makes hundreds of thousands of these (see CLAUDE.md) cheap in the first
 * place, splitting into 2 canopy shapes doesn't change that order of
 * magnitude. Geometry is a unit primitive (radius/height 1), scaled
 * per-instance via the matrix. */
function buildTrees(points: PlantingPoint3D[], canopyColor: string, disposables: Disposable[]): THREE.Object3D[] {
  if (points.length === 0) return [];

  const trunkGeometry = new THREE.CylinderGeometry(1, 1, 1, 8);
  const trunkMaterial = new THREE.MeshStandardMaterial({ color: TRUNK_COLOR });
  const trunkMesh = new THREE.InstancedMesh(trunkGeometry, trunkMaterial, points.length);
  disposables.push({ geometry: trunkGeometry, material: trunkMaterial });

  const conicalGeometry = new THREE.ConeGeometry(1, 1, 10);
  const roundedGeometry = new THREE.SphereGeometry(1, 8, 6);
  const canopyMaterial = new THREE.MeshStandardMaterial({ color: canopyColor });
  disposables.push({ geometry: conicalGeometry, material: canopyMaterial });
  disposables.push({ geometry: roundedGeometry, material: canopyMaterial });

  const conicalCount = points.filter((p) => hashToUnit(p.x * 0.1013 + p.y * 0.0721) < 0.5).length;
  const conicalMesh = new THREE.InstancedMesh(conicalGeometry, canopyMaterial, conicalCount);
  const roundedMesh = new THREE.InstancedMesh(roundedGeometry, canopyMaterial, points.length - conicalCount);

  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  const yAxis = new THREE.Vector3(0, 1, 0);
  let trunkIndex = 0;
  let conicalIndex = 0;
  let roundedIndex = 0;
  for (const point of points) {
    const isConical = hashToUnit(point.x * 0.1013 + point.y * 0.0721) < 0.5;
    const { sizeJitter, rotationY } = plantStyle(point, 0);

    const trunkHeight = point.canopyRadius * 1.5 * sizeJitter;
    const trunkRadius = point.canopyRadius * 0.15 * sizeJitter;
    const canopyHeight = point.canopyRadius * 2 * sizeJitter;
    const canopyRadius = point.canopyRadius * sizeJitter;

    matrix.compose(
      new THREE.Vector3(point.x, PLANTING_BASE_Y + trunkHeight / 2, -point.y),
      quaternion.identity(),
      new THREE.Vector3(trunkRadius, trunkHeight, trunkRadius)
    );
    trunkMesh.setMatrixAt(trunkIndex++, matrix);

    matrix.compose(
      new THREE.Vector3(point.x, PLANTING_BASE_Y + trunkHeight + canopyHeight / 2, -point.y),
      quaternion.setFromAxisAngle(yAxis, rotationY),
      new THREE.Vector3(canopyRadius, canopyHeight, canopyRadius)
    );
    if (isConical) conicalMesh.setMatrixAt(conicalIndex++, matrix);
    else roundedMesh.setMatrixAt(roundedIndex++, matrix);
  }
  trunkMesh.instanceMatrix.needsUpdate = true;
  conicalMesh.instanceMatrix.needsUpdate = true;
  roundedMesh.instanceMatrix.needsUpdate = true;

  return [trunkMesh, conicalMesh, roundedMesh];
}

/** Shrubs stay a single sphere geometry/InstancedMesh -- unlike trees, their
 * "shape variety" comes entirely from a non-uniform scale per instance
 * (rounder vs. flatter bush) rather than a second geometry, since a sphere
 * squashed on Y already reads as a distinct silhouette without the extra
 * draw call a real second shape would cost. */
function buildShrubs(points: PlantingPoint3D[], shrubColor: string, disposables: Disposable[]): THREE.Object3D[] {
  if (points.length === 0) return [];

  const geometry = new THREE.SphereGeometry(1, 8, 6);
  const material = new THREE.MeshStandardMaterial({ color: shrubColor });
  const mesh = new THREE.InstancedMesh(geometry, material, points.length);
  disposables.push({ geometry, material });

  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  points.forEach((point, index) => {
    const { sizeJitter, rotationY } = plantStyle(point, 97.4);
    // A second, independent jitter for the vertical squash -- reusing
    // sizeJitter here would make "bigger" and "flatter" always move
    // together, which looks less natural than the two varying separately.
    const squash = 0.75 + hashToUnit(point.x * 0.1013 + point.y * 0.0721 + 141.0) * 0.5;
    const radius = point.canopyRadius * sizeJitter;
    matrix.compose(
      new THREE.Vector3(point.x, PLANTING_BASE_Y + radius * squash, -point.y),
      quaternion.setFromAxisAngle(new THREE.Vector3(0, 1, 0), rotationY),
      new THREE.Vector3(radius, radius * squash, radius)
    );
    mesh.setMatrixAt(index, matrix);
  });
  mesh.instanceMatrix.needsUpdate = true;

  return [mesh];
}

// Remembers where the camera was left, per project -- without this, every
// page refresh (or every switch back to 3D after the 2D map) reframed the
// whole territory from scratch, throwing away whatever angle/zoom the user
// had just set up. Same localStorage mechanism useProjectSession.ts already
// uses for session persistence, just keyed differently (per-project here,
// not a single global key -- see the storageKey prop's own comment).
const CAMERA_STORAGE_PREFIX = "greenproject:camera3d:";

interface StoredCamera {
  position: [number, number, number];
  target: [number, number, number];
}

function isFiniteTriple(value: unknown): value is [number, number, number] {
  return Array.isArray(value) && value.length === 3 && value.every((n) => typeof n === "number" && Number.isFinite(n));
}

function loadStoredCamera(key: string): StoredCamera | null {
  try {
    const raw = localStorage.getItem(CAMERA_STORAGE_PREFIX + key);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<StoredCamera>;
    if (!isFiniteTriple(parsed.position) || !isFiniteTriple(parsed.target)) return null;
    return { position: parsed.position, target: parsed.target };
  } catch {
    return null; // corrupted value or a storage-disabled browser -- just re-frame from bounds instead
  }
}

function saveStoredCamera(key: string, position: THREE.Vector3, target: THREE.Vector3) {
  try {
    const value: StoredCamera = { position: position.toArray(), target: target.toArray() };
    localStorage.setItem(CAMERA_STORAGE_PREFIX + key, JSON.stringify(value));
  } catch {
    // ignore -- losing the remembered angle isn't worth surfacing an error for
  }
}

/** Read-only 3D view of the same plan the 2D map shows (project.layers +
 * plan.features) -- a separate additive scene, not a 3D editor: no
 * selection, no drag, nothing here writes back to the plan. See
 * lib/plan3d.ts for how the GeoJSON is turned into local-meter geometry
 * (including what's deliberately left out -- zoning, utilities, real
 * building heights we don't have). */
export default function ThreeDView({ layers, plan, season = "summer", storageKey }: ThreeDViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [norms, setNorms] = useState<PlantingNorms | undefined>(undefined);
  // Distinct from `norms` itself being set -- without this, the very first
  // render (before the fetch below resolves) would already have `data`
  // computed and the scene-building effect below would mount a full WebGL
  // scene using plan3d.ts's fallback canopy sizes, then immediately tear the
  // whole thing down and rebuild it again once the real norms arrive a
  // moment later (both `data`'s useMemo and the scene effect depend on
  // `norms`). Harmless on a small plan; on tens of thousands of instanced
  // trees/shrubs, building the scene twice measurably doubled time-to-first-frame
  // during this session's own stress test. Gating scene construction on
  // "the fetch attempt is done" (success or failure) means it only ever
  // builds once.
  const [normsReady, setNormsReady] = useState(false);

  useEffect(() => {
    let cancelled = false;
    getPlantingNorms()
      .then((result) => {
        if (!cancelled) setNorms(result);
      })
      .catch(() => {
        // plan3d.ts falls back to its own default canopy radii -- a failed
        // fetch here just means slightly-off tree/shrub sizing, not a broken view.
      })
      .finally(() => {
        if (!cancelled) setNormsReady(true);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const data = useMemo(() => (plan && normsReady ? buildScene3DData(layers, plan, norms) : null), [layers, plan, norms, normsReady]);
  const overItemLimit = (data?.totalItemCount ?? 0) > MAX_3D_ITEMS;

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !data || overItemLimit) return;

    const disposables: Disposable[] = [];
    const palette = SEASON_PALETTES[season];

    const scene = new THREE.Scene();
    // A flat per-season sky color, not a gradient sphere: a custom
    // ShaderMaterial sky sphere was tried and found live to render as a
    // solid black low-poly blob instead of a smooth backdrop under this
    // renderer's logarithmicDepthBuffer setup (adding the standard
    // <logdepthbuf_*> chunks didn't fix it, and the remaining behavior
    // wasn't worth further custom-shader debugging for a purely cosmetic
    // gradient) -- see docs/decision_log.md. This is the same
    // scene.background mechanism the view used before season support, just
    // keyed off the season palette now.
    scene.background = new THREE.Color(palette.skyTop);

    // near=0.5 (not the more typical 0.1) and a far plane sized to the actual
    // scene (not a fixed huge fallback) both matter for a non-logarithmic
    // depth buffer's precision -- see the layer-gap comment above; the two
    // fixes work together, neither alone was enough to stop territory's blue
    // flickering through the layers above it during real testing.
    const camera = new THREE.PerspectiveCamera(55, 1, 0.5, 20_000);
    const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    container.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.1;

    // A hemisphere light (sky color from above, lawn's own seasonal tone
    // bouncing from below) instead of a flat AmbientLight -- costs nothing
    // extra (still a single Light object) but reads as outdoor daylight
    // rather than a uniform studio fill, and ties naturally into the same
    // per-season palette everything else here already uses.
    scene.add(new THREE.HemisphereLight(new THREE.Color(palette.skyTop), new THREE.Color(palette.lawn), 0.75));
    const sun = new THREE.DirectionalLight(0xffffff, 0.9);
    sun.position.set(1, 1.6, 0.6);
    scene.add(sun);

    // Frame the camera around whatever was actually loaded -- plots range
    // from a ~100x80m synthetic square to a real ~1.5x1.5km site (CLAUDE.md),
    // so a fixed camera position would either swallow a small plot or leave
    // a real one's territory a speck.
    const bounds = data.bounds;
    const centerX = bounds ? (bounds.minX + bounds.maxX) / 2 : 0;
    const centerY = bounds ? (bounds.minY + bounds.maxY) / 2 : 0;
    const width = bounds ? bounds.maxX - bounds.minX : 40;
    const depth = bounds ? bounds.maxY - bounds.minY : 40;
    const diagonal = Math.max(Math.hypot(width, depth), 20);
    const cameraDistance = diagonal * 0.7;

    // A camera angle the user already set up (see the OrbitControls "change"
    // listener below) beats re-framing the whole territory from scratch on
    // every reload/re-mount -- restored only when it parses as three finite
    // numbers each; OrbitControls.update() below re-clamps distance-from-target
    // into [minDistance, maxDistance] regardless, so even a corrupted or
    // stale value can't leave the camera somewhere nonsensical.
    const storedCamera = storageKey ? loadStoredCamera(storageKey) : null;
    if (storedCamera) {
      camera.position.set(...storedCamera.position);
      controls.target.set(...storedCamera.target);
    } else {
      camera.position.set(centerX + cameraDistance, cameraDistance * 0.7 + (bounds?.maxBuildingHeight ?? 0), -centerY + cameraDistance);
      controls.target.set(centerX, 0, -centerY);
    }
    camera.far = diagonal * 3 + 100;
    // OrbitControls has no distance limit by default -- mouse-wheel dolly
    // could move the camera farther from target than camera.far allows,
    // silently clipping the entire scene at once (found live: objects
    // vanished when zoomed out far enough). Bounding maxDistance well under
    // camera.far means that can't happen regardless of territory size.
    controls.minDistance = Math.max(diagonal * 0.01, camera.near);
    controls.maxDistance = diagonal * 2.5;
    camera.updateProjectionMatrix();
    controls.update();

    // Fog color matches scene.background exactly (the standard three.js
    // convention for a seamless horizon) -- also usefully hides the ground
    // pad's own outer edge (see padSize below) and the point where distant
    // trees/buildings would otherwise pop rather than fade at maxDistance.
    scene.fog = new THREE.Fog(new THREE.Color(palette.skyTop), diagonal * 0.9, diagonal * 3);

    // Debounced (not on every "change" event, which OrbitControls fires
    // continuously mid-drag/zoom) -- writing localStorage on every animation
    // frame of a drag would be pure waste. 400ms after the user stops moving
    // the camera is early enough to survive an accidental tab close.
    let saveTimeout: ReturnType<typeof setTimeout> | undefined;
    const handleControlsChange = () => {
      if (!storageKey) return;
      clearTimeout(saveTimeout);
      saveTimeout = setTimeout(() => saveStoredCamera(storageKey, camera.position, controls.target), 400);
    };
    controls.addEventListener("change", handleControlsChange);

    // A neutral pad under everything, larger than the territory itself, so
    // there's no visible gap/void around a plot with sparse layers (e.g. no
    // existing_greenery at all).
    const padSize = diagonal * 1.6;
    const padGeometry = new THREE.PlaneGeometry(padSize, padSize);
    const padMaterial = new THREE.MeshStandardMaterial({ color: "#e7e5e4" });
    const pad = new THREE.Mesh(padGeometry, padMaterial);
    pad.rotation.x = -Math.PI / 2;
    pad.position.set(centerX, -0.05, -centerY);
    scene.add(pad);
    disposables.push({ geometry: padGeometry, material: padMaterial });

    const addFlat = (polygons: Polygon3D[], color: string, y: number) => {
      for (const polygon of polygons) {
        const mesh = flatMesh(polygon, color, y);
        if (!mesh) continue;
        scene.add(mesh);
        disposables.push({ geometry: mesh.geometry, material: mesh.material as THREE.Material });
      }
    };
    // Territory is the one layer given real thickness (a thin slab, not a
    // zero-height plane) so it reads as solid ground, top surface at
    // y=TERRITORY_DEPTH_M -- everything else (road/greenery/lawn/buildings/
    // plantings) sits on or above that surface, never inside it.
    for (const polygon of data.territory) {
      const mesh = extrudedMesh(polygon, ZONE_COLORS.territory, TERRITORY_DEPTH_M, 0);
      if (!mesh) continue;
      scene.add(mesh);
      disposables.push({ geometry: mesh.geometry, material: mesh.material as THREE.Material });
    }
    addFlat(data.roads, ZONE_COLORS.road, ROAD_Y);
    addFlat(data.existingGreenery, ZONE_COLORS.existing_greenery, EXISTING_GREENERY_Y);
    for (const polygon of data.lawns) {
      const mesh = lawnMesh(polygon, palette.lawn, LAWN_Y, disposables);
      if (mesh) scene.add(mesh);
    }

    for (const building of data.buildings) {
      const mesh = buildingMesh(building, ZONE_COLORS.building, TERRITORY_DEPTH_M, disposables);
      if (mesh) scene.add(mesh);
    }

    for (const object of buildTrees(data.trees, palette.tree, disposables)) scene.add(object);
    for (const object of buildShrubs(data.shrubs, palette.shrub, disposables)) scene.add(object);

    // A function expression, not `function resize() {}` -- a hoisted
    // declaration would fall outside TypeScript's narrowing of `container`
    // from the guard above (declarations are treated as available from the
    // top of the block, before the narrowing; expressions aren't).
    const resize = () => {
      const { clientWidth, clientHeight } = container;
      if (clientWidth === 0 || clientHeight === 0) return;
      camera.aspect = clientWidth / clientHeight;
      camera.updateProjectionMatrix();
      renderer.setSize(clientWidth, clientHeight);
    };
    resize();
    const resizeObserver = new ResizeObserver(resize);
    resizeObserver.observe(container);

    let frameId = 0;
    const animate = () => {
      frameId = requestAnimationFrame(animate);
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(frameId);
      clearTimeout(saveTimeout);
      controls.removeEventListener("change", handleControlsChange);
      resizeObserver.disconnect();
      controls.dispose();
      for (const { geometry, material, texture } of disposables) {
        geometry.dispose();
        material.dispose();
        texture?.dispose();
      }
      renderer.dispose();
      if (renderer.domElement.parentNode === container) container.removeChild(renderer.domElement);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- overItemLimit is derived from `data`, not an independent input
  }, [data, overItemLimit, season, storageKey]);

  if (!plan) {
    return <div className="flex h-full w-full items-center justify-center text-sm text-stone-500">Нет плана для 3D-просмотра.</div>;
  }
  if (overItemLimit) {
    return (
      <div className="flex h-full w-full items-center justify-center text-sm text-stone-500">
        В плане {(data?.totalItemCount ?? 0).toLocaleString("ru-RU")} посадок — слишком много для 3D-просмотра.
      </div>
    );
  }
  return <div ref={containerRef} className="h-full w-full" />;
}
