"use client";

import { useEffect } from "react";
import "leaflet/dist/leaflet.css";
import { Circle, CircleMarker, MapContainer, Polygon, Polyline, TileLayer, GeoJSON, useMap, useMapEvents } from "react-leaflet";
import L, { type Layer, type PathOptions } from "leaflet";
import type { Feature } from "geojson";

import type { EditMode, GeoJSONFeatureCollection, PlantingType } from "@/lib/api";

const UTILITY_COLOR = "#b91c1c"; // red — exclusion-driving constraints
const ZONE_COLORS: Record<string, string> = {
  building: "#78716c",
  road: "#57534e",
  territory: "#0ea5e9",
  zoning: "#a78bfa",
  existing_greenery: "#16a34a",
};
const PLANTING_COLORS: Record<string, string> = {
  tree: "#15803d",
  shrub: "#65a30d",
  lawn: "#a3e635",
};
const PLANTING_TYPE_LABELS: Record<PlantingType, string> = {
  tree: "Дерево",
  shrub: "Куст",
  lawn: "Газон",
};

function layerStyle(feature?: Feature): PathOptions {
  const props = (feature?.properties ?? {}) as Record<string, unknown>;
  if (props.kind === "utility") {
    return { color: UTILITY_COLOR, weight: 2 };
  }
  const zoneType = String(props.zone_type ?? props.object_type ?? "");
  return { color: ZONE_COLORS[zoneType] ?? "#9ca3af", weight: 1, fillOpacity: 0.15 };
}

function planStyle(feature?: Feature): PathOptions {
  const plantingType = String((feature?.properties as Record<string, unknown> | undefined)?.planting_type ?? "");
  const color = PLANTING_COLORS[plantingType] ?? "#22c55e";
  return { color, fillColor: color, fillOpacity: 0.7, weight: 1 };
}

/** A small colored circle drawn with plain CSS, not an image asset — a
 * standard L.Marker's default icon points at image files the bundler
 * doesn't resolve (see git history), which is why plan items used to render
 * as non-draggable CircleMarkers instead of Markers. A divIcon sidesteps
 * that entirely (no external asset) while still getting Leaflet's built-in
 * marker dragging, which CircleMarker (an SVG Path, not a Marker) doesn't
 * support without an extra plugin.
 */
function planItemIcon(feature?: Feature): L.DivIcon {
  const plantingType = String((feature?.properties as Record<string, unknown> | undefined)?.planting_type ?? "");
  const color = PLANTING_COLORS[plantingType] ?? "#22c55e";
  return L.divIcon({
    className: "",
    html: `<div style="width:10px;height:10px;border-radius:50%;background:${color};border:1px solid white;box-shadow:0 0 1px rgba(0,0,0,0.6);"></div>`,
    iconSize: [10, 10],
    iconAnchor: [5, 5],
  });
}

function bindPopup(feature: Feature, layer: Layer) {
  const props = feature.properties as Record<string, unknown> | undefined;
  if (!props) return;
  const lines = Object.entries(props)
    .map(([key, value]) => `<b>${key}</b>: ${String(value)}`)
    .join("<br/>");
  layer.bindPopup(lines);
}

/** Point plan items (tree/shrub) get a draggable marker plus a popup with
 * retype/delete buttons — the "click a tree, move/delete/retype it" manual
 * editing that the 3 structured operations (radius/polygon-based) don't
 * cover. Lawn items are polygons (a different geometry type entirely, not
 * routed through this) and keep the plain read-only popup.
 */
function bindPlanItemInteractions(
  feature: Feature,
  layer: Layer,
  callbacks: {
    onMoveItem?: (itemId: string, lat: number, lng: number) => void;
    onRetypeItem?: (itemId: string, type: PlantingType) => void;
    onDeleteItem?: (itemId: string) => void;
  }
) {
  const props = feature.properties as Record<string, unknown> | undefined;
  if (!props || !(layer instanceof L.Marker)) {
    bindPopup(feature, layer);
    return;
  }

  const itemId = String(props.id);
  const currentType = String(props.planting_type);
  const score = Number(props.score);

  layer.on("dragend", () => {
    const latlng = (layer as L.Marker).getLatLng();
    callbacks.onMoveItem?.(itemId, latlng.lat, latlng.lng);
  });

  const container = L.DomUtil.create("div", "text-xs flex flex-col gap-1");
  const scoreLine = L.DomUtil.create("div", "font-medium", container);
  scoreLine.textContent = `Оценка: ${Number.isFinite(score) ? score.toFixed(2) : "—"}`;

  // "lawn" is a polygon type in this app's model (generate_area_candidates
  // makes it a whole sub-polygon, not a point) -- retyping a point item
  // (tree/shrub) to it would leave planting_type="lawn" on a Point geometry,
  // a combination nothing else in the pipeline expects. Only offer the two
  // point-shaped types here.
  const typeRow = L.DomUtil.create("div", "flex gap-1", container);
  (["tree", "shrub"] as PlantingType[]).forEach((type) => {
    const btn = L.DomUtil.create("button", "rounded border px-1.5 py-0.5", typeRow) as HTMLButtonElement;
    btn.textContent = PLANTING_TYPE_LABELS[type];
    btn.style.fontWeight = type === currentType ? "700" : "400";
    btn.style.borderColor = type === currentType ? "#15803d" : "#d6d3d1";
    L.DomEvent.on(btn, "click", (e) => {
      L.DomEvent.stop(e);
      callbacks.onRetypeItem?.(itemId, type);
      layer.closePopup();
    });
  });

  const deleteBtn = L.DomUtil.create("button", "mt-1 rounded border border-red-300 px-1.5 py-0.5 text-red-700", container) as HTMLButtonElement;
  deleteBtn.textContent = "Удалить";
  L.DomEvent.on(deleteBtn, "click", (e) => {
    L.DomEvent.stop(e);
    callbacks.onDeleteItem?.(itemId);
    layer.closePopup();
  });

  layer.bindPopup(container);
}

/** Zooms/pans to fit whatever data is actually loaded, instead of assuming
 * a fixed real-world center — territories may have no known CRS yet (see
 * geo_io._to_wgs84) and land at whatever raw coordinates they were stored
 * in, which are meaningless to center on in advance.
 */
function FitBounds({ layers, plan }: { layers?: GeoJSONFeatureCollection; plan?: GeoJSONFeatureCollection }) {
  const map = useMap();

  useEffect(() => {
    const collections = [layers, plan].filter(Boolean) as GeoJSONFeatureCollection[];
    if (collections.length === 0) return;

    const bounds = L.latLngBounds([]);
    for (const collection of collections) {
      // eslint-disable-next-line @typescript-eslint/no-explicit-any
      const layerBounds = L.geoJSON(collection as any).getBounds();
      if (layerBounds.isValid()) bounds.extend(layerBounds);
    }
    if (bounds.isValid()) map.fitBounds(bounds, { padding: [20, 20] });
  }, [layers, plan, map]);

  return null;
}

/** Captures map clicks while a structured edit is being drawn (editMode !==
 * "none") and previews what's been drawn so far — a marker+circle for
 * remove_within_radius, a growing polygon outline for the polygon-based
 * operations. Coordinates are reported as [lat, lng] (Leaflet/WGS84, same as
 * everything else in this component) — the caller is responsible for
 * converting to the [lng, lat] GeoJSON order the API expects.
 */
function EditLayer({
  editMode,
  editCenter,
  editRadiusM,
  editPolygon,
  onMapClick,
}: {
  editMode: EditMode;
  editCenter: [number, number] | null;
  editRadiusM: number;
  editPolygon: [number, number][];
  onMapClick: (lat: number, lng: number) => void;
}) {
  useMapEvents({
    click(e) {
      if (editMode !== "none") onMapClick(e.latlng.lat, e.latlng.lng);
    },
  });

  if (editMode === "remove_within_radius") {
    return editCenter ? (
      <>
        <CircleMarker center={editCenter} radius={5} pathOptions={{ color: "#dc2626", fillColor: "#dc2626", fillOpacity: 1 }} />
        <Circle center={editCenter} radius={editRadiusM} pathOptions={{ color: "#dc2626", fillOpacity: 0.1 }} />
      </>
    ) : null;
  }

  if (editMode === "exclude_polygon" || editMode === "replace_type_in_zone") {
    if (editPolygon.length === 0) return null;
    const pathOptions = { color: "#dc2626", fillOpacity: 0.15 };
    return editPolygon.length >= 3 ? (
      <Polygon positions={editPolygon} pathOptions={pathOptions} />
    ) : (
      <Polyline positions={editPolygon} pathOptions={pathOptions} />
    );
  }

  return null;
}

export interface MapViewProps {
  layers?: GeoJSONFeatureCollection;
  plan?: GeoJSONFeatureCollection;
  /** Identify which upload/generation `layers`/`plan` came from (e.g.
   * project.id / plan.plan_id) so the GeoJSON layers below remount instead
   * of leaving stale Leaflet features on the map — react-leaflet's
   * <GeoJSON> does not reactively re-diff its `data` prop, so without a
   * `key` tied to the actual data identity, uploading a new project can
   * leave the previous one's shapes rendered underneath the new ones. */
  layersKey?: string;
  planKey?: string;
  showLayers?: boolean;
  showPlan?: boolean;
  center?: [number, number];
  zoom?: number;
  editMode?: EditMode;
  editCenter?: [number, number] | null;
  editRadiusM?: number;
  editPolygon?: [number, number][];
  onMapClick?: (lat: number, lng: number) => void;
  /** Click-a-tree/shrub manual editing: drag to move, popup buttons to
   * retype or delete. Independent of editMode/onMapClick above (those drive
   * the area-based structured operations) — always active on plan point
   * items when provided. */
  onMoveItem?: (itemId: string, lat: number, lng: number) => void;
  onRetypeItem?: (itemId: string, type: PlantingType) => void;
  onDeleteItem?: (itemId: string) => void;
}

export default function MapView({
  layers,
  plan,
  layersKey,
  planKey,
  showLayers = true,
  showPlan = true,
  center = [55.751244, 37.618423],
  zoom = 12,
  editMode = "none",
  editCenter = null,
  editRadiusM = 10,
  editPolygon = [],
  onMapClick,
  onMoveItem,
  onRetypeItem,
  onDeleteItem,
}: MapViewProps) {
  return (
    <MapContainer center={center} zoom={zoom} className="h-full w-full">
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <FitBounds layers={layers} plan={plan} />
      {layers && showLayers && (
        <GeoJSON
          key={layersKey ?? "layers"}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          data={layers as any}
          style={layerStyle}
          onEachFeature={bindPopup}
        />
      )}
      {plan && showPlan && (
        <GeoJSON
          key={planKey ?? "plan"}
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          data={plan as any}
          style={planStyle}
          pointToLayer={(feature, latlng) => L.marker(latlng, { icon: planItemIcon(feature), draggable: true })}
          onEachFeature={(feature, layer) => bindPlanItemInteractions(feature, layer, { onMoveItem, onRetypeItem, onDeleteItem })}
        />
      )}
      {onMapClick && (
        <EditLayer editMode={editMode} editCenter={editCenter} editRadiusM={editRadiusM} editPolygon={editPolygon} onMapClick={onMapClick} />
      )}
    </MapContainer>
  );
}
