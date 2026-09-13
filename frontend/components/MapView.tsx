"use client";

import { useCallback, useEffect, useMemo, useRef, useState, type MutableRefObject } from "react";
import "leaflet/dist/leaflet.css";
import { AttributionControl, MapContainer, Rectangle, TileLayer, GeoJSON, useMap, useMapEvents } from "react-leaflet";
import L, { type Layer, type PathOptions } from "leaflet";
import type { Feature } from "geojson";

import type { GeoJSONFeature, GeoJSONFeatureCollection, LngLat } from "@/lib/api";
import { isZoningFeature, layerGroupColor, layerGroupKey, plantingColor } from "@/lib/mapStyle";
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

function layerStyle(feature?: Feature): PathOptions {
  const key = layerGroupKey((feature ?? { type: "Feature", properties: {} }) as unknown as GeoJSONFeature);
  const color = layerGroupColor(key);
  if (key === "utility") return { color, weight: 2 };
  // fillOpacity used to be 0.15 -- barely visible against the basemap,
  // which is exactly why the legend (ControlPanel.tsx) exists now: colors
  // have to actually read as distinct fills, not just a faint tint.
  return { color, weight: 1, fillOpacity: 0.45 };
}

function areaStyle(type: string, selected: boolean, violation: boolean): PathOptions {
  const base = plantingColor(type);
  return {
    color: selected ? SELECTION_COLOR : violation ? VIOLATION_COLOR : base,
    fillColor: violation ? VIOLATION_COLOR : base,
    fillOpacity: violation ? 0.45 : 0.7,
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

function bindPopup(feature: Feature, layer: Layer) {
  const props = feature.properties as Record<string, unknown> | undefined;
  if (!props) return;
  const lines = Object.entries(props)
    .map(([key, value]) => `<b>${key}</b>: ${String(value)}`)
    .join("<br/>");
  layer.bindPopup(lines);
}

/** Zooms/pans to fit the loaded data — only when a different project or
 * plan is opened (`fitKey`), not after every edit of the same plan, which
 * would yank the view away from wherever the user zoomed in to edit. */
function FitBounds({ fitKey, layers, plan }: { fitKey: string; layers?: GeoJSONFeatureCollection; plan?: GeoJSONFeatureCollection }) {
  const map = useMap();
  const dataRef = useRef({ layers, plan });
  dataRef.current = { layers, plan };

  useEffect(() => {
    const collections = [dataRef.current.layers, dataRef.current.plan].filter(Boolean) as GeoJSONFeatureCollection[];
    if (collections.length === 0) return;

    const bounds = L.latLngBounds([]);
    for (const collection of collections) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const layerBounds = L.geoJSON(collection as any).getBounds();
      if (layerBounds.isValid()) bounds.extend(layerBounds);
    }
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
  }, [fitKey, map]);

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
// Caps how many DOM markers a single dense viewport can force into
// existence at once -- past this, individual markers stop being useful
// anyway (they'd overlap into an unreadable smear), so a "zoom in" hint
// takes over instead of paying the mount cost.
const MAX_VISIBLE_MARKERS = 4000;

/** Renders point plan items (tree/shrub) as real Leaflet markers, but only
 * for whichever ones currently fall inside the (padded) viewport -- a
 * real-scale plan can hold hundreds of thousands of items, and mounting one
 * DOM marker per item regardless of what's actually on screen doesn't scale
 * (see CLAUDE.md's rendering performance notes). Lawn polygons aren't
 * virtualized (they stay in the ordinary <GeoJSON> layer in MapView below):
 * there's normally only a handful of them, and "on screen" isn't a single
 * coordinate check for an area the way it is for a point.
 *
 * Markers are created/destroyed imperatively on `moveend`/`zoomend` and
 * whenever the plan itself changes, through the *same* `registerLayer`
 * bookkeeping the always-mounted lawn layer already uses below -- so
 * selecting, dragging, and violation/selection highlighting work
 * identically whether a given item happens to be virtualized in right now
 * or not; SelectionController's hit-testing only ever looks at whatever DOM
 * element is actually under the pointer, never caring how it got there.
 */
function VirtualizedMarkers({
  planIndex,
  planRevision,
  registerLayer,
}: {
  planIndex?: PlanIndex;
  planRevision: number;
  registerLayer: (id: string, type: string, layer: Layer) => void;
}) {
  const map = useMap();
  const groupRef = useRef<L.LayerGroup | null>(null);
  const mountedRef = useRef(new Map<string, L.Marker>());
  const [visibleCount, setVisibleCount] = useState(0);
  const [overflow, setOverflow] = useState(false);

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
    const mounted = mountedRef.current;
    if (!group) return;
    if (!planIndex) {
      mounted.forEach((marker) => marker.remove());
      mounted.clear();
      setOverflow(false);
      setVisibleCount(0);
      return;
    }

    const bounds = map.getBounds().pad(VIEWPORT_PADDING_RATIO);
    const visible: string[] = [];
    planIndex.forEach((item) => {
      if (item.isPoint && bounds.contains([item.minLat, item.minLng])) visible.push(item.id);
    });

    if (visible.length > MAX_VISIBLE_MARKERS) {
      mounted.forEach((marker) => marker.remove());
      mounted.clear();
      setOverflow(true);
      setVisibleCount(visible.length);
      return;
    }
    setOverflow(false);
    setVisibleCount(visible.length);

    const visibleSet = new Set(visible);
    mounted.forEach((marker, id) => {
      if (!visibleSet.has(id)) {
        marker.remove();
        mounted.delete(id);
      }
    });
    for (const id of visible) {
      const item = planIndex.get(id);
      if (!item) continue;
      const existing = mounted.get(id);
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
        continue;
      }
      const marker = L.marker([item.minLat, item.minLng], { icon: planItemIcon(item.type), keyboard: false });
      registerLayer(id, item.type, marker);
      marker.addTo(group);
      mounted.set(id, marker);
    }
  }, [map, planIndex, registerLayer]);

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
      mountedRef.current.forEach((marker) => marker.remove());
      mountedRef.current.clear();
    };
  }, []);

  useMapEvents({ moveend: recompute, zoomend: recompute });

  if (!overflow) return null;
  return (
    <div className="pointer-events-none absolute inset-x-0 top-3 z-[1000] flex justify-center">
      <div className="rounded-full border border-stone-200 bg-white/95 px-3 py-1.5 text-xs text-stone-700 shadow">
        В кадре {visibleCount.toLocaleString("ru-RU")} объектов — приблизьте карту, чтобы увидеть и редактировать их
      </div>
    </div>
  );
}

export interface MapViewProps {
  layers?: GeoJSONFeatureCollection;
  plan?: GeoJSONFeatureCollection;
  planIndex?: PlanIndex;
  /** Identify which upload/plan `layers`/`plan` came from — react-leaflet's
   * <GeoJSON> does not reactively re-diff its `data` prop, so the layers
   * below remount on a key tied to the actual data identity. `planRevision`
   * bumps on every reload of the same plan (after an edit), so the edit
   * actually shows up. */
  layersKey?: string;
  planId?: string;
  planRevision?: number;
  /** Source-layer types (see lib/mapStyle.ts::layerGroupKey) currently
   * hidden via the panel's per-type toggles -- replaces a single blanket
   * "show layers" flag so buildings/greenery/etc. can be shown independently. */
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
}

export default function MapView({
  layers,
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
  // VirtualizedMarkers below instead, which mounts only whichever ones are
  // actually in view (see its own docstring). Every lawn a plan has always
  // comes back planting_type="lawn"/geometry.type!=="Point" together (see
  // the planting_type<->geometry contract in CLAUDE.md), so this filter is
  // exactly "everything virtualization doesn't already cover".
  const areaFeatures = useMemo(
    () => (plan ? { ...plan, features: plan.features.filter((f) => f.geometry.type !== "Point") } : undefined),
    [plan]
  );

  // Only the currently-toggled-on source layer types (see ControlPanel.tsx's
  // per-type legend) -- filtered client-side rather than asking the backend
  // for less, since these are the same few dozen/hundred zone features
  // either way, not something worth a network round trip over.
  const visibleLayers = useMemo(() => {
    if (!layers) return undefined;
    if (!hiddenLayerTypes || hiddenLayerTypes.size === 0) return layers;
    return { ...layers, features: layers.features.filter((f) => !hiddenLayerTypes.has(layerGroupKey(f))) };
  }, [layers, hiddenLayerTypes]);
  // <GeoJSON> doesn't reactively re-diff `data` (see layersKey's own
  // docstring) -- toggling a layer type needs the same key-bump remount
  // trick, keyed on which types are hidden right now.
  const hiddenLayersSignature = hiddenLayerTypes ? [...hiddenLayerTypes].sort().join(",") : "";

  // The overall extent of everything loaded (not just the currently-visible
  // subset) -- a stable reference rectangle so a user toggling individual
  // layers on/off (or looking at an irregular real territory instead of a
  // synthetic square) can still see "this is the whole working area".
  // Excludes zoning features on purpose (any zoning_category): unlike every
  // other layer, a real zoning polygon's authentic shape can span a whole
  // neighborhood (its own boundary follows real streets/blocks, so it isn't
  // clipped down to the project's own scale the way roads/buildings are --
  // see data/README.md's real_moscow_courtyard.geojson notes) -- including
  // it here would balloon this rectangle out to that same neighborhood
  // instead of framing the actual site.
  const extentBounds = useMemo(() => {
    if (!layers) return null;
    const boundedFeatures = layers.features.filter((f) => !isZoningFeature(f));
    if (boundedFeatures.length === 0) return null;
    try {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const bounds = L.geoJSON({ type: "FeatureCollection", features: boundedFeatures } as any).getBounds();
      return bounds.isValid() ? bounds : null;
    } catch {
      return null;
    }
  }, [layers]);

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
    <MapContainer center={center} zoom={zoom} className="h-full w-full" attributionControl={false}>
      {/* prefix={false} drops the "Leaflet" credit -- just a courtesy line,
          not a license requirement (Leaflet is BSD-2-Clause). The OSM
          copyright below stays: that one *is* required by OpenStreetMap's
          tile usage policy for using their tiles at all. */}
      <AttributionControl prefix={false} />
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <FitBounds fitKey={`${layersKey ?? ""}:${planId ?? ""}`} layers={layers} plan={plan} />
      {extentBounds && (
        <Rectangle
          bounds={extentBounds}
          pathOptions={{ color: "#1e293b", weight: 1.5, dashArray: "6 4", fill: false }}
          interactive={false}
        />
      )}
      {visibleLayers && visibleLayers.features.length > 0 && (
        <GeoJSON
          key={`${layersKey ?? "layers"}:${hiddenLayersSignature}`}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          data={visibleLayers as any}
          style={layerStyle}
          onEachFeature={bindPopup}
        />
      )}
      {areaFeatures && showPlan && (
        <GeoJSON
          key={`${planId ?? "plan"}:${planRevision}`}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          data={areaFeatures as any}
          style={planStyle}
          onEachFeature={registerPlanFeature}
        />
      )}
      {showPlan && <VirtualizedMarkers planIndex={planIndex} planRevision={planRevision} registerLayer={registerLayer} />}
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
