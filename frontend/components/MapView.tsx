"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import "leaflet/dist/leaflet.css";
import { AttributionControl, ImageOverlay, MapContainer, Rectangle, TileLayer, GeoJSON, useMap, useMapEvents } from "react-leaflet";
import L, { type Layer, type PathOptions } from "leaflet";
import type { Feature } from "geojson";

import type { GeoJSONFeatureCollection, LayersRaster, LngLat } from "@/lib/api";
import { plantingColor } from "@/lib/mapStyle";
import { boundsOfItems, itemsInBox, type LatLngBox, type PlanIndex } from "@/lib/planIndex";

// Selection is blue on purpose: red means "violates a setback norm", and an
// item can be both selected and violating at the same time.
const SELECTION_COLOR = "#2563eb";
const VIOLATION_COLOR = "#dc2626";

/** Below this much pointer travel a press counts as a click, not a drag. */
const DRAG_THRESHOLD_PX = 4;
/** Pressing this close to the selection's bounding box still grabs it. */
const SELECTION_GRAB_PADDING_PX = 8;
/** Up to this many selected items follow the pointer live while being
 * dragged (one setLatLng per item per animation frame); a bigger selection
 * only moves its bounding box until release, to keep the drag smooth. */
const LIVE_MOVE_PREVIEW_LIMIT = 2000;

// Leaflet's default panes stack tilePane(200) < overlayPane(400) <
// shadowPane(500) < markerPane(600) < tooltipPane(650) < popupPane(700) --
// plan items are Markers (markerPane), so a Rectangle left in the default
// overlayPane would always render *underneath* every tree/shrub dot. A
// dedicated pane above markerPane fixes that; pointerEvents:none keeps it
// purely visual so it never intercepts the press that starts a gesture.
const SELECTION_PANE = "selectionOverlay";

function areaStyle(type: string, selected: boolean, violation: boolean): PathOptions {
  const base = plantingColor(type);
  return {
    color: selected ? SELECTION_COLOR : violation ? VIOLATION_COLOR : base,
    fillColor: violation ? VIOLATION_COLOR : base,
    // Lawn is the only non-Point planting_type (see areaFeatures' filter
    // above), and it's mounted *after* the source-layer <ImageOverlay>s
    // below -- correctly, per the brief's "result on a separate layer over
    // the original drawing". But at the old 0.7 it painted near-opaque over
    // its own footprint, and generate_area_candidates() walks the whole
    // buildable_area -- which includes real existing turf (existing_lawn
    // isn't a hard obstacle, see buffers.HARD_OBSTACLE_ZONE_TYPES) -- so the
    // new lawn polygon routinely coincides with where existing_lawn's raster
    // paints underneath it, at a near-identical pale-lime hue
    // (PLANTING_COLORS.lawn #a3e635 vs. layer_raster.py's existing_lawn
    // #bef264). The two together read as "the existing lawn never rendered
    // at all" -- a repeat live complaint -- when it was rendering, just
    // completely covered. Turned down so the backdrop (existing_lawn, and
    // any other source context under the new lawn's outline) stays legible
    // through it; violation keeps a stronger fill since it's the one state
    // that should visually dominate, not blend in.
    fillOpacity: violation ? 0.55 : 0.35,
    weight: selected ? 3 : violation ? 2 : 1,
    dashArray: selected ? "6 4" : undefined,
  };
}

function planStyle(feature?: Feature): PathOptions {
  return areaStyle(String(feature?.properties?.planting_type ?? ""), false, false);
}

const iconCache = new Map<string, L.DivIcon>();

/** A small colored dot drawn with plain CSS, not an image asset — a standard
 * L.Marker's default icon points at image files the bundler doesn't resolve
 * (see git history). One shared icon per planting type: selection/violation
 * state is a CSS class toggled on each marker's own element, not a
 * different icon, so changing it never re-creates marker DOM. */
function planItemIcon(type: string): L.DivIcon {
  let icon = iconCache.get(type);
  if (!icon) {
    icon = L.divIcon({
      className: "gp-plan-marker",
      html: `<span class="gp-plan-dot" style="background:${plantingColor(type)}"></span>`,
      iconSize: [16, 16],
      iconAnchor: [8, 8],
    });
    iconCache.set(type, icon);
  }
  return icon;
}

/** Zooms/pans to fit the loaded data — only when a different project or
 * plan is opened (`fitKey`), not after every edit of the same plan, which
 * would yank the view away from wherever the user zoomed in to edit. */
function FitBounds({
  fitKey,
  layersBounds,
  plan,
}: {
  fitKey: string;
  layersBounds?: [[number, number], [number, number]] | null;
  plan?: GeoJSONFeatureCollection;
}) {
  const map = useMap();
  const dataRef = useRef({ layersBounds, plan });
  dataRef.current = { layersBounds, plan };

  useEffect(() => {
    const bounds = L.latLngBounds([]);
    if (dataRef.current.layersBounds) bounds.extend(dataRef.current.layersBounds);
    if (dataRef.current.plan) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const planBounds = L.geoJSON(dataRef.current.plan as any).getBounds();
      if (planBounds.isValid()) bounds.extend(planBounds);
    }
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
    // `layersBounds` itself isn't a dep (an edit shouldn't yank the view --
    // see this function's own doc comment), but its *presence* has to be:
    // layersRaster is fetched separately from and later than the project
    // that sets `fitKey` (see useProjectSession.ts's restore/upload flow),
    // so on a fresh load this effect's first run reliably finds
    // layersBounds still null -- fits to nothing, map stays at the default
    // Moscow-wide view -- and without this, nothing ever re-fits once the
    // real bounds arrive a moment later (fitKey/map don't change again).
    // Found live: the raster layers were mounted at the correct place all
    // along, just a few pixels wide at the default zoom -- indistinguishable
    // from "not there" without measuring. Same reasoning for `plan`.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [fitKey, map, Boolean(layersBounds), Boolean(plan)]);

  return null;
}

/** Leaflet only recalculates its container size on the window `resize`
 * event -- it has no way to know the sidebar's CSS width transition
 * (app/page.tsx) just resized *this* container instead. Without this, the
 * map stays cropped/offset to its old size after the panel is
 * collapsed/expanded. 220ms is just past the panel's own 200ms transition. */
function InvalidateSizeOnChange({ trigger }: { trigger: unknown }) {
  const map = useMap();
  useEffect(() => {
    const t = setTimeout(() => map.invalidateSize(), 220);
    return () => clearTimeout(t);
  }, [trigger, map]);
  return null;
}

type RegistryEntry = { layer: Layer; type: string };
type LatLngTree = L.LatLng | LatLngTree[];

function hitItemId(target: EventTarget | null): string | null {
  if (!(target instanceof Element)) return null;
  return target.closest("[data-item-id]")?.getAttribute("data-item-id") ?? null;
}

function boxFromCorners(a: L.LatLng, b: L.LatLng): LatLngBox {
  return { minLat: Math.min(a.lat, b.lat), maxLat: Math.max(a.lat, b.lat), minLng: Math.min(a.lng, b.lng), maxLng: Math.max(a.lng, b.lng) };
}

function boxBounds(box: LatLngBox, offset?: { dLat: number; dLng: number } | null): L.LatLngBoundsExpression {
  const dLat = offset?.dLat ?? 0;
  const dLng = offset?.dLng ?? 0;
  return [
    [box.minLat + dLat, box.minLng + dLng],
    [box.maxLat + dLat, box.maxLng + dLng],
  ];
}

function shiftLatLngs(tree: LatLngTree, dLat: number, dLng: number): LatLngTree {
  return Array.isArray(tree) ? tree.map((t) => shiftLatLngs(t, dLat, dLng)) : L.latLng(tree.lat + dLat, tree.lng + dLng);
}

interface Gesture {
  pointerId: number;
  /** "rect": draw a selection box. "move": drag items to a new spot. Either
   * one turns into a plain click if released before DRAG_THRESHOLD_PX. */
  kind: "rect" | "move";
  startPoint: L.Point;
  startLatLng: L.LatLng;
  hitId: string | null;
  additive: boolean;
  dragging: boolean;
  moveIds: string[];
  /** Each live-previewed layer's position at drag start. */
  originals: Map<Layer, LatLngTree>;
}

interface SelectionControllerProps {
  selectMode: boolean;
  editsLocked: boolean;
  showPlan: boolean;
  planIndex?: PlanIndex;
  /** Not read directly -- planIndex is now patched in place rather than
   * rebuilt per edit (see useProjectSession.ts), so its reference alone no
   * longer signals "positions may have changed"; this does. */
  planRevision?: number;
  selectedIds: ReadonlySet<string>;
  registryRef: MutableRefObject<Map<string, RegistryEntry>>;
  onSelectionChange: (ids: string[]) => void;
  onMoveItems: (ids: string[], from: LngLat, to: LngLat) => void;
  onContextMenu: (clientX: number, clientY: number) => void;
  onViewChange?: () => void;
  focusRequest?: { id: string; seq: number } | null;
}

/** All pointer handling for plan items: click-select, box-select, drag-move,
 * right-click menu.
 *
 * Listens on the map *container* in the capture phase rather than through
 * Leaflet's own map events: L.Marker defaults to bubblingMouseEvents:false,
 * so a press that lands on a tree/shrub dot never reaches map-level
 * mousedown handlers — which is exactly why a selection box used to be
 * impossible to start on top of a dense cluster of dots. Capturing at the
 * container sees every press first, and preventDefault() on pointerdown
 * suppresses the compatibility mousedown that would otherwise start
 * Leaflet's map panning under the gesture.
 *
 * Normal mode: the map pans as usual; pressing on a dot (or on anything
 * already selected) drags it — the whole selection if it's part of one.
 * Selection mode: left-drag draws a box instead of panning; dragging inside
 * the current selection's bounding box moves the selection. In both, a click
 * selects (Shift/Ctrl/Cmd adds or removes), a click on empty space clears.
 */
function SelectionController(props: SelectionControllerProps) {
  const { selectMode, showPlan, planIndex, planRevision, selectedIds, focusRequest } = props;
  const map = useMap();
  const latest = useRef(props);
  latest.current = props;

  if (!map.getPane(SELECTION_PANE)) {
    const pane = map.createPane(SELECTION_PANE);
    pane.style.zIndex = "620";
    pane.style.pointerEvents = "none";
  }

  const [drawBox, setDrawBox] = useState<LatLngBox | null>(null);
  const [moveOffset, setMoveOffset] = useState<{ dLat: number; dLng: number } | null>(null);
  const gestureRef = useRef<Gesture | null>(null);
  const suppressClickRef = useRef(false);

  function handleClick(hitId: string | null, additive: boolean) {
    const p = latest.current;
    if (hitId !== null) {
      if (additive) {
        const next = new Set(p.selectedIds);
        if (next.has(hitId)) next.delete(hitId);
        else next.add(hitId);
        p.onSelectionChange([...next]);
      } else {
        p.onSelectionChange([hitId]);
      }
    } else if (!additive) {
      p.onSelectionChange([]);
    }
  }

  useEffect(() => {
    const container = map.getContainer();
    container.classList.toggle("gp-select-mode", selectMode);
    if (selectMode) {
      map.dragging.disable();
      map.boxZoom.disable();
      container.style.touchAction = "none";
    } else {
      map.dragging.enable();
      map.boxZoom.enable();
      container.style.touchAction = "";
    }
  }, [selectMode, map]);

  useEffect(() => {
    const container = map.getContainer();
    let frame: number | null = null;
    let pendingPoint: L.Point | null = null;

    function selectionGrabbable(point: L.Point): boolean {
      const p = latest.current;
      if (!p.planIndex || p.selectedIds.size === 0) return false;
      const box = boundsOfItems(p.planIndex, p.selectedIds);
      if (!box) return false;
      const topLeft = map.latLngToContainerPoint([box.maxLat, box.minLng]);
      const bottomRight = map.latLngToContainerPoint([box.minLat, box.maxLng]);
      const pad = SELECTION_GRAB_PADDING_PX;
      return point.x >= topLeft.x - pad && point.x <= bottomRight.x + pad && point.y >= topLeft.y - pad && point.y <= bottomRight.y + pad;
    }

    function applyMovePreview(g: Gesture, dLat: number, dLng: number) {
      g.originals.forEach((original, layer) => {
        if (layer instanceof L.Marker) {
          const o = original as L.LatLng;
          layer.setLatLng(L.latLng(o.lat + dLat, o.lng + dLng));
        } else if (layer instanceof L.Polygon) {
          layer.setLatLngs(shiftLatLngs(original, dLat, dLng) as L.LatLng[]);
        }
      });
    }

    function cancelGesture() {
      const g = gestureRef.current;
      if (!g) return;
      gestureRef.current = null;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      applyMovePreview(g, 0, 0);
      setDrawBox(null);
      setMoveOffset(null);
      try {
        container.releasePointerCapture(g.pointerId);
      } catch {
        // pointer already released
      }
    }

    function beginMove(g: Gesture) {
      const p = latest.current;
      // An edit is still being saved: don't start another one on top of it
      // (the refresh that follows it would snap this drag's preview back anyway).
      if (p.editsLocked) return;
      let ids: string[];
      if (g.hitId !== null && !p.selectedIds.has(g.hitId)) {
        ids = g.additive ? [...p.selectedIds, g.hitId] : [g.hitId];
        p.onSelectionChange(ids);
      } else {
        ids = [...p.selectedIds];
      }
      g.moveIds = ids;
      if (ids.length > LIVE_MOVE_PREVIEW_LIMIT) return;
      for (const id of ids) {
        const entry = p.registryRef.current.get(id);
        if (entry?.layer instanceof L.Marker) g.originals.set(entry.layer, entry.layer.getLatLng());
        else if (entry?.layer instanceof L.Polygon) g.originals.set(entry.layer, entry.layer.getLatLngs() as LatLngTree);
      }
    }

    function flushFrame() {
      frame = null;
      const g = gestureRef.current;
      if (!g || !pendingPoint || !g.dragging) return;
      const latlng = map.containerPointToLatLng(pendingPoint);
      if (g.kind === "rect") {
        setDrawBox(boxFromCorners(g.startLatLng, latlng));
      } else if (g.moveIds.length > 0) {
        const offset = { dLat: latlng.lat - g.startLatLng.lat, dLng: latlng.lng - g.startLatLng.lng };
        applyMovePreview(g, offset.dLat, offset.dLng);
        setMoveOffset(offset);
      }
    }

    function onPointerDown(e: PointerEvent) {
      suppressClickRef.current = false;
      if (gestureRef.current) {
        // A second finger (pinch-zoom) -- hand the touch back to Leaflet.
        if (e.pointerType === "touch") cancelGesture();
        return;
      }
      const p = latest.current;
      if (e.button !== 0 || !p.showPlan || !p.planIndex) return;
      const target = e.target instanceof Element ? e.target : null;
      if (target?.closest(".leaflet-control-container, .leaflet-popup")) return;

      const hitId = hitItemId(target);
      const startPoint = map.mouseEventToContainerPoint(e);
      const additive = e.shiftKey || e.ctrlKey || e.metaKey;
      let kind: Gesture["kind"];
      if (p.selectMode) {
        const grabsSelection = !additive && ((hitId !== null && p.selectedIds.has(hitId)) || selectionGrabbable(startPoint));
        kind = grabsSelection ? "move" : "rect";
      } else {
        if (hitId === null) return; // empty map: Leaflet pans as usual
        // An unselected lawn can cover a lot of the map -- pressing on one
        // still pans (a click on it selects it via the map click handler).
        if (!p.planIndex.get(hitId)?.isPoint && !p.selectedIds.has(hitId)) return;
        kind = "move";
      }

      e.preventDefault();
      e.stopPropagation();
      try {
        container.setPointerCapture(e.pointerId);
      } catch {
        // not supported for this pointer -- moves outside the map just won't be tracked
      }
      gestureRef.current = {
        pointerId: e.pointerId,
        kind,
        startPoint,
        startLatLng: map.containerPointToLatLng(startPoint),
        hitId,
        additive,
        dragging: false,
        moveIds: [],
        originals: new Map(),
      };
      suppressClickRef.current = true;
    }

    function onPointerMove(e: PointerEvent) {
      const g = gestureRef.current;
      if (!g || e.pointerId !== g.pointerId) return;
      const point = map.mouseEventToContainerPoint(e);
      if (!g.dragging) {
        if (point.distanceTo(g.startPoint) < DRAG_THRESHOLD_PX) return;
        g.dragging = true;
        if (g.kind === "move") beginMove(g);
      }
      pendingPoint = point;
      if (frame === null) frame = requestAnimationFrame(flushFrame);
    }

    function onPointerUp(e: PointerEvent) {
      const g = gestureRef.current;
      if (!g || e.pointerId !== g.pointerId) return;
      gestureRef.current = null;
      if (frame !== null) cancelAnimationFrame(frame);
      frame = null;
      try {
        container.releasePointerCapture(e.pointerId);
      } catch {
        // already released
      }
      setDrawBox(null);
      setMoveOffset(null);

      const p = latest.current;
      if (!g.dragging) {
        handleClick(g.hitId, g.additive);
        return;
      }
      const end = map.containerPointToLatLng(map.mouseEventToContainerPoint(e));
      if (g.kind === "rect") {
        if (!p.planIndex) return;
        const ids = itemsInBox(p.planIndex, boxFromCorners(g.startLatLng, end));
        p.onSelectionChange(g.additive ? [...new Set([...p.selectedIds, ...ids])] : ids);
      } else if (g.moveIds.length > 0) {
        if (end.equals(g.startLatLng)) {
          applyMovePreview(g, 0, 0);
          return;
        }
        // Items stay where they were dropped until the saved plan comes back
        // (or snap back with it, if the server rejects the move).
        applyMovePreview(g, end.lat - g.startLatLng.lat, end.lng - g.startLatLng.lng);
        p.onMoveItems(g.moveIds, { x: g.startLatLng.lng, y: g.startLatLng.lat }, { x: end.lng, y: end.lat });
      }
    }

    function onPointerCancel(e: PointerEvent) {
      if (gestureRef.current?.pointerId === e.pointerId) cancelGesture();
    }

    // The browser still fires `click` after a handled press; without this it
    // would reach Leaflet's map click (and e.g. clear a just-drawn selection).
    function onClickCapture(e: MouseEvent) {
      if (!suppressClickRef.current) return;
      suppressClickRef.current = false;
      e.stopPropagation();
    }

    function onContextMenu(e: MouseEvent) {
      const p = latest.current;
      if (!p.planIndex || !p.showPlan) return;
      const target = e.target instanceof Element ? e.target : null;
      if (target?.closest(".leaflet-control-container")) return;
      e.preventDefault();
      e.stopPropagation();
      if (gestureRef.current?.dragging) return;
      cancelGesture(); // a touch long-press opens the menu instead of dragging
      const hitId = hitItemId(target);
      if (hitId !== null && !p.selectedIds.has(hitId)) p.onSelectionChange([hitId]);
      p.onContextMenu(e.clientX, e.clientY);
    }

    // Leaflet listens to native touch events on touch devices, which
    // preventDefault() on pointerdown doesn't suppress.
    function onTouchStart(e: TouchEvent) {
      if (gestureRef.current && e.touches.length === 1) e.stopPropagation();
    }

    container.addEventListener("pointerdown", onPointerDown, true);
    container.addEventListener("pointermove", onPointerMove);
    container.addEventListener("pointerup", onPointerUp);
    container.addEventListener("pointercancel", onPointerCancel);
    container.addEventListener("click", onClickCapture, true);
    container.addEventListener("contextmenu", onContextMenu, true);
    container.addEventListener("touchstart", onTouchStart, { capture: true, passive: true });
    return () => {
      cancelGesture();
      container.removeEventListener("pointerdown", onPointerDown, true);
      container.removeEventListener("pointermove", onPointerMove);
      container.removeEventListener("pointerup", onPointerUp);
      container.removeEventListener("pointercancel", onPointerCancel);
      container.removeEventListener("click", onClickCapture, true);
      container.removeEventListener("contextmenu", onContextMenu, true);
      container.removeEventListener("touchstart", onTouchStart, { capture: true });
    };
  }, [map]);

  useMapEvents({
    // Only reached by presses the controller above didn't claim: normal-mode
    // clicks on empty map or on a lawn (Leaflet already filters out clicks
    // that ended a pan).
    click(e) {
      const p = latest.current;
      if (p.selectMode || !p.showPlan || !p.planIndex) return;
      const oe = e.originalEvent;
      handleClick(hitItemId(oe.target), oe.shiftKey || oe.ctrlKey || oe.metaKey);
    },
    movestart() {
      latest.current.onViewChange?.();
    },
    zoomstart() {
      latest.current.onViewChange?.();
    },
  });

  useEffect(() => {
    if (!focusRequest) return;
    const item = latest.current.planIndex?.get(focusRequest.id);
    if (!item) return;
    if (item.isPoint) {
      map.setView([item.minLat, item.minLng], Math.max(map.getZoom(), 18));
    } else {
      map.fitBounds(
        [
          [item.minLat, item.minLng],
          [item.maxLat, item.maxLng],
        ],
        { padding: [40, 40], maxZoom: 18 }
      );
    }
  }, [focusRequest, map]);

  const selectionBox = useMemo(
    () => (planIndex && showPlan && selectedIds.size >= 2 ? boundsOfItems(planIndex, selectedIds) : null),
    [planIndex, planRevision, showPlan, selectedIds]
  );

  return (
    <>
      {selectionBox && (
        <Rectangle
          bounds={boxBounds(selectionBox, moveOffset)}
          pane={SELECTION_PANE}
          pathOptions={{
            color: SELECTION_COLOR,
            weight: 1.5,
            dashArray: "6 4",
            fillColor: SELECTION_COLOR,
            fillOpacity: moveOffset ? 0.1 : 0.04,
            interactive: false,
          }}
        />
      )}
      {drawBox && (
        <Rectangle
          bounds={boxBounds(drawBox)}
          pane={SELECTION_PANE}
          pathOptions={{ color: SELECTION_COLOR, weight: 1.5, fillColor: SELECTION_COLOR, fillOpacity: 0.12, interactive: false }}
        />
      )}
    </>
  );
}

// A viewport-height of extra padding on each side, so a small pan doesn't
// visibly pop markers in right at the map's edge.
const VIEWPORT_PADDING_RATIO = 0.5;

// Cluster grid cell size, in screen pixels -- roughly a cluster bubble's own
// footprint, so neighbouring bubbles don't visually overlap. Bucketing is
// done in degrees, not by calling Leaflet's per-point pixel projection
// (map.latLngToContainerPoint) for every candidate: at a real plan's scale
// (hundreds of thousands of points can be "in view" at once when zoomed out
// to see a whole territory), that per-point API call is real, avoidable
// cost. Web Mercator's pixels-per-degree-of-longitude is constant at a
// given zoom regardless of latitude (256 * 2^zoom / 360) -- converting the
// desired pixel cell size to a degree cell size is therefore one division,
// and reusing that same degree size for latitude too is a deliberate
// approximation (exact for longitude, off by cos(latitude) for latitude) --
// fine at this project's scale (a single street/district, a sliver of
// latitude), same tolerance this codebase already accepts elsewhere for
// non-geodesic math at small scale (see plan3d.ts's own equirectangular
// projection). Under Leaflet's CRS.Simple (unverified projects, see
// crsVerified below) "degrees" are really raw local metres and the
// 256*2^zoom/360 constant above doesn't apply at all -- see
// pixelsPerLatLngUnit for the CRS-aware version this actually calls.
const CLUSTER_CELL_PX = 56;

// Below this many visible points, every one renders as a real marker, even
// if several land in the same 56px cell -- grid clustering alone made a
// hedge unreadable at any zoom short of "so close only a handful of shrubs
// fit on screen at all", because adjacent hedge points sit ~0.5m apart and
// 56 screen-px covers far more than 0.5m of ground at any normal zoom. The
// count that actually needs collapsing into bubbles is hundreds of
// thousands of items in view at once (a whole real territory zoomed out) --
// not a few hundred/thousand, which is what a normal working view shows and
// where every point being individually visible/selectable is the point.
// Reuses the same order-of-magnitude as the old VirtualizedMarkers
// threshold (MAX_VISIBLE_MARKERS) this component replaced.
const MAX_UNCLUSTERED_VISIBLE_POINTS = 4000;

/** Screen pixels per one unit of `latlng` space at a given zoom -- degrees
 * of longitude under the default WGS84/Mercator CRS (256*2^zoom pixels
 * span 360°, exact for longitude, off by cos(latitude) for latitude, see
 * this file's own note on CLUSTER_CELL_PX), or raw local metres directly
 * under Leaflet's CRS.Simple (its own scale(zoom) is just 2^zoom, no
 * 256/360 tile-based normalization -- see MapView's crs= prop). Getting
 * this wrong for the CRS.Simple case wouldn't crash anything, just silently
 * make every cluster cell far too small, so clustering would stop
 * triggering exactly where it matters most (a whole territory zoomed out).
 */
function pixelsPerLatLngUnit(zoom: number, crsVerified: boolean): number {
  return crsVerified ? (256 * Math.pow(2, zoom)) / 360 : Math.pow(2, zoom);
}

function clusterCellSizeDeg(zoom: number, crsVerified: boolean): number {
  return CLUSTER_CELL_PX / pixelsPerLatLngUnit(zoom, crsVerified);
}

function clusterBubbleSizePx(count: number): number {
  if (count < 10) return 28;
  if (count < 100) return 36;
  if (count < 1000) return 46;
  return 56;
}

function clusterDivIcon(count: number): L.DivIcon {
  const size = clusterBubbleSizePx(count);
  const label = count > 9999 ? "9999+" : count.toLocaleString("ru-RU");
  return L.divIcon({
    className: "gp-cluster-marker",
    html: `<span class="gp-cluster-bubble" style="width:${size}px;height:${size}px">${label}</span>`,
    iconSize: [size, size],
    iconAnchor: [size / 2, size / 2],
  });
}

interface MountedCluster {
  marker: L.Marker;
  count: number;
}

/** Renders point plan items (tree/shrub) as real Leaflet markers when
 * they're spaced out enough to be individually useful, and as cluster
 * bubbles (a count, not real geometry) when several fall into the same
 * screen-pixel cell at the current zoom -- a real-scale plan can hold
 * hundreds of thousands of items, and at any zoom where they'd render as an
 * unreadable smear of overlapping dots anyway, a handful of cluster bubbles
 * both reads better *and* costs a bounded number of DOM nodes regardless of
 * how many points are actually behind them (cell count is bounded by
 * viewport-pixels / CLUSTER_CELL_PX, not by plan size). Lawn polygons aren't
 * part of this: they stay in the ordinary <GeoJSON> layer in MapView below
 * (normally only a handful per plan, and "on screen" isn't a single
 * coordinate the way it is for a point).
 *
 * Only cells with exactly one point render a real, selectable/draggable
 * marker -- clusters are deliberately not wired into `registerLayer`'s id
 * registry (no `data-item-id`), so SelectionController's hit-testing simply
 * never finds one: a press on a cluster bubble falls through as an ordinary
 * map click/pan, and this component's own click handler on the bubble zooms
 * to its contents' bounds instead. Editing a group therefore always means
 * zooming in until it dissolves into real markers first -- deliberately
 * simpler and harder to fat-finger than resolving a cluster into hundreds of
 * ids for a single gesture.
 *
 * Real markers are created/destroyed and resynced through the *same*
 * `registerLayer` bookkeeping the always-mounted lawn layer uses below, same
 * as before clustering existed -- selection/violation highlighting doesn't
 * know or care whether a marker happens to be clustered away right now.
 */
function ClusteredMarkers({
  planIndex,
  planRevision,
  registerLayer,
  crsVerified,
}: {
  planIndex?: PlanIndex;
  planRevision: number;
  registerLayer: (id: string, type: string, layer: Layer) => void;
  crsVerified?: boolean;
}) {
  const map = useMap();
  const groupRef = useRef<L.LayerGroup | null>(null);
  const markersRef = useRef(new Map<string, L.Marker>());
  const clustersRef = useRef(new Map<string, MountedCluster>());

  if (!groupRef.current) groupRef.current = L.layerGroup();

  useEffect(() => {
    const group = groupRef.current;
    if (!group) return;
    group.addTo(map);
    return () => {
      group.remove();
    };
  }, [map]);

  const recompute = useCallback(() => {
    const group = groupRef.current;
    const markers = markersRef.current;
    const clusters = clustersRef.current;
    if (!group) return;
    if (!planIndex) {
      markers.forEach((marker) => marker.remove());
      markers.clear();
      clusters.forEach(({ marker }) => marker.remove());
      clusters.clear();
      return;
    }

    const bounds = map.getBounds().pad(VIEWPORT_PADDING_RATIO);
    const cellDeg = clusterCellSizeDeg(map.getZoom(), crsVerified ?? true);

    const visibleIds: string[] = [];
    planIndex.forEach((item) => {
      if (item.isPoint && bounds.contains([item.minLat, item.minLng])) visibleIds.push(item.id);
    });

    // cellKey -> ids of every visible point that lands in that cell. Below
    // the threshold, every point gets its own one-item "cell" (its own id as
    // the key) so nothing clusters regardless of how tightly packed it is on
    // screen -- see MAX_UNCLUSTERED_VISIBLE_POINTS.
    const buckets = new Map<string, string[]>();
    if (visibleIds.length > MAX_UNCLUSTERED_VISIBLE_POINTS) {
      for (const id of visibleIds) {
        const item = planIndex.get(id);
        if (!item) continue;
        const key = `${Math.floor(item.minLat / cellDeg)}:${Math.floor(item.minLng / cellDeg)}`;
        const bucket = buckets.get(key);
        if (bucket) bucket.push(id);
        else buckets.set(key, [id]);
      }
    } else {
      for (const id of visibleIds) buckets.set(id, [id]);
    }

    const nextSingles = new Set<string>();
    const nextClusters = new Map<string, string[]>();
    buckets.forEach((ids, key) => {
      if (ids.length === 1) nextSingles.add(ids[0]);
      else nextClusters.set(key, ids);
    });

    // Real markers -- same resync logic this had before clustering existed.
    markers.forEach((marker, id) => {
      if (!nextSingles.has(id)) {
        marker.remove();
        markers.delete(id);
      }
    });
    nextSingles.forEach((id) => {
      const item = planIndex.get(id);
      if (!item) return;
      const existing = markers.get(id);
      if (existing) {
        // Resync an already-mounted marker to the plan's actual data --
        // not just an optimization, this is load-bearing: a live drag
        // preview (SelectionController) moves a marker via a raw Leaflet
        // setLatLng, bypassing React entirely, before the server has
        // confirmed anything. If that move gets rejected (e.g. outside the
        // territory), the plan data never changes, so without this the
        // marker's on-screen position just silently keeps whatever the
        // preview last drew -- it never snaps back. Same story for a
        // retype's icon/color, which is baked into the marker at creation
        // and otherwise never revisited once mounted.
        const target = L.latLng(item.minLat, item.minLng);
        if (!existing.getLatLng().equals(target)) existing.setLatLng(target);
        if (existing.options.icon !== iconCache.get(item.type)) existing.setIcon(planItemIcon(item.type));
        return;
      }
      const marker = L.marker([item.minLat, item.minLng], { icon: planItemIcon(item.type), keyboard: false });
      registerLayer(id, item.type, marker);
      marker.addTo(group);
      markers.set(id, marker);
    });

    // Cluster bubbles -- cell keys are only stable within one zoom level
    // (cellDeg changes with zoom), so a zoom change naturally retires every
    // old bubble and mints fresh ones; a plain pan re-keys nothing.
    clusters.forEach(({ marker }, key) => {
      if (!nextClusters.has(key)) {
        marker.remove();
        clusters.delete(key);
      }
    });
    nextClusters.forEach((ids, key) => {
      const existing = clusters.get(key);
      if (existing) {
        // An edit (delete/move into or out of this cell) can change the
        // count under an otherwise-stable key -- re-render the bubble's
        // label/size when that happens, same "don't trust a stale visual"
        // reasoning as the real-marker resync above.
        if (existing.count !== ids.length) {
          existing.marker.setIcon(clusterDivIcon(ids.length));
          existing.count = ids.length;
        }
        return;
      }
      const [cellLat, cellLng] = key.split(":").map(Number);
      const marker = L.marker([(cellLat + 0.5) * cellDeg, (cellLng + 0.5) * cellDeg], {
        icon: clusterDivIcon(ids.length),
        keyboard: false,
        interactive: true,
      });
      marker.on("click", () => {
        const box = boundsOfItems(planIndex, ids);
        if (box) map.fitBounds([[box.minLat, box.minLng], [box.maxLat, box.maxLng]], { padding: [40, 40] });
      });
      marker.addTo(group);
      clusters.set(key, { marker, count: ids.length });
    });
  }, [map, planIndex, registerLayer, crsVerified]);

  useEffect(() => {
    recompute();
    // planRevision isn't read directly by recompute -- it's the signal that
    // plan content (positions/types) changed under the same ids. planIndex
    // is now patched in place rather than rebuilt per edit (see
    // useProjectSession.ts), so its *reference* no longer changes on every
    // edit either -- planRevision is what actually makes this effect re-run;
    // recompute() itself still reads planIndex's current (mutated) contents
    // live, since Maps are mutable regardless of when this closure was made.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [planIndex, planRevision, recompute]);

  useEffect(() => {
    return () => {
      markersRef.current.forEach((marker) => marker.remove());
      markersRef.current.clear();
      clustersRef.current.forEach(({ marker }) => marker.remove());
      clustersRef.current.clear();
    };
  }, []);

  useMapEvents({ moveend: recompute, zoomend: recompute });

  return null;
}

export interface MapViewProps {
  /** The loaded source drawing, already rendered server-side into one PNG
   * per legend group -- see layer_raster.py's docstring for why this is
   * pixels, not GeoJSON features, by the time it reaches the map. */
  layersRaster?: LayersRaster | null;
  /** Whether project.source_crs is trustworthy enough to show a real
   * OpenStreetMap basemap under the plan -- see backend's
   * Project.crs_verified for exactly what "trustworthy" means here (auto-
   * detected from the uploaded file's own coordinates, never a typed-in
   * guess, and never true for a DXF/DWG upload -- that format carries no
   * CRS metadata at all). Undefined/false switches the whole map to
   * Leaflet's CRS.Simple (see the crs= prop below) and shows a neutral
   * CAD-style grid instead of OSM tiles: every coordinate `layersRaster`/
   * `plan` carries is then raw local metres, not WGS84 degrees (see
   * backend's geo_io.py::display_crs), because there's no known real-world
   * placement to reproject them onto -- reprojecting a plausible-but-
   * unverified CRS onto a real basemap is exactly what produced the
   * Kenya-map bug (see CLAUDE.md). */
  crsVerified?: boolean;
  plan?: GeoJSONFeatureCollection;
  planIndex?: PlanIndex;
  /** Identify which upload/plan `plan` came from — react-leaflet's
   * <GeoJSON> does not reactively re-diff its `data` prop, so the plan layer
   * below remounts on a key tied to the actual data identity. `planRevision`
   * bumps on every reload of the same plan (after an edit), so the edit
   * actually shows up. */
  layersKey?: string;
  planId?: string;
  planRevision?: number;
  /** Source-layer group keys (see layer_raster.py::layer_group_key, mirrored
   * from lib/mapStyle.ts::layerGroupKey) currently hidden via the panel's
   * per-type toggles -- replaces a single blanket "show layers" flag so
   * buildings/greenery/etc. can be shown independently. */
  hiddenLayerTypes?: ReadonlySet<string>;
  showPlan?: boolean;
  center?: [number, number];
  zoom?: number;
  selectMode: boolean;
  selectedIds: ReadonlySet<string>;
  /** Items that break a setback norm — painted red. */
  violationIds: ReadonlySet<string>;
  /** True while an edit is being saved; new drag-moves don't start meanwhile. */
  editsLocked: boolean;
  onSelectionChange: (ids: string[]) => void;
  onMoveItems: (ids: string[], from: LngLat, to: LngLat) => void;
  onContextMenu: (clientX: number, clientY: number) => void;
  /** The map started panning/zooming (e.g. to close an open context menu). */
  onViewChange?: () => void;
  /** Pan/zoom to this item; `seq` makes a repeat request for the same id re-fire. */
  focusRequest?: { id: string; seq: number } | null;
  /** Whether the control panel is currently shown -- purely to know when its
   * CSS width transition finishes, so the map's container size can be
   * recomputed (see InvalidateSizeOnChange below). Not otherwise used. */
  sidebarOpen?: boolean;
}

export default function MapView({
  layersRaster,
  crsVerified,
  plan,
  planIndex,
  layersKey,
  planId,
  planRevision = 0,
  hiddenLayerTypes,
  showPlan = true,
  center = [55.751244, 37.618423],
  zoom = 12,
  selectMode,
  selectedIds,
  violationIds,
  editsLocked,
  onSelectionChange,
  onMoveItems,
  onContextMenu,
  onViewChange,
  focusRequest,
  sidebarOpen,
}: MapViewProps) {
  // item id -> its Leaflet layer while on the map, so selection/violation
  // highlighting can restyle just the items whose state changed instead of
  // remounting every marker (maintained through each layer's add/remove
  // events, so it's correct however react-leaflet (re)creates the layer group).
  const registryRef = useRef(new Map<string, RegistryEntry>());
  const stateRef = useRef({ selectedIds, violationIds });
  stateRef.current = { selectedIds, violationIds };
  const appliedRef = useRef({ selectedIds, violationIds });

  function decorate(id: string, entry: RegistryEntry) {
    const selected = stateRef.current.selectedIds.has(id);
    const violation = stateRef.current.violationIds.has(id);
    const { layer } = entry;
    if (layer instanceof L.Marker) {
      const el = layer.getElement();
      if (!el) return;
      el.setAttribute("data-item-id", id);
      el.classList.toggle("gp-selected", selected);
      el.classList.toggle("gp-violation", violation);
    } else if (layer instanceof L.Path) {
      layer.setStyle(areaStyle(entry.type, selected, violation));
      layer.getElement()?.setAttribute("data-item-id", id);
    }
  }

  const registerLayer = useCallback((id: string, type: string, layer: Layer) => {
    const entry: RegistryEntry = { layer, type };
    layer.on("add", () => {
      registryRef.current.set(id, entry);
      decorate(id, entry);
    });
    layer.on("remove", () => {
      if (registryRef.current.get(id) === entry) registryRef.current.delete(id);
    });
    // decorate only reads refs
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function registerPlanFeature(feature: Feature, layer: Layer) {
    registerLayer(String(feature.properties?.id), String(feature.properties?.planting_type ?? ""), layer);
  }

  // Lawn polygons only -- point items (tree/shrub) are handled by
  // ClusteredMarkers below instead, which mounts only whichever ones are
  // actually in view, individually or as cluster bubbles (see its own
  // docstring). Every lawn a plan has always comes back
  // planting_type="lawn"/geometry.type!=="Point" together (see the
  // planting_type<->geometry contract in CLAUDE.md), so this filter is
  // exactly "everything ClusteredMarkers doesn't already cover".
  const areaFeatures = useMemo(
    () => (plan ? { ...plan, features: plan.features.filter((f) => f.geometry.type !== "Point") } : undefined),
    [plan]
  );

  // Only the currently-toggled-on source layer groups (see ControlPanel.tsx's
  // per-type legend) -- each group is already its own PNG, so "hidden" just
  // means "don't mount that <ImageOverlay>", no client-side feature filtering.
  const visibleRasterGroups = useMemo(() => {
    if (!layersRaster) return [];
    if (!hiddenLayerTypes || hiddenLayerTypes.size === 0) return layersRaster.groups;
    return layersRaster.groups.filter((g) => !hiddenLayerTypes.has(g.key));
  }, [layersRaster, hiddenLayerTypes]);

  // The overall extent of everything loaded, straight from the raster
  // endpoint's own bounds (it already excludes zoning for the same reason
  // extentBounds used to compute this client-side: a real zoning polygon's
  // shape can span a whole neighborhood, and letting it set the frame would
  // zoom the actual site down to a speck -- see layer_raster.py).
  const extentBounds = layersRaster?.bounds ?? null;

  useEffect(() => {
    const previous = appliedRef.current;
    const changed = new Set<string>();
    const collectChanges = (before: ReadonlySet<string>, after: ReadonlySet<string>) => {
      if (before === after) return;
      before.forEach((id) => !after.has(id) && changed.add(id));
      after.forEach((id) => !before.has(id) && changed.add(id));
    };
    collectChanges(previous.selectedIds, selectedIds);
    collectChanges(previous.violationIds, violationIds);
    changed.forEach((id) => {
      const entry = registryRef.current.get(id);
      if (entry) decorate(id, entry);
    });
    appliedRef.current = { selectedIds, violationIds };
    // decorate only reads refs
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selectedIds, violationIds]);

  return (
    <MapContainer
      // Leaflet's Map object fixes its CRS at construction and never
      // re-reads this prop -- keyed on crsVerified so switching between a
      // verified and an unverified project (rare, but possible across two
      // uploads in the same session) tears down and rebuilds the Leaflet
      // instance instead of silently keeping the old, now-wrong CRS.
      // Switching between two projects that share the same crsVerified
      // value (by far the common case) doesn't remount anything.
      key={crsVerified ? "geo" : "local"}
      // A real drawing's coordinates are raw local metres, not WGS84 degrees
      // (see backend's geo_io.py::display_crs) -- CRS.Simple treats them as
      // plain Cartesian units (north stays up: its default transformation
      // just flips pixel-y, same convention as any northing-increases-north
      // survey drawing) instead of running them through Web Mercator, which
      // is undefined outside real longitude/latitude ranges and is exactly
      // what produced the Kenya-map bug for a plausible-but-unverified CRS.
      crs={crsVerified ? undefined : L.CRS.Simple}
      center={crsVerified ? center : [0, 0]}
      zoom={crsVerified ? zoom : 0}
      // CRS.Simple has no fixed real-world tile size to bottom out at --
      // without a generously negative floor, FitBounds can't zoom out far
      // enough to fit a territory bigger than one screen's worth of metres
      // at zoom 0 (1 CRS unit = 1px there).
      minZoom={crsVerified ? undefined : -20}
      // 19 is roughly where OSM's own tiles stop getting sharper; the plan
      // itself is vector (real markers, not tiles) and benefits from going
      // deeper than that -- planting_norms.yaml allows spacing as tight as
      // 0.3m, which at zoom 19 is still sub-pixel at Moscow's latitude and
      // needs a couple more zoom levels to actually separate on screen.
      maxZoom={21}
      className={`h-full w-full ${crsVerified ? "" : "gp-neutral-map-bg"}`}
      attributionControl={false}
    >
      {/* prefix={false} drops the "Leaflet" credit -- just a courtesy line,
          not a license requirement (Leaflet is BSD-2-Clause). The OSM
          copyright below stays: that one *is* required by OpenStreetMap's
          tile usage policy for using their tiles at all. */}
      <AttributionControl prefix={false} />
      {crsVerified && (
        <TileLayer
          attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
          url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
          maxNativeZoom={19}
        />
      )}
      <FitBounds fitKey={`${layersKey ?? ""}:${planId ?? ""}`} layersBounds={extentBounds} plan={plan} />
      <InvalidateSizeOnChange trigger={sidebarOpen} />
      {extentBounds && (
        <Rectangle
          bounds={extentBounds}
          pathOptions={{ color: "#1e293b", weight: 1.5, dashArray: "6 4", fill: false }}
          interactive={false}
        />
      )}
      {/* The source drawing itself -- one flat PNG per legend group, all
          sharing `extentBounds` so they stack in register regardless of how
          many objects any one group holds (see layer_raster.py). Not
          interactive: nothing here was ever clickable (only plan.features
          is), so there's no hit-testing to wire up. */}
      {extentBounds &&
        visibleRasterGroups.map((group) => (
          <ImageOverlay key={`${layersKey ?? "layers"}:${group.key}`} url={group.url} bounds={extentBounds} />
        ))}
      {areaFeatures && showPlan && (
        <GeoJSON
          key={`${planId ?? "plan"}:${planRevision}`}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          data={areaFeatures as any}
          style={planStyle}
          onEachFeature={registerPlanFeature}
        />
      )}
      {showPlan && (
        <ClusteredMarkers planIndex={planIndex} planRevision={planRevision} registerLayer={registerLayer} crsVerified={crsVerified} />
      )}
      <SelectionController
        selectMode={selectMode}
        editsLocked={editsLocked}
        showPlan={showPlan}
        planIndex={planIndex}
        planRevision={planRevision}
        selectedIds={selectedIds}
        registryRef={registryRef}
        onSelectionChange={onSelectionChange}
        onMoveItems={onMoveItems}
        onContextMenu={onContextMenu}
        onViewChange={onViewChange}
        focusRequest={focusRequest}
      />
    </MapContainer>
  );
}
