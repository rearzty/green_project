"use client";

import { useEffect, useRef, useState } from "react";
import type { Map as LeafletMap } from "leaflet";

import { MAP_BG_COLOR, PLANTING_COLORS, VIOLATION_COLOR } from "@/lib/mapStyle";

export interface LegendItem {
  key: string;
  label: string;
  swatch: "tree" | "shrub" | "lawn" | "violation" | "dash";
  color?: string;
}

/** Метров в одном пикселе экрана в центре карты. Меряется самой картой
 * (`map.distance` по двум точкам на расстоянии 100 px), а не формулой
 * Меркатора: так одинаково верно и для геодезической СК, и для локальных
 * метров чертежа под CRS.Simple (там distance — просто евклидово). */
function metersPerPixel(map: LeafletMap): number {
  const y = map.getSize().y / 2;
  const a = map.containerPointToLatLng([0, y]);
  const b = map.containerPointToLatLng([100, y]);
  return map.distance(a, b) / 100;
}

/** Ближайшее «круглое» число (1, 2, 5 × 10ⁿ) метров, которое укладывается
 * в 120 px линейки, и её ширина в пикселях. */
function niceScale(mpp: number): { meters: number; px: number } | null {
  if (!isFinite(mpp) || mpp <= 0) return null;
  const maxMeters = mpp * 120;
  const pow = Math.pow(10, Math.floor(Math.log10(maxMeters)));
  const meters = [5, 2, 1].map((m) => m * pow).find((m) => m <= maxMeters) ?? pow;
  return { meters, px: meters / mpp };
}

const formatLength = (m: number) => (m >= 1000 ? `${(m / 1000).toLocaleString("ru-RU")} км` : `${m.toLocaleString("ru-RU")} м`);

function Swatch({ item }: { item: LegendItem }) {
  switch (item.swatch) {
    case "tree":
      return <span className="h-[9px] w-[9px] rounded-full" style={{ background: PLANTING_COLORS.tree }} />;
    case "shrub":
      return <span className="h-1.5 w-1.5 rounded-full" style={{ background: PLANTING_COLORS.shrub }} />;
    case "lawn":
      return <span className="h-2 w-3" style={{ background: "rgba(140,200,110,.3)", boxShadow: "inset 0 0 0 1px rgba(140,200,110,.6)" }} />;
    case "violation":
      // Кольцо, а не точка — та же форма, что на карте (globals.css .gp-violation).
      return <span className="h-[9px] w-[9px] rounded-full border-2" style={{ borderColor: VIOLATION_COLOR, boxShadow: `0 0 0 1px ${MAP_BG_COLOR}` }} />;
    default:
      return <span className="w-3.5 border-t-[1.5px] border-dashed" style={{ borderColor: item.color }} />;
  }
}

/** Статус-строка под картой (34px): легенда того, что есть на экране,
 * подсказка про наведение, масштабная линейка и координаты курсора.
 * Масштаб и координаты подписываются на события самой карты и живут в
 * состоянии этого компонента — страница от движения мыши не перерисовывается. */
export function StatusBar({
  map,
  crsVerified,
  legend,
  hint,
}: {
  map: LeafletMap | null;
  crsVerified: boolean;
  legend: LegendItem[];
  /** Подсказка, пока ничего не наведено; null — не показывать. */
  hint: string | null;
}) {
  const [scale, setScale] = useState<{ meters: number; px: number } | null>(null);
  const [cursor, setCursor] = useState<{ x: number; y: number } | null>(null);
  // Ширина самой строки, а не окна: правая колонка (инспектор, Юна) сужает
  // карту, и подсказка с координатами тогда не помещаются в одну строку.
  const footerRef = useRef<HTMLElement>(null);
  const [width, setWidth] = useState(1200);
  useEffect(() => {
    const el = footerRef.current;
    if (!el) return;
    const observer = new ResizeObserver(([entry]) => setWidth(entry.contentRect.width));
    observer.observe(el);
    return () => observer.disconnect();
  }, []);

  useEffect(() => {
    if (!map) {
      setScale(null);
      setCursor(null);
      return;
    }
    const updateScale = () => setScale(niceScale(metersPerPixel(map)));
    updateScale();
    let frame: number | null = null;
    let pending: { lat: number; lng: number } | null = null;
    const onMove = (e: { latlng: { lat: number; lng: number } }) => {
      pending = e.latlng;
      if (frame !== null) return;
      frame = requestAnimationFrame(() => {
        frame = null;
        if (pending) setCursor({ x: pending.lng, y: pending.lat });
      });
    };
    map.on("zoomend moveend resize", updateScale);
    map.on("mousemove", onMove);
    return () => {
      map.off("zoomend moveend resize", updateScale);
      map.off("mousemove", onMove);
      if (frame !== null) cancelAnimationFrame(frame);
    };
  }, [map]);

  // Под проверенной СК координаты карты — градусы, иначе локальные метры
  // чертежа (geo_io.py::display_crs): подписываем так, как они есть.
  const coords = cursor
    ? crsVerified
      ? `${cursor.y.toLocaleString("ru-RU", { minimumFractionDigits: 5, maximumFractionDigits: 5 })}° с. ш. · ${cursor.x.toLocaleString("ru-RU", { minimumFractionDigits: 5, maximumFractionDigits: 5 })}° в. д.`
      : `X ${cursor.x.toLocaleString("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 })} · Y ${cursor.y.toLocaleString("ru-RU", { minimumFractionDigits: 1, maximumFractionDigits: 1 })}`
    : null;

  return (
    <footer ref={footerRef} className="flex min-h-[34px] flex-none flex-wrap items-center gap-x-[18px] gap-y-1 border-t border-gp-line bg-gp-panel px-3.5 py-1 text-xs text-gp-text-2">
      {legend.map((item) => (
        <span key={item.key} className="flex items-center gap-1.5">
          <Swatch item={item} />
          {item.label}
        </span>
      ))}
      <span className="flex-1" />
      {hint && width >= 1000 && <span className="text-gp-text-3">{hint}</span>}
      {scale && (
        <span className="flex items-center gap-2" title="Масштаб">
          <span className="font-mono tabular-nums">{formatLength(scale.meters)}</span>
          <span className="inline-block h-[5px] border-[1.5px] border-t-0 border-gp-text-3" style={{ width: scale.px }} aria-hidden />
        </span>
      )}
      {coords && width >= 860 && <span className="font-mono tabular-nums text-gp-text-3">{coords}</span>}
    </footer>
  );
}
