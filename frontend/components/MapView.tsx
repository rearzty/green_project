"use client";

import { useEffect } from "react";
import "leaflet/dist/leaflet.css";
import { MapContainer, TileLayer, GeoJSON, useMap } from "react-leaflet";
import L, { type CircleMarkerOptions, type Layer, type PathOptions } from "leaflet";
import type { Feature } from "geojson";

import type { GeoJSONFeatureCollection } from "@/lib/api";

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

function planCircleStyle(feature?: Feature): CircleMarkerOptions {
  return { ...planStyle(feature), radius: 4 };
}

function bindPopup(feature: Feature, layer: Layer) {
  const props = feature.properties as Record<string, unknown> | undefined;
  if (!props) return;
  const lines = Object.entries(props)
    .map(([key, value]) => `<b>${key}</b>: ${String(value)}`)
    .join("<br/>");
  layer.bindPopup(lines);
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

export interface MapViewProps {
  layers?: GeoJSONFeatureCollection;
  plan?: GeoJSONFeatureCollection;
  center?: [number, number];
  zoom?: number;
}

export default function MapView({ layers, plan, center = [55.751244, 37.618423], zoom = 12 }: MapViewProps) {
  return (
    <MapContainer center={center} zoom={zoom} className="h-full w-full">
      <TileLayer
        attribution='&copy; <a href="https://www.openstreetmap.org/copyright">OpenStreetMap</a> contributors'
        url="https://{s}.tile.openstreetmap.org/{z}/{x}/{y}.png"
      />
      <FitBounds layers={layers} plan={plan} />
      {layers && (
        // eslint-disable-next-line @typescript-eslint/no-explicit-any
        <GeoJSON data={layers as any} style={layerStyle} onEachFeature={bindPopup} />
      )}
      {plan && (
        <GeoJSON
          // eslint-disable-next-line @typescript-eslint/no-explicit-any
          data={plan as any}
          style={planStyle}
          pointToLayer={(feature, latlng) => L.circleMarker(latlng, planCircleStyle(feature))}
          onEachFeature={bindPopup}
        />
      )}
    </MapContainer>
  );
}
