"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

import { getPlantingNorms, type GeoJSONFeatureCollection, type PlantingNorms } from "@/lib/api";
import { buildScene3DData, MAX_3D_ITEMS, type Building3D, type Polygon3D } from "@/lib/plan3d";
import { SEASON_PALETTES, ZONE_COLORS, type Season } from "@/lib/mapStyle";

export interface ThreeDViewProps {
  layers?: GeoJSONFeatureCollection;
  plan?: GeoJSONFeatureCollection;
  /** Cosmetic tree/shrub/lawn/sky tint -- see SEASON_PALETTES (mapStyle.ts).
   * A full scene rebuild is cheap enough (~130ms at 33,789 items, measured
   * this session's stress test) that season is just another dependency of
   * the main build effect below, not a separate live-material-update path. */
  season?: Season;
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

/** One InstancedMesh pair (trunk + conical canopy) for every tree, or one
 * InstancedMesh (a squashed sphere) for every shrub -- not a THREE.Mesh per
 * plant. A real-scale plan can hold hundreds of thousands of these (see
 * CLAUDE.md); GPU instancing draws all of them in one draw call each,
 * versus one drawcall (and one JS object) per plant naively. Geometry is
 * built as a unit primitive (radius/height 1) and scaled per-instance via
 * the matrix, so the same two buffers serve every tree regardless of its
 * own canopy radius. */
function buildTrees(
  points: { x: number; y: number; canopyRadius: number }[],
  canopyColor: string,
  disposables: Disposable[]
): THREE.Object3D[] {
  if (points.length === 0) return [];

  const trunkGeometry = new THREE.CylinderGeometry(1, 1, 1, 8);
  const trunkMaterial = new THREE.MeshStandardMaterial({ color: TRUNK_COLOR });
  const trunkMesh = new THREE.InstancedMesh(trunkGeometry, trunkMaterial, points.length);
  disposables.push({ geometry: trunkGeometry, material: trunkMaterial });

  const canopyGeometry = new THREE.ConeGeometry(1, 1, 10);
  const canopyMaterial = new THREE.MeshStandardMaterial({ color: canopyColor });
  const canopyMesh = new THREE.InstancedMesh(canopyGeometry, canopyMaterial, points.length);
  disposables.push({ geometry: canopyGeometry, material: canopyMaterial });

  const matrix = new THREE.Matrix4();
  const identityQuaternion = new THREE.Quaternion();
  points.forEach((point, index) => {
    const trunkHeight = point.canopyRadius * 1.5;
    const trunkRadius = point.canopyRadius * 0.15;
    const canopyHeight = point.canopyRadius * 2;

    matrix.compose(
      new THREE.Vector3(point.x, PLANTING_BASE_Y + trunkHeight / 2, -point.y),
      identityQuaternion,
      new THREE.Vector3(trunkRadius, trunkHeight, trunkRadius)
    );
    trunkMesh.setMatrixAt(index, matrix);

    matrix.compose(
      new THREE.Vector3(point.x, PLANTING_BASE_Y + trunkHeight + canopyHeight / 2, -point.y),
      identityQuaternion,
      new THREE.Vector3(point.canopyRadius, canopyHeight, point.canopyRadius)
    );
    canopyMesh.setMatrixAt(index, matrix);
  });
  trunkMesh.instanceMatrix.needsUpdate = true;
  canopyMesh.instanceMatrix.needsUpdate = true;

  return [trunkMesh, canopyMesh];
}

function buildShrubs(
  points: { x: number; y: number; canopyRadius: number }[],
  shrubColor: string,
  disposables: Disposable[]
): THREE.Object3D[] {
  if (points.length === 0) return [];

  const geometry = new THREE.SphereGeometry(1, 8, 6);
  const material = new THREE.MeshStandardMaterial({ color: shrubColor });
  const mesh = new THREE.InstancedMesh(geometry, material, points.length);
  disposables.push({ geometry, material });

  const matrix = new THREE.Matrix4();
  const identityQuaternion = new THREE.Quaternion();
  points.forEach((point, index) => {
    matrix.compose(
      new THREE.Vector3(point.x, PLANTING_BASE_Y + point.canopyRadius, -point.y),
      identityQuaternion,
      new THREE.Vector3(point.canopyRadius, point.canopyRadius, point.canopyRadius)
    );
    mesh.setMatrixAt(index, matrix);
  });
  mesh.instanceMatrix.needsUpdate = true;

  return [mesh];
}

/** Read-only 3D view of the same plan the 2D map shows (project.layers +
 * plan.features) -- a separate additive scene, not a 3D editor: no
 * selection, no drag, nothing here writes back to the plan. See
 * lib/plan3d.ts for how the GeoJSON is turned into local-meter geometry
 * (including what's deliberately left out -- zoning, utilities, real
 * building heights we don't have). */
export default function ThreeDView({ layers, plan, season = "summer" }: ThreeDViewProps) {
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

    scene.add(new THREE.AmbientLight(0xffffff, 0.7));
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
    camera.position.set(centerX + cameraDistance, cameraDistance * 0.7 + (bounds?.maxBuildingHeight ?? 0), -centerY + cameraDistance);
    controls.target.set(centerX, 0, -centerY);
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
    addFlat(data.lawns, palette.lawn, LAWN_Y);

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
  }, [data, overItemLimit, season]);

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
