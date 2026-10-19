"use client";

import { useEffect, useMemo, useRef, useState } from "react";
import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";
import { mergeGeometries, mergeVertices } from "three/examples/jsm/utils/BufferGeometryUtils.js";

import { getPlantingNorms, type GeoJSONFeatureCollection, type PlantingNorms } from "@/lib/api";
import { buildScene3DData, hashToUnit, MAX_3D_ITEMS, walkStops, buildingCollider, pointInRing, type Building3D, type PlantingPoint3D, type Polygon3D } from "@/lib/plan3d";
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

/** Вид от первого лица: рост наблюдателя, поле зрения, скорости. Идти по улице в
 * километр шагом 1,4 м/с слишком долго, поэтому обычная ходьба быстрее реальной,
 * а с Shift — бегом. */
const FPV_EYE_HEIGHT_M = 1.7;
const FPV_FOV_DEG = 70;
const FPV_NEAR_M = 0.15;
const FPV_WALK_SPEED_MS = 4;
const FPV_RUN_SPEED_MS = 12;
const FPV_TURN_RAD_S = 1.6;
const FPV_LOOK_RAD_PER_PX = 0.0025;
/** Максимальный шаг по времени при проверке столкновений, с. */
const FPV_SUBSTEP_S = 0.05;
const FPV_MAX_PITCH = 1.45;
/** Радиус «тела» для столкновения со зданиями. */
const FPV_BODY_RADIUS_M = 0.4;
/** Дальше этого от участка не уйти — иначе можно потеряться в пустом поле. */
const FPV_ROAM_M = 60;
/** С какого расстояния подсказка показывает посадку под прицелом. */
const FPV_AIM_RANGE_M = 45;
const FPV_AIM_INTERVAL_MS = 140;
/** Вход встаёт позади участка обхода, чтобы посадки были впереди, а не под ногами. */
const FPV_START_BACK_M = 8;

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
  /** Вид от первого лица: ходьба WASD со столкновением со зданиями и взглядом мышью. */
  firstPerson?: boolean;
  /** Режим закончился сам (Esc) — страница снимает свой флаг. */
  onFirstPersonChange?: (on: boolean) => void;
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

/** Анизотропная фильтрация для текстур окон и газона: без неё окна здания на
 * расстоянии рябят муаром. Выставляется один раз при создании рендерера (он
 * знает предел видеокарты), текстуры читают значение при создании. */
let textureAnisotropy = 1;

/** Тени считаются только на сцене не крупнее этого числа посадок: теневая карта —
 * ещё один проход по всем экземплярам, а на реальном масштабе (сотни тысяч
 * посадок на участке в километр) разрешения одной карты всё равно не хватило бы
 * на читаемую тень. Выше порога сцена рисуется без теней, как раньше. */
const SHADOW_ITEM_LIMIT = 80_000;

// A shared canvas texture for every lawn, drawn in
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

/** Стили фасадов: панельный дом, кирпичный, оштукатуренный. Какой у здания — по
 * хэшу его контура (plan3d.ts), как и высота: реальных данных о фасадах нет, это
 * оформление, а не сведения о доме. */
type FacadeStyle = "panel" | "brick" | "plaster";
const FACADE_STYLES: FacadeStyle[] = ["panel", "brick", "plaster"];
/** Плитка фасада — 4×4 окна по 3 м: у каждого своё состояние (шторы, свет), и узор
 * повторяется только раз в 12 м, а не в каждом окне (иначе фасад — штамп). */
const FACADE_TILE_M = 12;
const FACADE_BAY_M = 3;
/** Лёгкий оттенок стены поверх цвета текстуры: дома одного стиля не близнецы. */
const FACADE_TINTS = ["#ffffff", "#f6eee2", "#e8efe9", "#f2e8ee"];
const ROOF_COLORS = ["#8e8c88", "#6f6b67", "#9a5d4a", "#7d8b7b"];

function facadeTexture(style: FacadeStyle): THREE.CanvasTexture {
  const size = 256;
  const bay = size / 4;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const salt = style === "brick" ? 1.7 : style === "plaster" ? 5.3 : 9.1;
  const rnd = (n: number) => hashToUnit(n * 7.13 + salt);

  ctx.fillStyle = style === "brick" ? "#c98d73" : style === "plaster" ? "#eee6cf" : "#dedad2";
  ctx.fillRect(0, 0, size, size);
  if (style === "brick") {
    for (let row = 0; row < size / 6; row++) {
      const y = row * 6;
      const offset = row % 2 ? 7 : 0;
      for (let x = -offset; x < size; x += 14) {
        const tone = rnd(row * 53 + x);
        ctx.fillStyle = tone > 0.5 ? `rgba(255,222,200,${0.06 + tone * 0.1})` : `rgba(90,40,30,${0.05 + tone * 0.12})`;
        ctx.fillRect(x + 1, y + 1, 12, 4);
      }
      ctx.fillStyle = "rgba(236,226,216,0.6)";
      ctx.fillRect(0, y + 5, size, 1);
      for (let x = -offset; x < size; x += 14) ctx.fillRect(x + 13, y, 1, 6);
    }
  } else {
    for (let i = 0; i < 700; i++) {
      ctx.fillStyle = `rgba(0,0,0,${0.025 + rnd(i) * 0.03})`;
      ctx.fillRect(rnd(i * 3 + 1) * size, rnd(i * 5 + 2) * size, 1 + rnd(i * 7 + 3) * 2, 1 + rnd(i * 11 + 4) * 2);
    }
    if (style === "panel") {
      // Стыки панелей: между окнами и между этажами.
      ctx.fillStyle = "rgba(120,115,108,0.55)";
      for (let k = 0; k < 4; k++) {
        ctx.fillRect(k * bay, 0, 2, size);
        ctx.fillRect(0, k * bay, size, 2);
      }
    } else {
      // Поэтажные полочки.
      for (let k = 0; k < 4; k++) {
        ctx.fillStyle = "rgba(255,255,255,0.55)";
        ctx.fillRect(0, k * bay + 2, size, 3);
        ctx.fillStyle = "rgba(0,0,0,0.08)";
        ctx.fillRect(0, k * bay + 5, size, 2);
      }
    }
  }

  for (let j = 0; j < 4; j++) {
    for (let i = 0; i < 4; i++) {
      const wide = style === "plaster" ? 40 : 36;
      const x = i * bay + (bay - wide) / 2;
      const y = j * bay + 14;
      const h = 34;
      const roll = rnd(j * 4 + i + 100);
      ctx.fillStyle = "#f4f4f2";
      ctx.fillRect(x - 2, y - 2, wide + 4, h + 4);
      const glass = ctx.createLinearGradient(0, y, 0, y + h);
      if (roll > 0.82) {
        glass.addColorStop(0, "#f2de9e");
        glass.addColorStop(1, "#c8a65a"); // свет в окне
      } else if (roll > 0.6) {
        glass.addColorStop(0, "#d2c4a4");
        glass.addColorStop(1, "#a99b7e"); // шторы
      } else {
        glass.addColorStop(0, "#718ca8");
        glass.addColorStop(1, "#33475e");
      }
      ctx.fillStyle = glass;
      ctx.fillRect(x, y, wide, h);
      ctx.fillStyle = "#f4f4f2";
      ctx.fillRect(x + wide / 2 - 1, y, 2, h);
      ctx.fillStyle = "rgba(255,255,255,0.14)";
      ctx.beginPath();
      ctx.moveTo(x + 4, y + h);
      ctx.lineTo(x + 14, y);
      ctx.lineTo(x + 22, y);
      ctx.lineTo(x + 12, y + h);
      ctx.closePath();
      ctx.fill();
      ctx.fillStyle = "rgba(70,66,60,0.55)";
      ctx.fillRect(x - 3, y + h + 2, wide + 6, 3);
      if (style === "panel" && rnd(j * 9 + i + 300) > 0.72) {
        // Балкон: плита и ограждение под окном.
        ctx.fillStyle = "rgba(96,99,103,0.85)";
        ctx.fillRect(i * bay + 4, y + h + 5, bay - 8, 5);
        ctx.fillStyle = "rgba(60,64,70,0.55)";
        ctx.fillRect(i * bay + 4, y + h - 6, bay - 8, 11);
      }
    }
  }

  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.colorSpace = THREE.SRGBColorSpace;
  // У боковых граней ExtrudeGeometry v = 1 − высота, то есть вверх он убывает; без flipY
  // фасад висел бы вверх ногами (подоконники над окнами).
  texture.flipY = false;
  texture.anisotropy = textureAnisotropy;
  texture.repeat.set(1 / FACADE_TILE_M, 1 / FACADE_TILE_M);
  return texture;
}

/** Общие материалы зданий: три фасада × четыре оттенка и четыре крыши на всю сцену,
 * а не по материалу и текстуре на каждый дом. Тёмная тема приглушает всё. */
class BuildingKit {
  private readonly textures = new Map<FacadeStyle, THREE.CanvasTexture>();
  private readonly walls = new Map<string, THREE.MeshStandardMaterial>();
  private readonly roofs = new Map<number, THREE.MeshStandardMaterial>();
  constructor(private readonly dim: number, private readonly disposables: Disposable[]) {}

  wall(style: FacadeStyle, tone: number): THREE.MeshStandardMaterial {
    const key = `${style}:${tone}`;
    let material = this.walls.get(key);
    if (!material) {
      let texture = this.textures.get(style);
      if (!texture) {
        texture = facadeTexture(style);
        this.textures.set(style, texture);
      }
      material = new THREE.MeshStandardMaterial({ map: texture, vertexColors: true, color: new THREE.Color(FACADE_TINTS[tone]).multiplyScalar(this.dim), roughness: 0.92 });
      this.walls.set(key, material);
      this.disposables.push({ material, texture: this.textures.get(style) });
    }
    return material;
  }

  roof(index: number): THREE.MeshStandardMaterial {
    let material = this.roofs.get(index);
    if (!material) {
      material = new THREE.MeshStandardMaterial({ color: new THREE.Color(ROOF_COLORS[index]).multiplyScalar(this.dim), vertexColors: true, roughness: 0.95 });
      this.roofs.set(index, material);
      this.disposables.push({ material });
    }
    return material;
  }
}

/** Здание: ExtrudeGeometry группирует грани по материалам — индекс 0 крышка и дно,
 * индекс 1 боковые стены (подтверждено чтением исходников three 0.186), поэтому
 * крыша — плоский цвет, а фасад — текстура, не размазанная по крыше. Цвет вершин
 * темнее у земли (стена там затенена и испачкана — без этого дом «стоит на листе»),
 * сдвиг текстуры свой у каждого дома — соседние фасады не повторяют друг друга. */
function buildingMesh(building: Building3D, kit: BuildingKit, baseY: number): THREE.Mesh | null {
  const shape = shapeFromPolygon(building);
  if (!shape) return null;
  const geometry = new THREE.ExtrudeGeometry(shape, { depth: building.height, bevelEnabled: false });

  const position = geometry.attributes.position;
  const colors = new Float32Array(position.count * 3);
  for (let i = 0; i < position.count; i++) {
    const z = position.getZ(i);
    const t = Math.max(0, Math.min(1, z / 5));
    const shade = 0.66 + 0.34 * t * t * (3 - 2 * t);
    colors[i * 3] = shade;
    colors[i * 3 + 1] = shade;
    colors[i * 3 + 2] = shade;
  }
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));

  const uv = geometry.attributes.uv;
  const offsetU = hashToUnit(building.hash01 * 91.7 + 3.1) * FACADE_TILE_M;
  const offsetV = Math.floor(hashToUnit(building.hash01 * 57.3 + 8.8) * 4) * FACADE_BAY_M;
  for (let i = 0; i < uv.count; i++) uv.setXY(i, uv.getX(i) + offsetU, uv.getY(i) + offsetV);

  const style = FACADE_STYLES[Math.min(FACADE_STYLES.length - 1, Math.floor(building.hash01 * FACADE_STYLES.length))];
  const tone = Math.floor(hashToUnit(building.hash01 * 13.7 + 1.9) * FACADE_TINTS.length) % FACADE_TINTS.length;
  const roof = Math.floor(hashToUnit(building.hash01 * 29.1 + 6.6) * ROOF_COLORS.length) % ROOF_COLORS.length;
  const mesh = new THREE.Mesh(geometry, [kit.roof(roof), kit.wall(style, tone)]);
  mesh.rotation.x = -Math.PI / 2;
  mesh.position.y = baseY;
  return mesh;
}

/** Карниз и крышные надстройки всех домов — одним мешем, а не по мешу на дом.
 * Карниз — полка вокруг крыши (контур, чуть увеличенный вокруг центра, ниже уровня
 * крыши: виден только свес). Надстройки — лифтовая/лестничная шахта и вентканалы в
 * местах, где они целиком внутри контура: крыша перестаёт быть пустой плиткой. */
function buildRoofDetails(buildings: Building3D[], baseY: number, dim: number, disposables: Disposable[]): THREE.Mesh | null {
  const parts: THREE.BufferGeometry[] = [];
  const paint = (geometry: THREE.BufferGeometry, hex: string) => {
    const color = new THREE.Color(hex).multiplyScalar(dim);
    const count = geometry.attributes.position.count;
    const array = new Float32Array(count * 3);
    for (let i = 0; i < count; i++) array.set([color.r, color.g, color.b], i * 3);
    geometry.setAttribute("color", new THREE.BufferAttribute(array, 3));
    geometry.clearGroups();
    return geometry;
  };
  for (const building of buildings) {
    const ring = building.outer;
    if (ring.length < 4 || building.height < 5) continue;
    let minX = Infinity;
    let minY = Infinity;
    let maxX = -Infinity;
    let maxY = -Infinity;
    let cx = 0;
    let cy = 0;
    for (const [x, y] of ring) {
      minX = Math.min(minX, x);
      maxX = Math.max(maxX, x);
      minY = Math.min(minY, y);
      maxY = Math.max(maxY, y);
      cx += x;
      cy += y;
    }
    cx /= ring.length;
    cy /= ring.length;
    const half = Math.max(maxX - minX, maxY - minY) / 2;
    const top = baseY + building.height;

    // Карниз.
    const scaled = ring.map(([x, y]): [number, number] => [cx + (x - cx) * (1 + 0.5 / Math.max(2, half)), cy + (y - cy) * (1 + 0.5 / Math.max(2, half))]);
    const ledgeShape = shapeFromPolygon({ outer: scaled, holes: [] });
    if (ledgeShape) {
      const ledge = new THREE.ExtrudeGeometry(ledgeShape, { depth: 0.45, bevelEnabled: false });
      ledge.rotateX(-Math.PI / 2);
      ledge.translate(0, top - 0.5, 0);
      parts.push(paint(ledge, "#d9d5cd"));
    }

    // Надстройки: шахта побольше и пара вентканалов, каждая только если помещается в контур.
    const inside = (x: number, y: number, w: number, d: number) =>
      pointInRing(x - w / 2, y - d / 2, ring) && pointInRing(x + w / 2, y - d / 2, ring) && pointInRing(x - w / 2, y + d / 2, ring) && pointInRing(x + w / 2, y + d / 2, ring);
    const seed = building.hash01 * 100;
    const boxes: [number, number, number, number, number, string][] = [
      [2.4 + hashToUnit(seed + 1) * 1.4, 2.6, 2.4 + hashToUnit(seed + 2) * 1.4, (hashToUnit(seed + 3) - 0.5) * half * 0.6, (hashToUnit(seed + 4) - 0.5) * half * 0.6, "#7d7974"],
      [0.8, 0.9, 0.8, (hashToUnit(seed + 5) - 0.5) * half * 1.1, (hashToUnit(seed + 6) - 0.5) * half * 1.1, "#8a8680"],
      [0.8, 0.9, 0.8, (hashToUnit(seed + 7) - 0.5) * half * 1.1, (hashToUnit(seed + 8) - 0.5) * half * 1.1, "#8a8680"],
    ];
    for (const [w, h, d, dx, dy, color] of boxes) {
      const bx = cx + dx;
      const by = cy + dy;
      if (!inside(bx, by, w, d)) continue;
      const box = new THREE.BoxGeometry(w, h, d).toNonIndexed();
      box.translate(bx, top + h / 2, -by); // план y → сцена −z
      parts.push(paint(box, color));
    }
  }
  if (parts.length === 0) return null;
  const merged = mergeGeometries(parts);
  parts.forEach((part) => part.dispose());
  if (!merged) return null;
  const material = new THREE.MeshStandardMaterial({ vertexColors: true, roughness: 0.95 });
  disposables.push({ geometry: merged, material });
  return new THREE.Mesh(merged, material);
}

interface Disposable {
  geometry?: THREE.BufferGeometry;
  /** Нет у геометрии, чей материал общий с другой и освобождается вместе с ней. */
  material?: THREE.Material;
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
 * Крона из пяти шаров детализации 2 — ~1600 треугольников: для сотни деревьев
 * это ничто и крона выглядит органично, а на реальном масштабе (сотни тысяч
 * посадок, см. MAX_3D_ITEMS) — десятки миллионов и слайд-шоу на обычной видеокарте.
 * Поэтому детализация падает ступенями: 2 (≈1600 треугольников, гладкая, с текстурой
 * листвы) → 1 (≈400) → 0 (≈100, плоские грани) → -1 (один шар, 20): силуэт леса при
 * этом остаётся, а число экземпляров не режется. */
function crownDetail(instances: number): number {
  if (instances <= 1_500) return 2;
  if (instances <= 25_000) return 1;
  if (instances <= 120_000) return 0;
  return -1;
}

/** Сколько вариантов формы у одной формы кроны: без них все липы — близнецы и лес
 * читается как штамп. Каждый вариант — свой `InstancedMesh`, то есть вызовов отрисовки
 * на форму в три раза больше, но не больше, чем число посадок. */
const CROWN_VARIANTS = 3;

/** Шум значений в [0, 1]: гладкий, детерминированный, без внешних зависимостей. Нужен
 * для неровностей кроны — на гладком шаре дерево читается леденцом на палочке. */
function noise3(x: number, y: number, z: number): number {
  const ix = Math.floor(x);
  const iy = Math.floor(y);
  const iz = Math.floor(z);
  const fx = x - ix;
  const fy = y - iy;
  const fz = z - iz;
  const u = fx * fx * (3 - 2 * fx);
  const v = fy * fy * (3 - 2 * fy);
  const w = fz * fz * (3 - 2 * fz);
  const at = (dx: number, dy: number, dz: number) => hashToUnit((ix + dx) * 12.9898 + (iy + dy) * 78.233 + (iz + dz) * 37.719);
  const lerp = (a: number, b: number, t: number) => a + (b - a) * t;
  const x00 = lerp(at(0, 0, 0), at(1, 0, 0), u);
  const x10 = lerp(at(0, 1, 0), at(1, 1, 0), u);
  const x01 = lerp(at(0, 0, 1), at(1, 0, 1), u);
  const x11 = lerp(at(0, 1, 1), at(1, 1, 1), u);
  return lerp(lerp(x00, x10, v), lerp(x01, x11, v), w);
}

/** Сдвиг вершин шара вдоль направления от его центра на шум в двух масштабах:
 * крупные бугры кроны и мелкая неровность края. Зависит только от положения
 * вершины, поэтому совпадающие вершины соседних граней сдвигаются одинаково и
 * щелей не остаётся. */
function roughen(geometry: THREE.BufferGeometry, centre: [number, number, number], amplitude: number, seed: number): void {
  const position = geometry.attributes.position;
  for (let i = 0; i < position.count; i++) {
    const x = position.getX(i);
    const y = position.getY(i);
    const z = position.getZ(i);
    const dx = x - centre[0];
    const dy = y - centre[1];
    const dz = z - centre[2];
    const length = Math.hypot(dx, dy, dz) || 1;
    const bump = (noise3(x * 2.6 + seed, y * 2.6 + seed * 1.7, z * 2.6 + seed * 2.3) - 0.5) * 2 * amplitude + (noise3(x * 7 + seed, y * 7, z * 7 - seed) - 0.5) * 2 * amplitude * 0.4;
    position.setXYZ(i, x + (dx / length) * bump, y + (dy / length) * bump, z + (dz / length) * bump);
  }
}

/** Ярусный конус (можжевельник): три яруса, а не гладкий конус из учебника. */
function tieredConeParts(detail: number): THREE.BufferGeometry[] {
  const radial = detail >= 1 ? 14 : 8;
  return [
    [1, 1.15, -1],
    [0.76, 1.0, -0.38],
    [0.5, 0.9, 0.2],
  ].map(([radius, height, bottom]) => {
    const cone = new THREE.ConeGeometry(radius, height, radial, 1).toNonIndexed();
    cone.translate(0, bottom + height / 2, 0);
    return cone;
  });
}

/** Единичная геометрия кроны, вписанная в [-1, 1] по всем осям: экземпляр
 * растягивается до своей ширины и высоты матрицей, так что одна геометрия
 * обслуживает кроны любого размера. «lobed» и «bush» — несколько сросшихся
 * неровных шаров, «oval» — яйцо, суженное кверху (берёза), «cone» — ярусный конус.
 * `variant` сдвигает шум и положение шаров: три варианта на форму, чтобы деревья
 * одной породы не были близнецами. Вблизи (detail ≥ 1) нормали сглажены, на
 * удалении (detail ≤ 0) — плоские грани: там грани крошечные и дешевле. */
function crownGeometry(form: CrownForm, detail: number, variant: number): THREE.BufferGeometry {
  const seed = variant * 17.31 + (form === "bush" ? 3.1 : form === "cone" ? 7.7 : form === "oval" ? 11.3 : 0);
  let merged: THREE.BufferGeometry;
  if (detail < 0) {
    merged = new THREE.IcosahedronGeometry(1, 0);
  } else if (form === "cone") {
    const parts = tieredConeParts(detail);
    parts.forEach((part) => roughen(part, [0, 0, 0], 0.045, seed));
    merged = mergeGeometries(parts)!;
    parts.forEach((part) => part.dispose());
  } else if (form === "oval") {
    const ball = new THREE.IcosahedronGeometry(1, Math.min(detail + 1, 3));
    const position = ball.attributes.position;
    for (let i = 0; i < position.count; i++) {
      // Яйцо: у верхушки уже, чем у основания.
      const taper = 1 - 0.3 * Math.max(0, position.getY(i));
      position.setXYZ(i, position.getX(i) * taper, position.getY(i), position.getZ(i) * taper);
    }
    roughen(ball, [0, 0, 0], 0.06, seed);
    merged = ball;
  } else {
    const base: [number, number, number, number][] =
      form === "bush"
        ? [
            [0, 0, 0, 0.8],
            [0.55, -0.2, 0.1, 0.55],
            [-0.5, -0.15, -0.2, 0.58],
            [0.05, 0.1, 0.55, 0.5],
            [-0.1, -0.05, -0.55, 0.45],
          ]
        : [
            [0, 0, 0, 0.85],
            [0.5, -0.15, 0.2, 0.6],
            [-0.45, -0.1, 0.35, 0.58],
            [0.1, 0.3, -0.5, 0.6],
            [-0.2, 0.45, 0.1, 0.5],
            [0.35, 0.35, 0.35, 0.42],
          ];
    const parts = base.map(([x, y, z, r], k) => {
      // Каждый вариант чуть сдвигает и раздувает шары — кроны не повторяются.
      const jitter = (n: number) => (hashToUnit(seed + k * 3.7 + n * 9.1) - 0.5) * 0.18;
      const cx = x + jitter(1);
      const cy = y + jitter(2) * 0.6;
      const cz = z + jitter(3);
      const part = new THREE.IcosahedronGeometry(r * (1 + jitter(4) * 0.5), detail);
      part.translate(cx, cy, cz);
      roughen(part, [cx, cy, cz], 0.075, seed + k);
      return part;
    });
    merged = mergeGeometries(parts)!;
    parts.forEach((part) => part.dispose());
  }

  merged.computeBoundingBox();
  const box = merged.boundingBox!;
  const halfWidth = Math.max(Math.abs(box.min.x), box.max.x, Math.abs(box.min.z), box.max.z);
  const halfHeight = Math.max(Math.abs(box.min.y), box.max.y);
  merged.scale(1 / halfWidth, 1 / halfHeight, 1 / halfWidth);

  // Гладкие нормали вблизи: убрать нормали и uv, слить совпадающие вершины, пересчитать.
  let geometry = merged;
  if (detail >= 1) {
    merged.deleteAttribute("normal");
    merged.deleteAttribute("uv");
    geometry = mergeVertices(merged, 1e-4);
    merged.dispose();
  } else {
    merged.deleteAttribute("uv");
  }
  geometry.computeVertexNormals();

  // Плоская проекция на текстуру листвы (без шва, у сферической развёртки он был бы виден) и
  // цвет вершин: снизу темнее (кроне нечего освещать изнутри), сверху светлее, плюс
  // пятнистость — без неё крона одного цвета читается пластилином.
  const position = geometry.attributes.position;
  const uv = new Float32Array(position.count * 2);
  const colors = new Float32Array(position.count * 3);
  for (let i = 0; i < position.count; i++) {
    const x = position.getX(i);
    const y = position.getY(i);
    const z = position.getZ(i);
    uv[i * 2] = x * 0.5 + 0.5 + z * 0.31;
    uv[i * 2 + 1] = y * 0.5 + 0.5 - z * 0.17;
    const height = Math.max(0, Math.min(1, (y + 0.9) / 1.8));
    const light = 0.62 + 0.5 * height * height * (3 - 2 * height);
    const mottling = 0.86 + 0.28 * noise3(x * 3.2 + seed, y * 3.2, z * 3.2 - seed);
    const v = light * mottling;
    colors[i * 3] = v;
    colors[i * 3 + 1] = v;
    colors[i * 3 + 2] = v;
  }
  geometry.setAttribute("uv", new THREE.BufferAttribute(uv, 2));
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
  return geometry;
}

/** Мелкие пятна листвы: серая (светлее основы) тайловая текстура, умножается на
 * цвет породы. Края сшиты — пятна, вылезающие за край, дорисовываются с обратной
 * стороны. Без неё даже неровная крона одного оттенка выглядит гладкой. */
function foliageTexture(): THREE.CanvasTexture {
  const size = 128;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#dcdcdc";
  ctx.fillRect(0, 0, size, size);
  for (let i = 0; i < 520; i++) {
    const x = hashToUnit(i * 1.7 + 0.3) * size;
    const y = hashToUnit(i * 2.9 + 4.1) * size;
    const rx = 2 + hashToUnit(i * 3.3 + 7.7) * 5;
    const ry = 1.5 + hashToUnit(i * 5.1 + 1.9) * 3.5;
    const angle = hashToUnit(i * 7.3 + 2.2) * Math.PI;
    const grey = Math.round(120 + hashToUnit(i * 11.7 + 9.4) * 135);
    ctx.fillStyle = `rgba(${grey},${grey},${grey},0.5)`;
    for (const dx of [-size, 0, size]) {
      for (const dy of [-size, 0, size]) {
        if (x + dx < -rx - 2 || x + dx > size + rx + 2 || y + dy < -rx - 2 || y + dy > size + rx + 2) continue;
        ctx.beginPath();
        ctx.ellipse(x + dx, y + dy, rx, ry, angle, 0, Math.PI * 2);
        ctx.fill();
      }
    }
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(2.4, 2.4);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

/** Кора: вертикальные прожилки и редкие поперечные чёрточки (у берёзы читаются как её
 * знаменитые отметины), серая — умножается на цвет ствола породы. */
function barkTexture(): THREE.CanvasTexture {
  const width = 64;
  const height = 128;
  const canvas = document.createElement("canvas");
  canvas.width = width;
  canvas.height = height;
  const ctx = canvas.getContext("2d")!;
  ctx.fillStyle = "#e0e0e0";
  ctx.fillRect(0, 0, width, height);
  for (let i = 0; i < 34; i++) {
    let x = hashToUnit(i * 1.3 + 2.1) * width;
    const alpha = 0.18 + hashToUnit(i * 4.7 + 1.1) * 0.35;
    ctx.strokeStyle = `rgba(55,55,55,${alpha})`;
    ctx.lineWidth = 1 + hashToUnit(i * 6.1 + 3.3) * 1.6;
    ctx.beginPath();
    ctx.moveTo(x, 0);
    for (let y = 8; y <= height; y += 8) {
      x += (hashToUnit(i * 9.9 + y) - 0.5) * 2.4;
      ctx.lineTo(x, y);
    }
    ctx.stroke();
  }
  for (let i = 0; i < 16; i++) {
    ctx.fillStyle = "rgba(40,40,40,0.55)";
    ctx.fillRect(hashToUnit(i * 2.3 + 8.8) * (width - 12), hashToUnit(i * 3.9 + 6.4) * height, 5 + hashToUnit(i * 7.7) * 7, 2);
  }
  const texture = new THREE.CanvasTexture(canvas);
  texture.wrapS = THREE.RepeatWrapping;
  texture.wrapT = THREE.RepeatWrapping;
  texture.repeat.set(2, 3);
  texture.colorSpace = THREE.SRGBColorSpace;
  return texture;
}

/** Мягкое тёмное пятно под растением: вкладывает его в землю. Тени от солнца
 * (SHADOW_ITEM_LIMIT) есть не на каждой сцене и не мягкие, а без заземления
 * кроны «парят» над газоном. */
function groundBlobTexture(): THREE.CanvasTexture {
  const size = 64;
  const canvas = document.createElement("canvas");
  canvas.width = size;
  canvas.height = size;
  const ctx = canvas.getContext("2d")!;
  const gradient = ctx.createRadialGradient(size / 2, size / 2, 0, size / 2, size / 2, size / 2);
  gradient.addColorStop(0, "rgba(0,0,0,0.95)");
  gradient.addColorStop(0.55, "rgba(0,0,0,0.45)");
  gradient.addColorStop(1, "rgba(0,0,0,0)");
  ctx.fillStyle = gradient;
  ctx.fillRect(0, 0, size, size);
  return new THREE.CanvasTexture(canvas);
}

/** Ствол: усечённый конус с расширением у корня. */
function trunkGeometry(): THREE.BufferGeometry {
  const geometry = new THREE.CylinderGeometry(0.55, 1, 1, 8, 4);
  const position = geometry.attributes.position;
  for (let i = 0; i < position.count; i++) {
    const t = position.getY(i) + 0.5; // 0 у земли, 1 у кроны
    if (t < 0.16) {
      const flare = 1 + (1 - t / 0.16) * 0.55;
      position.setX(i, position.getX(i) * flare);
      position.setZ(i, position.getZ(i) * flare);
    }
  }
  geometry.translate(0, 0.5, 0);
  geometry.computeVertexNormals();
  return geometry;
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
 * форму кроны и вариант формы, один на стволы и один на тени, цвет — на экземпляр
 * (`setColorAt`), так что число вызовов отрисовки не зависит ни от числа посадок,
 * ни от числа пород. */
function buildPlants(points: PlantingPoint3D[], plantingType: "tree" | "shrub", season: Season, disposables: Disposable[]): THREE.Object3D[] {
  if (points.length === 0) return [];

  const byForm = new Map<CrownForm, PlantEntry[][]>();
  const trunked: PlantEntry[] = [];
  for (const point of points) {
    const look = lookOf(point.species, plantingType, point.crownClass);
    const entry: PlantEntry = { point, look, jitter: plantJitter(point) };
    const variant = Math.min(CROWN_VARIANTS - 1, Math.floor(hashToUnit(point.x * 0.37 + point.y * 0.91 + 5.5) * CROWN_VARIANTS));
    let buckets = byForm.get(look.form);
    if (!buckets) {
      buckets = Array.from({ length: CROWN_VARIANTS }, () => []);
      byForm.set(look.form, buckets);
    }
    buckets[variant].push(entry);
    if (look.trunkShare > 0) trunked.push(entry);
  }

  const objects: THREE.Object3D[] = [];
  const matrix = new THREE.Matrix4();
  const quaternion = new THREE.Quaternion();
  const yAxis = new THREE.Vector3(0, 1, 0);
  const position = new THREE.Vector3();
  const scale = new THREE.Vector3();

  // Мягкая тень под каждым растением: кусты в изгороди перекрываются, поэтому у них слабее.
  {
    const geometry = new THREE.CircleGeometry(1, 20);
    geometry.rotateX(-Math.PI / 2);
    const texture = groundBlobTexture();
    const material = new THREE.MeshBasicMaterial({
      map: texture,
      transparent: true,
      opacity: plantingType === "tree" ? 0.5 : 0.3,
      depthWrite: false,
      polygonOffset: true,
      polygonOffsetFactor: -2,
      polygonOffsetUnits: -2,
    });
    const mesh = new THREE.InstancedMesh(geometry, material, points.length);
    disposables.push({ geometry, material, texture });
    points.forEach((point, index) => {
      const radius = point.canopyRadius * plantJitter(point).size * (plantingType === "tree" ? 1.2 : 1.1);
      matrix.compose(position.set(point.x, PLANTING_BASE_Y + 0.03, -point.y), quaternion.identity(), scale.set(radius, 1, radius));
      mesh.setMatrixAt(index, matrix);
    });
    mesh.instanceMatrix.needsUpdate = true;
    mesh.renderOrder = 1;
    mesh.userData.decor = true;
    objects.push(mesh);
  }

  // Стволы: сужающийся конус с коркой, цвет по породе (у берёзы белый).
  if (trunked.length > 0) {
    const geometry = trunkGeometry();
    const texture = barkTexture();
    const material = new THREE.MeshStandardMaterial({ color: 0xffffff, map: texture, roughness: 1 });
    const mesh = new THREE.InstancedMesh(geometry, material, trunked.length);
    disposables.push({ geometry, material, texture });
    trunked.forEach(({ point, look, jitter }, index) => {
      const diameter = point.canopyRadius * 2 * jitter.size;
      const height = diameter * look.heightRatio * jitter.height;
      const trunkHeight = height * look.trunkShare;
      const radius = Math.max(diameter * 0.045, 0.1);
      matrix.compose(position.set(point.x, PLANTING_BASE_Y, -point.y), quaternion.setFromAxisAngle(yAxis, jitter.rotationY), scale.set(radius, trunkHeight, radius));
      mesh.setMatrixAt(index, matrix);
      mesh.setColorAt(index, new THREE.Color(look.trunkColor).offsetHSL(0, 0, jitter.lightness * 0.5));
    });
    mesh.instanceMatrix.needsUpdate = true;
    if (mesh.instanceColor) mesh.instanceColor.needsUpdate = true;
    mesh.userData.castShadow = true;
    mesh.userData.entries = trunked;
    objects.push(mesh);
  }

  let foliage: THREE.CanvasTexture | null = null;
  for (const [form, buckets] of byForm) {
    const total = buckets.reduce((sum, bucket) => sum + bucket.length, 0);
    const detail = crownDetail(total);
    // Текстура листвы — только там, где крона достаточно детальна, чтобы её было видно.
    if (detail >= 1 && !foliage) foliage = foliageTexture();
    const material = new THREE.MeshStandardMaterial({
      color: 0xffffff,
      map: detail >= 1 ? foliage : null,
      vertexColors: true,
      flatShading: detail <= 0,
      roughness: 0.92,
      metalness: 0,
    });
    let materialOwned = false;
    buckets.forEach((entries, variant) => {
      if (entries.length === 0) return;
      const geometry = crownGeometry(form, detail, variant);
      const mesh = new THREE.InstancedMesh(geometry, material, entries.length);
      // Материал на форму один: освобождается вместе с первой геометрией, остальные — только геометрия.
      disposables.push(materialOwned ? { geometry } : { geometry, material, texture: foliage && detail >= 1 ? foliage : undefined });
      materialOwned = true;
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
    });
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
export default function ThreeDView({ layers, plan, crsVerified, season = "summer", storageKey, sceneTheme = "day", cameraCommand = null, autoRotate = false, onWalkChange, firstPerson = false, onFirstPersonChange }: ThreeDViewProps) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [hover, setHover] = useState<PlantHover | null>(null);
  // Что нужно командам камеры и облёту, чтобы не пересобирать сцену ради них:
  // эффект сцены кладёт сюда живые камеру и управление.
  const viewRef = useRef<{ flyTo: (pose: CameraPose) => void; controls: OrbitControls; setFirstPerson: (on: boolean) => void } | null>(null);
  const autoRotateRef = useRef(autoRotate);
  autoRotateRef.current = autoRotate;
  // Колбэк живёт дольше рендера (его зовёт обработчик внутри эффекта сцены) —
  // через реф, чтобы не пересобирать сцену из-за новой функции на каждый рендер.
  const onWalkChangeRef = useRef(onWalkChange);
  onWalkChangeRef.current = onWalkChange;
  const firstPersonRef = useRef(firstPerson);
  firstPersonRef.current = firstPerson;
  const onFirstPersonChangeRef = useRef(onFirstPersonChange);
  onFirstPersonChangeRef.current = onFirstPersonChange;
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
    const buildingDim = graphite ? 0.7 : 1;
    const buildingKit = new BuildingKit(buildingDim, disposables);
    for (const building of data.buildings) {
      const mesh = buildingMesh(building, buildingKit, TERRITORY_DEPTH_M);
      if (mesh) {
        mesh.userData.castShadow = true;
        scene.add(mesh);
        buildingMeshes.push(mesh);
        disposables.push({ geometry: mesh.geometry });
      }
    }
    const roofDetails = buildRoofDetails(data.buildings, TERRITORY_DEPTH_M, buildingDim, disposables);
    if (roofDetails) {
      roofDetails.userData.castShadow = true;
      scene.add(roofDetails);
    }

    // Деревья и кусты, по которым наводится подсказка: меши с `userData.entries`.
    const plantMeshes: { mesh: THREE.InstancedMesh; plantingType: "tree" | "shrub" }[] = [];
    for (const [points, plantingType] of [[data.trees, "tree"], [data.shrubs, "shrub"]] as const) {
      for (const object of buildPlants(points, plantingType, season, disposables)) {
        scene.add(object);
        if (object instanceof THREE.InstancedMesh && !object.userData.decor) plantMeshes.push({ mesh: object, plantingType });
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

    // --- вид от первого лица --------------------------------------------------
    // Свой маленький контроллер, а не PointerLockControls: нужна ходьба с
    // коллизией, а взгляд должен работать и без захвата мыши (зажатая кнопка) —
    // захват браузер даёт не везде и не всегда.
    const collides = buildingCollider(data?.buildings ?? []);
    const roam = bounds ? { minX: bounds.minX - FPV_ROAM_M, maxX: bounds.maxX + FPV_ROAM_M, minY: bounds.minY - FPV_ROAM_M, maxY: bounds.maxY + FPV_ROAM_M } : null;
    const isBlocked = (x: number, y: number) =>
      collides(x, y) || collides(x + FPV_BODY_RADIUS_M, y) || collides(x - FPV_BODY_RADIUS_M, y) || collides(x, y + FPV_BODY_RADIUS_M) || collides(x, y - FPV_BODY_RADIUS_M);
    const fpv = {
      active: false,
      x: 0, // координаты плана: x — на восток, y — на север (в сцене z = −y)
      y: 0,
      yaw: 0,
      pitch: 0,
      keys: new Set<string>(),
      saved: null as { position: THREE.Vector3; target: THREE.Vector3 } | null,
      lastTime: 0,
      lastAim: 0,
      lastReport: 0,
      aimedEntry: null as PlantEntry | null,
    };
    const applyFpvCamera = () => {
      camera.position.set(fpv.x, FPV_EYE_HEIGHT_M, -fpv.y);
      camera.rotation.order = "YXZ";
      camera.rotation.set(fpv.pitch, fpv.yaw, 0);
    };
    /** Свободная точка рядом с желаемой: старт не должен оказаться внутри дома. */
    const freeSpotNear = (x: number, y: number): [number, number] => {
      if (!isBlocked(x, y)) return [x, y];
      for (let r = 2; r <= 40; r += 2) {
        for (let a = 0; a < 16; a++) {
          const nx = x + r * Math.cos((a / 16) * Math.PI * 2);
          const ny = y + r * Math.sin((a / 16) * Math.PI * 2);
          if (!isBlocked(nx, ny)) return [nx, ny];
        }
      }
      return [x, y];
    };
    /** Встать у участка обхода лицом по ходу улицы (yaw: 0 — на север, + — налево). */
    const placeAtStop = (index: number) => {
      const stop = stops[index];
      const [px, py] = freeSpotNear(stop.x - stop.heading[0] * FPV_START_BACK_M, stop.y - stop.heading[1] * FPV_START_BACK_M);
      fpv.x = px;
      fpv.y = py;
      fpv.yaw = Math.atan2(-stop.heading[0], stop.heading[1]);
      fpv.pitch = -0.04;
      applyFpvCamera();
    };
    const setFirstPerson = (on: boolean) => {
      if (on === fpv.active) return;
      if (on) {
        fpv.saved = { position: camera.position.clone(), target: controls.target.clone() };
        flight = null;
        controls.autoRotate = false;
        controls.enabled = false;
        camera.fov = FPV_FOV_DEG;
        camera.near = FPV_NEAR_M;
        camera.updateProjectionMatrix();
        if (stops.length > 0) {
          if (walkIndex < 0) {
            // С какого участка начать: ближайший к тому, на что смотрит обзорная камера.
            let best = 0;
            let bestDistance = Infinity;
            stops.forEach((stop, index) => {
              const d = Math.hypot(stop.x - controls.target.x, stop.y + controls.target.z);
              if (d < bestDistance) {
                bestDistance = d;
                best = index;
              }
            });
            walkIndex = best;
            onWalkChangeRef.current?.({ index: best + 1, total: stops.length });
          }
          placeAtStop(walkIndex);
        } else {
          const [px, py] = freeSpotNear(controls.target.x, -controls.target.z);
          fpv.x = px;
          fpv.y = py;
          fpv.yaw = Math.atan2(-(camera.position.x - controls.target.x), -(camera.position.z - controls.target.z)) + Math.PI;
          fpv.pitch = 0;
          applyFpvCamera();
        }
        fpv.active = true;
        fpv.lastTime = performance.now();
        setHover(null);
      } else {
        fpv.active = false;
        fpv.keys.clear();
        fpv.aimedEntry = null;
        if (document.pointerLockElement === renderer.domElement) document.exitPointerLock?.();
        delete container.dataset.fpvState;
        camera.fov = 55;
        camera.near = 0.5;
        camera.updateProjectionMatrix();
        controls.enabled = true;
        setHover(null);
        // Обратно в обзорную камеру — плавно, из того места, где стоял наблюдатель.
        if (fpv.saved) {
          flight = { fromPosition: camera.position.clone(), fromTarget: controls.target.clone(), toPosition: fpv.saved.position, toTarget: fpv.saved.target, startedAt: performance.now() };
        }
      }
    };
    const fpvKeyActions: Record<string, string> = {
      KeyW: "forward",
      ArrowUp: "forward",
      KeyS: "back",
      ArrowDown: "back",
      KeyA: "left",
      KeyD: "right",
      ArrowLeft: "turn-left",
      ArrowRight: "turn-right",
      ShiftLeft: "run",
      ShiftRight: "run",
    };
    const inField = (target: EventTarget | null) => {
      const el = target as HTMLElement | null;
      return !!el && (el.isContentEditable || ["INPUT", "TEXTAREA", "SELECT"].includes(el.tagName));
    };
    const handleFpvKeyDown = (event: KeyboardEvent) => {
      if (!fpv.active || inField(event.target) || event.ctrlKey || event.metaKey || event.altKey) return;
      if (event.code === "Escape") {
        event.stopImmediatePropagation();
        onFirstPersonChangeRef.current?.(false);
        return;
      }
      const action = fpvKeyActions[event.code];
      if (!action) return;
      event.preventDefault();
      event.stopImmediatePropagation(); // W/A/S/D — это ещё и горячие клавиши страницы
      fpv.keys.add(action);
    };
    const handleFpvKeyUp = (event: KeyboardEvent) => {
      const action = fpvKeyActions[event.code];
      if (action) fpv.keys.delete(action);
    };
    const handleFpvBlur = () => fpv.keys.clear();
    const handleFpvLook = (event: PointerEvent) => {
      if (!fpv.active) return;
      if (document.pointerLockElement !== renderer.domElement && (event.buttons & 1) === 0) return;
      fpv.yaw -= event.movementX * FPV_LOOK_RAD_PER_PX;
      fpv.pitch = Math.max(-FPV_MAX_PITCH, Math.min(FPV_MAX_PITCH, fpv.pitch - event.movementY * FPV_LOOK_RAD_PER_PX));
      applyFpvCamera();
    };
    const handleFpvPointerDown = (event: PointerEvent) => {
      if (!fpv.active) return;
      try {
        renderer.domElement.setPointerCapture(event.pointerId);
      } catch {
        // pointer already gone
      }
    };
    const handleFpvClick = () => {
      if (!fpv.active || document.pointerLockElement === renderer.domElement) return;
      try {
        // Захват мыши — удобнее зажатой кнопки; не дали — взгляд работает и без него.
        Promise.resolve(renderer.domElement.requestPointerLock?.()).catch(() => undefined);
      } catch {
        // not allowed here
      }
    };
    window.addEventListener("keydown", handleFpvKeyDown, true);
    window.addEventListener("keyup", handleFpvKeyUp, true);
    window.addEventListener("blur", handleFpvBlur);
    renderer.domElement.addEventListener("pointermove", handleFpvLook);
    renderer.domElement.addEventListener("pointerdown", handleFpvPointerDown);
    renderer.domElement.addEventListener("click", handleFpvClick);
    /** Шаг ходьбы за кадр: скольжение вдоль стены — сначала по обеим осям, потом по одной. */
    const updateFirstPerson = (now: number) => {
      // Время кадра делится на подшаги: скорость не зависит от частоты кадров (на
      // слабой машине сцена идёт в несколько кадров в секунду, и «ходьба» бы
      // отставала от реального времени), а столкновение проверяется на каждом
      // подшаге — через стену не перескочить и при просевшей частоте.
      const total = Math.min(1, (now - fpv.lastTime) / 1000);
      fpv.lastTime = now;
      const k = fpv.keys;
      const turn = (k.has("turn-left") ? 1 : 0) - (k.has("turn-right") ? 1 : 0);
      if (turn !== 0) fpv.yaw += turn * FPV_TURN_RAD_S * total;
      const along = (k.has("forward") ? 1 : 0) - (k.has("back") ? 1 : 0);
      const side = (k.has("right") ? 1 : 0) - (k.has("left") ? 1 : 0);
      if (along !== 0 || side !== 0) {
        const length = Math.hypot(along, side);
        const substeps = Math.max(1, Math.ceil(total / FPV_SUBSTEP_S));
        const speed = ((k.has("run") ? FPV_RUN_SPEED_MS : FPV_WALK_SPEED_MS) * total) / substeps;
        // Вперёд при yaw=0 — на север (+y плана); вправо — на восток.
        const fx = -Math.sin(fpv.yaw);
        const fy = Math.cos(fpv.yaw);
        const dx = ((fx * along + fy * side) / length) * speed;
        const dy = ((fy * along - fx * side) / length) * speed;
        const clampX = (x: number) => (roam ? Math.max(roam.minX, Math.min(roam.maxX, x)) : x);
        const clampY = (y: number) => (roam ? Math.max(roam.minY, Math.min(roam.maxY, y)) : y);
        for (let i = 0; i < substeps; i++) {
          const nx = clampX(fpv.x + dx);
          const ny = clampY(fpv.y + dy);
          if (!isBlocked(nx, ny)) {
            fpv.x = nx;
            fpv.y = ny;
          } else if (!isBlocked(nx, fpv.y)) fpv.x = nx;
          else if (!isBlocked(fpv.x, ny)) fpv.y = ny;
        }
      }
      applyFpvCamera();
      // Подсказка по прицелу: посадка в центре кадра.
      if (now - fpv.lastAim > FPV_AIM_INTERVAL_MS) {
        fpv.lastAim = now;
        let entry: PlantEntry | null = null;
        let plantingType: "tree" | "shrub" = "tree";
        if (plantInstances <= HOVER_INSTANCE_LIMIT) {
          const aim = new THREE.Raycaster();
          aim.setFromCamera(new THREE.Vector2(0, 0), camera);
          aim.far = FPV_AIM_RANGE_M;
          const hit = aim.intersectObjects(plantMeshes.map(({ mesh }) => mesh), false)[0];
          if (hit && hit.instanceId !== undefined) {
            entry = (hit.object.userData.entries as PlantEntry[] | undefined)?.[hit.instanceId] ?? null;
            plantingType = plantMeshes.find(({ mesh }) => mesh === hit.object)?.plantingType ?? "tree";
          }
        }
        if (entry !== fpv.aimedEntry) {
          fpv.aimedEntry = entry;
          if (entry) setHover({ x: container.clientWidth / 2 + 22, y: container.clientHeight / 2 + 22, ...plantHoverText(entry, plantingType) });
          else setHover(null);
        }
      }
      // Положение — в атрибут: по нему видно, где стоит наблюдатель (и это проверяет стенд).
      if (now - fpv.lastReport > 200) {
        fpv.lastReport = now;
        container.dataset.fpvState = `${fpv.x.toFixed(2)},${fpv.y.toFixed(2)},${fpv.yaw.toFixed(3)}`;
      }
    };

    viewRef.current = {
      controls,
      setFirstPerson,
      flyTo(pose) {
        let next: { position: THREE.Vector3; target: THREE.Vector3 };
        if (pose === "walk-next" || pose === "walk-prev") {
          if (stops.length === 0) return;
          // Первое нажатие «вперёд» — первый участок, «назад» — последний; дальше по кругу.
          walkIndex = walkIndex < 0 ? (pose === "walk-next" ? 0 : stops.length - 1) : (walkIndex + (pose === "walk-next" ? 1 : -1) + stops.length) % stops.length;
          onWalkChangeRef.current?.({ index: walkIndex + 1, total: stops.length });
          if (fpv.active) {
            // От первого лица «участок» — это телепорт, а не перелёт камеры.
            placeAtStop(walkIndex);
            return;
          }
          next = walkPose(walkIndex);
        } else {
          if (fpv.active) return;
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
      if (fpv.active) return;
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
      if (fpv.active) return; // от первого лица подсказка идёт по прицелу, а не по курсору
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
      if (fpv.active) {
        updateFirstPerson(performance.now());
        renderer.render(scene, camera);
        return;
      }
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
    // Режим включён до пересборки сцены (смена сезона/темы) — войти в него заново.
    if (firstPersonRef.current) setFirstPerson(true);

    return () => {
      cancelAnimationFrame(frameId);
      window.removeEventListener("keydown", handleFpvKeyDown, true);
      window.removeEventListener("keyup", handleFpvKeyUp, true);
      window.removeEventListener("blur", handleFpvBlur);
      renderer.domElement.removeEventListener("pointermove", handleFpvLook);
      renderer.domElement.removeEventListener("pointerdown", handleFpvPointerDown);
      renderer.domElement.removeEventListener("click", handleFpvClick);
      if (document.pointerLockElement === renderer.domElement) document.exitPointerLock?.();
      delete container.dataset.fpvState;
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
        geometry?.dispose();
        material?.dispose();
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
  useEffect(() => {
    viewRef.current?.setFirstPerson(firstPerson);
  }, [firstPerson]);

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
      {firstPerson && (
        <>
          {/* Прицел: подсказка показывает посадку, на которую он наведён. */}
          <div aria-hidden className="pointer-events-none absolute left-1/2 top-1/2 z-10 h-2 w-2 -translate-x-1/2 -translate-y-1/2 rounded-full border border-white/80 bg-black/30 shadow-[0_0_4px_rgba(0,0,0,.7)]" />
        </>
      )}
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
