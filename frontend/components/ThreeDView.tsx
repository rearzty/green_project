"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { mergeGeometries } from "three/examples/jsm/utils/BufferGeometryUtils.js";

import { getPlantingNorms, type GeoJSONFeatureCollection, type PlantingNorms } from "@/lib/api";
import { buildScene3DData, hashToUnit, MAX_3D_ITEMS, walkStops, type Building3D, type PlantingPoint3D, type Polygon3D } from "@/lib/plan3d";
import { lookOf, type CrownForm, type PlantLook } from "@/lib/plantLook";
import { applySceneTheme, SEASON_PALETTES, type SceneTheme, type Season } from "@/lib/mapStyle";
import { PLANTING_TYPE_LABELS } from "@/lib/format";

/** Ракурсы камеры (components/map/CameraControls3D.tsx): общий вид, сверху и
 * обход участков посадок вплотную (`walk-next`/`walk-prev`). */
export type CameraPose = "overview" | "top" | "walk-next" | "walk-prev";

/** Где камера в обходе: номер участка (с 1) и сколько их всего; 0 — обход не начат. */
export interface WalkState {
  index: number;
  total: number;
}

/** Камера обхода: за участком, на этом расстоянии по горизонтали и на этой высоте
 * над землёй. С такой точки видно ~60 м улицы вперёд, а у дерева различим ствол. */
const WALK_BACK_M = 26;
const WALK_HEIGHT_M = 13;
const WALK_TARGET_HEIGHT_M = 2;
/** Двойной щелчок по земле: подлёт к месту — столько метров от точки, под таким
 * углом возвышения. */
const FLY_TO_DISTANCE_M = 30;
const FLY_TO_ELEVATION = 0.55;
/** Щелчок по земле дальше этого от любой посадки — в пустоту не летим. */
const FLY_TO_PLANT_REACH_M = 15;
/** Ближе этого к цели камера не подходит: ниже уже стена из веток, а дальше
 * камеры лежит ближняя плоскость отсечения. */
const MIN_CAMERA_DISTANCE_M = 2;

export interface CameraCommand {
  pose: CameraPose;
  /** Растёт с каждым нажатием: повторный выбор того же ракурса — тоже команда. */
  seq: number;
}

export interface ThreeDViewProps {
  layers?: GeoJSONFeatureCollection;
  plan?: GeoJSONFeatureCollection;
  /** See MapView.tsx's own crsVerified doc -- when false, `layers`/`plan`
   * carry raw local metres, not WGS84 degrees, and buildScene3DData must
   * skip its usual equirectangular approximation (see plan3d.ts's
   * makeLocalProjector) or it'll scale those metres by a bogus
   * cos(latitude) factor. */
  crsVerified?: boolean;
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
  /** Оформление сцены: светлый день или тёмный «Графит» под интерфейс. */
  sceneTheme?: SceneTheme;
  /** Перелёт камеры в выбранный ракурс. Не пересобирает сцену. */
  cameraCommand?: CameraCommand | null;
  /** Медленный облёт вокруг участка. */
  autoRotate?: boolean;
  /** Сообщает, на каком участке обхода камера (для подписи «3 из 24»). */
  onWalkChange?: (state: WalkState) => void;
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

const BUILDING_ROOF_COLOR = "#8e8c88";

/** Анизотропная фильтрация для текстур окон и газона: без неё окна здания на
 * расстоянии рябят муаром. Выставляется один раз при создании рендерера (он
 * знает предел видеокарты), текстуры читают значение при создании. */
let textureAnisotropy = 1;

/** Тени считаются только на сцене не крупнее этого числа посадок: теневая карта —
 * ещё один проход по всем экземплярам, а на реальном масштабе (сотни тысяч
 * посадок на участке в километр) разрешения одной карты всё равно не хватило бы
 * на читаемую тень. Выше порога сцена рисуется без теней, как раньше. */
const SHADOW_ITEM_LIMIT = 80_000;

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
  texture.anisotropy = textureAnisotropy;
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
  wallTexture.anisotropy = textureAnisotropy;
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
    color: new THREE.Color().setHSL(building.hash01, 0.06, 0.58),
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

/** Случайные, но воспроизводимые отличия одного растения от другого — из его
 * же координат через hashToUnit (plan3d.ts): один и тот же план рисуется
 * одинаково на любой пересборке сцены (смена сезона, повторная загрузка), и
 * отдельного источника случайности, который надо синхронизировать, нет. */
interface PlantJitter {
  size: number;
  height: number;
  rotationY: number;
  hue: number;
  saturation: number;
  lightness: number;
}

function plantJitter(point: PlantingPoint3D): PlantJitter {
  const seed = point.x * 0.1013 + point.y * 0.0721;
  return {
    size: 0.9 + hashToUnit(seed + 31.7) * 0.2,
    height: 0.9 + hashToUnit(seed + 77.3) * 0.2,
    rotationY: hashToUnit(seed + 58.2) * Math.PI * 2,
    hue: (hashToUnit(seed + 12.9) - 0.5) * 0.03,
    saturation: (hashToUnit(seed + 44.1) - 0.5) * 0.12,
    lightness: (hashToUnit(seed + 91.6) - 0.5) * 0.12,
  };
}

/** Сколько полигонов позволить на крону при таком числе экземпляров одной формы.
 * Крона из пяти шаров детализации 1 — ~400 треугольников; на плане в 20 тысяч
 * деревьев это 8 млн, терпимо, а на реальном масштабе (сотни тысяч посадок,
 * см. MAX_3D_ITEMS) — десятки миллионов и слайд-шоу на обычной видеокарте.
 * Поэтому детализация падает ступенями: 1 (≈400 треугольников) → 0 (≈100) →
 * -1 (один шар, 20): силуэт леса при этом остаётся, а число экземпляров не режется. */
function crownDetail(instances: number): number {
  if (instances <= 25_000) return 1;
  if (instances <= 120_000) return 0;
  return -1;
}

/** Единичная геометрия кроны, вписанная в [-1, 1] по всем осям: экземпляр
 * растягивается до своей ширины и высоты матрицей, так что одна геометрия
 * обслуживает кроны любого размера. Крона «lobed» — несколько сросшихся шаров:
 * гладкий эллипсоид на стволе читается как леденец на палочке, а так — как
 * дерево. Низкополигональная (икосаэдр, детализация 1) намеренно: сотни тысяч
 * экземпляров, и плоское затенение даёт стилизованный, а не размытый вид. */
function crownGeometry(form: CrownForm, detail: number): THREE.BufferGeometry {
  if (form === "cone") return new THREE.ConeGeometry(1, 2, detail > 0 ? 9 : 6);
  if (form === "oval" || detail < 0) return new THREE.IcosahedronGeometry(1, Math.max(detail, 0));
  const lobes: [number, number, number, number][] =
    form === "bush"
      ? [
          [0, 0, 0, 0.8],
          [0.55, -0.2, 0.1, 0.55],
          [-0.5, -0.15, -0.2, 0.58],
          [0.05, 0.1, 0.55, 0.5],
        ]
      : [
          [0, 0, 0, 0.85],
          [0.5, -0.15, 0.2, 0.6],
          [-0.45, -0.1, 0.35, 0.58],
          [0.1, 0.3, -0.5, 0.6],
          [-0.2, 0.45, 0.1, 0.5],
        ];
  const parts = lobes.map(([x, y, z, r]) => {
    const part = new THREE.IcosahedronGeometry(r, detail);
    part.translate(x, y, z);
    return part;
  });
  const merged = mergeGeometries(parts)!;
  parts.forEach((part) => part.dispose());
  merged.computeBoundingBox();
  const box = merged.boundingBox!;
  const halfWidth = Math.max(Math.abs(box.min.x), box.max.x, Math.abs(box.min.z), box.max.z);
  const halfHeight = Math.max(Math.abs(box.min.y), box.max.y);
  merged.scale(1 / halfWidth, 1 / halfHeight, 1 / halfWidth);
  return merged;
}

interface PlantEntry {
  point: PlantingPoint3D;
  look: PlantLook;
  jitter: PlantJitter;
}

function tint(base: string, jitter: PlantJitter): THREE.Color {
  return new THREE.Color(base).offsetHSL(jitter.hue, jitter.saturation, jitter.lightness);
}

/** Деревья и кусты одним проходом: форма, размер и цвет каждого — по его породе
 * (lib/plantLook.ts), диаметр кроны — реальный из плана. Один `InstancedMesh` на
 * форму кроны плюс один на стволы, цвет — на экземпляр (`setColorAt`), так что
 * число вызовов отрисовки не зависит ни от числа посадок, ни от числа пород
 * (раньше: 3 на все деревья + 1 на кусты, и все одного цвета). */
function buildPlants(points: PlantingPoint3D[], plantingType: "tree" | "shrub", season: Season, disposables: Disposable[]): THREE.Object3D[] {
  if (points.length === 0) return [];

  const byForm = new Map<CrownForm, PlantEntry[]>();
  const trunked: PlantEntry[] = [];
  for (const point of points) {
    const look = lookOf(point.species, plantingType, point.crownClass);
    const entry: PlantEntry = { point, look, jitter: plantJitter(point) };
    const bucket = byForm.get(look.form);
    if (bucket) bucket.push(entry);
    else byForm.set(look.form, [entry]);
    if (look.trunkShare > 0) trunked.push(entry);
  }

  const objects: THREE.Object3D[] = [];
  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  const yAxis = new THREE.Vector3(0, 1, 0);
  const position = new THREE.Vector3();
  const scale = new THREE.Vector3();

  // Стволы: усечённый конус, цвет по породе (у берёзы белый).
  if (trunked.length > 0) {
    const geometry = new THREE.CylinderGeometry(0.6, 1, 1, 6);
    geometry.translate(0, 0.5, 0);
    const material = new THREE.MeshStandardMaterial({ color: 0xffffff, roughness: 1 });
    const mesh = new THREE.InstancedMesh(geometry, material, trunked.length);
    disposables.push({ geometry, material });
    trunked.forEach(({ point, look, jitter }, index) => {
      const diameter = point.canopyRadius * 2 * jitter.size;
      const height = diameter * look.heightRatio * jitter.height;
      const trunkHeight = height * look.trunkShare;
      const radius = Math.max(diameter * 0.045, 0.1);
      matrix.compose(position.set(point.x, PLANTING_BASE_Y, -point.y), quaternion.identity(), scale.set(radius, trunkHeight, radius));
      mesh.setMatrixAt(index, matrix);
      mesh.setColorAt(index, new THREE.Color(look.trunkColor).offsetHSL(0, 0, jitter.lightness * 0.5));
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.userData.castShadow = true;
    mesh.userData.entries = trunked;
    objects.push(mesh);
  }

  for (const [form, entries] of byForm) {
    const geometry = crownGeometry(form, crownDetail(entries.length));
    const material = new THREE.MeshStandardMaterial({ color: 0xffffff, flatShading: true, roughness: 0.95, metalness: 0 });
    const mesh = new THREE.InstancedMesh(geometry, material, entries.length);
    disposables.push({ geometry, material });
    entries.forEach(({ point, look, jitter }, index) => {
      const diameter = point.canopyRadius * 2 * jitter.size;
      const height = diameter * look.heightRatio * jitter.height;
      const trunkHeight = height * look.trunkShare;
      // Крона начинается чуть ниже верха ствола — иначе между ними виден зазор.
      const crownBottom = trunkHeight * 0.85;
      const crownHeight = Math.max(height - crownBottom, diameter * 0.3);
      // Кусты и можжевельник чуть заглублены, чтобы не висели над газоном.
      const centerY = PLANTING_BASE_Y + crownBottom + crownHeight / 2 - (look.trunkShare === 0 ? crownHeight * 0.06 : 0);
      matrix.compose(
        position.set(point.x, centerY, -point.y),
        quaternion.setFromAxisAngle(yAxis, jitter.rotationY),
        scale.set(diameter / 2, crownHeight / 2, diameter / 2)
      );
      mesh.setMatrixAt(index, matrix);
      mesh.setColorAt(index, tint(look.colors[season], jitter));
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.userData.castShadow = true;
    mesh.userData.entries = entries;
    objects.push(mesh);
  }
  return objects;
}

/** Что показывает подсказка при наведении на дерево или куст. Размер кроны —
 * данные плана; высота — оценка по породе (см. plantLook.ts), и подпись «≈» это
 * прямо говорит. */
interface PlantHover {
  x: number;
  y: number;
  title: string;
  subtitle: string;
}

function plantHoverText(entry: PlantEntry, plantingType: "tree" | "shrub"): { title: string; subtitle: string } {
  const diameter = entry.point.canopyRadius * 2;
  const height = diameter * entry.look.heightRatio;
  const fmt = (m: number) => m.toLocaleString("ru-RU", { maximumFractionDigits: 1 });
  return {
    title: entry.point.species ?? PLANTING_TYPE_LABELS[plantingType],
    subtitle: `${PLANTING_TYPE_LABELS[plantingType]} · крона ${fmt(diameter)} м · высота ≈ ${fmt(height)} м`,
  };
}

/** Подсказки не считаются на очень больших планах: луч проверяет каждый экземпляр
 * (в three у InstancedMesh нет пространственного индекса), а подсказка — удобство,
 * не повод ронять частоту кадров. */
const HOVER_INSTANCE_LIMIT = 60_000;
const HOVER_DELAY_MS = 90;
const FLIGHT_MS = 900;

/** Небо — вертикальный градиент канвас-текстурой как `scene.background`, а не
 * шейдерная сфера: сфера с собственным ShaderMaterial уже пробовалась и под
 * logarithmicDepthBuffer рисовалась чёрным многоугольником (см. комментарий о
 * небе ниже), а фоновая текстура рисуется штатным фоном three без глубины. */
function skyTexture(top: string, bottom: string): THREE.CanvasTexture {
  const canvas = document.createElement("canvas");
  canvas.width = 2;
  canvas.height = 256;
  const ctx = canvas.getContext("2d")!;
  const gradient = ctx.createLinearGradient(0, 0, 0, 256);
  gradient.addColorStop(0, top);
  gradient.addColorStop(1, bottom);
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, 2, 256);
  const texture = new THREE.CanvasTexture(canvas);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
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
export default function ThreeDView({ layers, plan, crsVerified, season = "summer", storageKey, sceneTheme = "day", cameraCommand = null, autoRotate = false, onWalkChange }: ThreeDViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<PlantHover | null>(null);
  // Что нужно командам камеры и облёту, чтобы не пересобирать сцену ради них:
  // эффект сцены кладёт сюда живые камеру и управление.
  const viewRef = useRef<{ flyTo: (pose: CameraPose) => void; controls: OrbitControls } | null>(null);
  const autoRotateRef = useRef(autoRotate);
  autoRotateRef.current = autoRotate;
  // Колбэк живёт дольше рендера (его зовёт обработчик внутри эффекта сцены) —
  // через реф, чтобы не пересобирать сцену из-за новой функции на каждый рендер.
  const onWalkChangeRef = useRef(onWalkChange);
  onWalkChangeRef.current = onWalkChange;
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

  const data = useMemo(
    () => (plan && normsReady ? buildScene3DData(layers, plan, norms, crsVerified ?? true) : null),
    [layers, plan, norms, normsReady, crsVerified]
  );
  const overItemLimit = (data?.totalItemCount ?? 0) > MAX_3D_ITEMS;

  useEffect(() => {
    const container = containerRef.current;
    if (!container || !data || overItemLimit) return;

    const disposables: Disposable[] = [];
    const palette = applySceneTheme(SEASON_PALETTES[season], sceneTheme);

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
    const sky = skyTexture(palette.skyTop, palette.skyBottom);
    scene.background = sky;

    // near=0.5 (not the more typical 0.1) and a far plane sized to the actual
    // scene (not a fixed huge fallback) both matter for a non-logarithmic
    // depth buffer's precision -- see the layer-gap comment above; the two
    // fixes work together, neither alone was enough to stop territory's blue
    // flickering through the layers above it during real testing.
    const camera = new THREE.PerspectiveCamera(55, 1, 0.5, 20_000);
    const renderer = new THREE.WebGLRenderer({ antialias: true, logarithmicDepthBuffer: true });
    renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
    textureAnisotropy = Math.min(8, renderer.capabilities.getMaxAnisotropy());
    // Тени — только на сцене, где их стоит считать (см. SHADOW_ITEM_LIMIT).
    const shadows = data.totalItemCount <= SHADOW_ITEM_LIMIT;
    if (shadows) {
      renderer.shadowMap.enabled = true;
      renderer.shadowMap.type = THREE.PCFShadowMap;
    }
    container.appendChild(renderer.domElement);

    const controls = new OrbitControls(camera, renderer.domElement);
    controls.enableDamping = true;
    controls.dampingFactor = 0.1;
    // Колесо приближает к тому, что под курсором, а не к центру участка: на улице
    // в километр иначе к нужному дереву не подойти без долгой панорамы.
    controls.zoomToCursor = true;
    // Под землю не заглянуть: у «пола» сцены нет нижней грани.
    controls.maxPolarAngle = Math.PI / 2 - 0.03;

    // A hemisphere light (sky color from above, lawn's own seasonal tone
    // bouncing from below) instead of a flat AmbientLight -- costs nothing
    // extra (still a single Light object) but reads as outdoor daylight
    // rather than a uniform studio fill, and ties naturally into the same
    // per-season palette everything else here already uses.
    const graphite = sceneTheme === "graphite";
    scene.add(new THREE.HemisphereLight(new THREE.Color(graphite ? "#8fa0b5" : palette.skyBottom), new THREE.Color(palette.lawn), graphite ? 1.0 : 1.5));
    // Силы света подобраны под физически корректное освещение three (после r155
    // интенсивность 1 — заметно темнее прежнего «условного» света).
    const sun = new THREE.DirectionalLight(sceneTheme === "graphite" ? 0xe3ecff : 0xfff4e0, sceneTheme === "graphite" ? 2.0 : 2.4);
    scene.add(sun);
    scene.add(sun.target);

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

    // Солнце — с юго-запада и повыше, тень падает на северо-восток. Теневая камера
    // охватывает всю сцену (ортографическая, по диагонали участка): одна карта
    // 4096² на сцену до порога SHADOW_ITEM_LIMIT даёт на дворе в сотню метров
    // сантиметровое разрешение.
    sun.target.position.set(centerX, 0, -centerY);
    sun.position.set(centerX + diagonal * 0.8, diagonal * 1.2, -centerY + diagonal * 0.5);
    if (shadows) {
      sun.castShadow = true;
      sun.shadow.mapSize.set(4096, 4096);
      const reach = diagonal * 0.65;
      Object.assign(sun.shadow.camera, { left: -reach, right: reach, top: reach, bottom: -reach, near: 1, far: diagonal * 4 });
      sun.shadow.camera.updateProjectionMatrix();
      sun.shadow.bias = -0.0004;
      sun.shadow.normalBias = 0.3;
    }
    // OrbitControls has no distance limit by default -- mouse-wheel dolly
    // could move the camera farther from target than camera.far allows,
    // silently clipping the entire scene at once (found live: objects
    // vanished when zoomed out far enough). Bounding maxDistance well under
    // camera.far means that can't happen regardless of territory size.
    controls.minDistance = MIN_CAMERA_DISTANCE_M;
    controls.maxDistance = diagonal * 2.5;
    camera.updateProjectionMatrix();
    controls.update();

    // Fog color matches scene.background exactly (the standard three.js
    // convention for a seamless horizon) -- also usefully hides the ground
    // pad's own outer edge (see padSize below) and the point where distant
    // trees/buildings would otherwise pop rather than fade at maxDistance.
    // Цвет тумана — цвет неба у горизонта: при взгляде сверху горизонт у верхнего
    // края экрана, где градиент ещё близок к skyTop.
    scene.fog = new THREE.Fog(new THREE.Color(palette.skyTop).lerp(new THREE.Color(palette.skyBottom), 0.25), diagonal * 0.8, diagonal * 2.2);

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
    // Пол больше, чем всё, что видно сквозь туман (fog far = 2.2·diagonal): край пола
    // не должен виднеться на фоне неба.
    const padSize = diagonal * 6;
    const padGeometry = new THREE.PlaneGeometry(padSize, padSize);
    const padMaterial = new THREE.MeshStandardMaterial({ color: palette.pad });
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
      const mesh = extrudedMesh(polygon, palette.ground, TERRITORY_DEPTH_M, 0);
      if (!mesh) continue;
      scene.add(mesh);
      disposables.push({ geometry: mesh.geometry, material: mesh.material as THREE.Material });
    }
    addFlat(data.roads, palette.road, ROAD_Y);
    addFlat(data.existingGreenery, palette.greenery, EXISTING_GREENERY_Y);
    for (const polygon of data.lawns) {
      const mesh = lawnMesh(polygon, palette.lawn, LAWN_Y, disposables);
      if (mesh) scene.add(mesh);
    }

    // Здания — ещё и преграда для луча двойного щелчка (иначе луч уходил сквозь
    // стену в землю за домом и камера подлетала к стене).
    const buildingMeshes: THREE.Mesh[] = [];
    for (const building of data.buildings) {
      const mesh = buildingMesh(building, graphite ? "#5b615c" : BUILDING_ROOF_COLOR, TERRITORY_DEPTH_M, disposables);
      if (mesh) {
        mesh.userData.castShadow = true;
        scene.add(mesh);
        buildingMeshes.push(mesh);
      }
    }

    // Деревья и кусты, по которым наводится подсказка: меши с `userData.entries`.
    const plantMeshes: { mesh: THREE.InstancedMesh; plantingType: "tree" | "shrub" }[] = [];
    for (const [points, plantingType] of [[data.trees, "tree"], [data.shrubs, "shrub"]] as const) {
      for (const object of buildPlants(points, plantingType, season, disposables)) {
        scene.add(object);
        if (object instanceof THREE.InstancedMesh) plantMeshes.push({ mesh: object, plantingType });
      }
    }

    // Тени: принимает всё, отбрасывают здания и растения (userData.castShadow).
    scene.traverse((object) => {
      if (!(object instanceof THREE.Mesh)) return;
      object.receiveShadow = shadows;
      object.castShadow = shadows && Boolean(object.userData.castShadow);
    });

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

    // --- ракурсы камеры -------------------------------------------------
    // Общий вид — тот же кадр, что и при первой загрузке; «сверху» — почти
    // отвесно (небольшой наклон, чтобы OrbitControls не терял ось «вверх»).
    // Обход — вплотную к участкам посадок, см. walkPose.
    const poseOf = (pose: CameraPose): { position: THREE.Vector3; target: THREE.Vector3 } => {
      if (pose === "top") {
        return { position: new THREE.Vector3(centerX, diagonal * 1.05, -centerY + diagonal * 0.02), target: new THREE.Vector3(centerX, 0, -centerY) };
      }
      return {
        position: new THREE.Vector3(centerX + cameraDistance, cameraDistance * 0.7 + (bounds?.maxBuildingHeight ?? 0), -centerY + cameraDistance),
        target: new THREE.Vector3(centerX, 0, -centerY),
      };
    };
    const stops = walkStops([...data.trees, ...data.shrubs]);
    let walkIndex = -1;
    const walkPose = (index: number): { position: THREE.Vector3; target: THREE.Vector3 } => {
      const stop = stops[index];
      return {
        target: new THREE.Vector3(stop.x, WALK_TARGET_HEIGHT_M, -stop.y),
        // Камера за участком, лицом по ходу движения; ось z сцены — минус y плана.
        position: new THREE.Vector3(stop.x - stop.heading[0] * WALK_BACK_M, WALK_HEIGHT_M, -(stop.y - stop.heading[1] * WALK_BACK_M)),
      };
    };
    onWalkChangeRef.current?.({ index: 0, total: stops.length });
    let flight: { fromPosition: THREE.Vector3; fromTarget: THREE.Vector3; toPosition: THREE.Vector3; toTarget: THREE.Vector3; startedAt: number } | null = null;
    viewRef.current = {
      controls,
      flyTo(pose) {
        let next: { position: THREE.Vector3; target: THREE.Vector3 };
        if (pose === "walk-next" || pose === "walk-prev") {
          if (stops.length === 0) return;
          // Первое нажатие «вперёд» — первый участок, «назад» — последний; дальше по кругу.
          walkIndex = walkIndex < 0 ? (pose === "walk-next" ? 0 : stops.length - 1) : (walkIndex + (pose === "walk-next" ? 1 : -1) + stops.length) % stops.length;
          onWalkChangeRef.current?.({ index: walkIndex + 1, total: stops.length });
          next = walkPose(walkIndex);
        } else {
          walkIndex = -1;
          onWalkChangeRef.current?.({ index: 0, total: stops.length });
          next = poseOf(pose);
        }
        flight = { fromPosition: camera.position.clone(), fromTarget: controls.target.clone(), toPosition: next.position, toTarget: next.target, startedAt: performance.now() };
      },
    };
    controls.autoRotate = autoRotateRef.current;
    controls.autoRotateSpeed = 1.2;

    // --- двойной щелчок: подлететь к месту ------------------------------------
    // Точка — первое растение или здание под курсором, а если там пусто, то земля (плоскость
    // y=0). Азимут взгляда сохраняется: пользователь не теряет, где у него север,
    // меняются только расстояние и наклон. Ближайший участок обхода становится
    // текущим, чтобы ‹ › продолжили путь оттуда, где человек оказался.
    const ground = new THREE.Plane(new THREE.Vector3(0, 1, 0), 0);
    const flyRaycaster = new THREE.Raycaster();
    const flyPointer = new THREE.Vector2();
    const handleDoubleClick = (event: MouseEvent) => {
      const rect = renderer.domElement.getBoundingClientRect();
      flyPointer.set(((event.clientX - rect.left) / rect.width) * 2 - 1, -((event.clientY - rect.top) / rect.height) * 2 + 1);
      flyRaycaster.setFromCamera(flyPointer, camera);
      // На очень больших планах растения под курсором не ищем (как и в подсказке): луч проверяет каждый экземпляр.
      const candidates: THREE.Object3D[] = [...buildingMeshes];
      if (plantInstances <= HOVER_INSTANCE_LIMIT) candidates.push(...plantMeshes.map(({ mesh }) => mesh));
      const solidHit = flyRaycaster.intersectObjects(candidates, false)[0];
      const point = solidHit ? solidHit.point.clone() : flyRaycaster.ray.intersectPlane(ground, new THREE.Vector3());
      if (!point) return; // луч ушёл в небо
      point.y = Math.min(Math.max(point.y, 0), WALK_TARGET_HEIGHT_M);
      let nearest = -1;
      let nearestDistance = Infinity;
      stops.forEach((stop, index) => {
        const d = Math.hypot(stop.x - point.x, -stop.y - point.z);
        if (d < nearestDistance) {
          nearestDistance = d;
          nearest = index;
        }
      });
      // Щёлкнули по пустой земле вдали от посадок: лететь в пустоту незачем —
      // подлетаем к ближайшему участку с посадками. «Вдали» меряем до ближайшей
      // самой посадки, а не до центра участка: участок в 40 м, и до его центра от
      // крайних кустов бывает далеко.
      let nearestPlantSq = Infinity;
      for (const list of [data.trees, data.shrubs]) {
        for (const plant of list) {
          const dx = plant.x - point.x;
          const dz = -plant.y - point.z;
          const d2 = dx * dx + dz * dz;
          if (d2 < nearestPlantSq) nearestPlantSq = d2;
        }
      }
      if (!solidHit && nearest >= 0 && nearestPlantSq > FLY_TO_PLANT_REACH_M * FLY_TO_PLANT_REACH_M) {
        walkIndex = nearest;
        onWalkChangeRef.current?.({ index: nearest + 1, total: stops.length });
        const target = walkPose(nearest);
        flight = { fromPosition: camera.position.clone(), fromTarget: controls.target.clone(), toPosition: target.position, toTarget: target.target, startedAt: performance.now() };
        return;
      }
      const away = new THREE.Vector3(camera.position.x - controls.target.x, 0, camera.position.z - controls.target.z);
      if (away.lengthSq() < 1e-6) away.set(1, 0, 1);
      away.normalize();
      const distance = Math.max(Math.min(FLY_TO_DISTANCE_M, camera.position.distanceTo(point)), 6);
      const toPosition = point.clone().addScaledVector(away, distance * Math.cos(FLY_TO_ELEVATION));
      toPosition.y += distance * Math.sin(FLY_TO_ELEVATION);
      flight = { fromPosition: camera.position.clone(), fromTarget: controls.target.clone(), toPosition, toTarget: point, startedAt: performance.now() };
      if (nearest >= 0) {
        walkIndex = nearest;
        onWalkChangeRef.current?.({ index: nearest + 1, total: stops.length });
      }
    };
    renderer.domElement.addEventListener("dblclick", handleDoubleClick);

    // --- подсказка при наведении на дерево/куст ----------------------------
    const plantInstances = plantMeshes.reduce((sum, { mesh }) => sum + mesh.count, 0);
    const raycaster = new THREE.Raycaster();
    const pointer = new THREE.Vector2();
    let hoverTimer: ReturnType<typeof setTimeout> | undefined;
    const handlePointerMove = (event: PointerEvent) => {
      clearTimeout(hoverTimer);
      // Тянут камеру или облёт идёт — подсказка не нужна и мешала бы.
      if (event.buttons !== 0 || controls.autoRotate) {
        setHover(null);
        return;
      }
      if (plantInstances > HOVER_INSTANCE_LIMIT) return;
      const rect = renderer.domElement.getBoundingClientRect();
      const x = event.clientX - rect.left;
      const y = event.clientY - rect.top;
      hoverTimer = setTimeout(() => {
        pointer.set((x / rect.width) * 2 - 1, -(y / rect.height) * 2 + 1);
        raycaster.setFromCamera(pointer, camera);
        const hit = raycaster.intersectObjects(plantMeshes.map(({ mesh }) => mesh), false)[0];
        const entry = hit && hit.instanceId !== undefined ? (hit.object.userData.entries as PlantEntry[] | undefined)?.[hit.instanceId] : undefined;
        if (!hit || !entry) {
          setHover(null);
          return;
        }
        const plantingType = plantMeshes.find(({ mesh }) => mesh === hit.object)?.plantingType ?? "tree";
        setHover({ x, y, ...plantHoverText(entry, plantingType) });
      }, HOVER_DELAY_MS);
    };
    const hideHover = () => {
      clearTimeout(hoverTimer);
      setHover(null);
    };
    renderer.domElement.addEventListener("pointermove", handlePointerMove);
    renderer.domElement.addEventListener("pointerleave", hideHover);
    renderer.domElement.addEventListener("pointerdown", hideHover);

    let frameId = 0;
    const animate = () => {
      frameId = requestAnimationFrame(animate);
      if (flight) {
        const k = Math.min((performance.now() - flight.startedAt) / FLIGHT_MS, 1);
        const eased = k < 0.5 ? 2 * k * k : 1 - Math.pow(-2 * k + 2, 2) / 2;
        camera.position.lerpVectors(flight.fromPosition, flight.toPosition, eased);
        controls.target.lerpVectors(flight.fromTarget, flight.toTarget, eased);
        if (k >= 1) flight = null;
      }
      controls.update();
      renderer.render(scene, camera);
    };
    animate();

    return () => {
      cancelAnimationFrame(frameId);
      clearTimeout(saveTimeout);
      clearTimeout(hoverTimer);
      renderer.domElement.removeEventListener("dblclick", handleDoubleClick);
      renderer.domElement.removeEventListener("pointermove", handlePointerMove);
      renderer.domElement.removeEventListener("pointerleave", hideHover);
      renderer.domElement.removeEventListener("pointerdown", hideHover);
      viewRef.current = null;
      setHover(null);
      controls.removeEventListener("change", handleControlsChange);
      resizeObserver.disconnect();
      controls.dispose();
      for (const { geometry, material, texture } of disposables) {
        geometry.dispose();
        material.dispose();
        texture?.dispose();
      }
      sky.dispose();
      renderer.dispose();
      if (renderer.domElement.parentNode === container) container.removeChild(renderer.domElement);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps -- overItemLimit is derived from `data`, not an independent input
  }, [data, overItemLimit, season, sceneTheme, storageKey]);

  // Команда ракурса: перелёт без пересборки сцены. seq в зависимостях — повторное
  // нажатие того же ракурса тоже должно сработать.
  useEffect(() => {
    if (cameraCommand) viewRef.current?.flyTo(cameraCommand.pose);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [cameraCommand?.seq]);
  useEffect(() => {
    if (viewRef.current) viewRef.current.controls.autoRotate = autoRotate;
    if (autoRotate) setHover(null);
  }, [autoRotate]);

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
  return (
    <div className="relative h-full w-full">
      <div ref={containerRef} className="h-full w-full" />
      {hover && (
        <div
          role="tooltip"
          style={{ left: Math.min(hover.x + 14, (containerRef.current?.clientWidth ?? 9999) - 250), top: hover.y + 14 }}
          className="pointer-events-none absolute z-10 w-[240px] rounded-lg border border-gp-border bg-gp-float px-3 py-2 text-gp-text shadow-[0_10px_28px_rgba(0,0,0,.45)]"
        >
          <p className="m-0 text-sm font-medium">{hover.title}</p>
          <p className="m-0 mt-0.5 text-xs text-gp-text-2">{hover.subtitle}</p>
        </div>
      )}
    </div>
  );
}
