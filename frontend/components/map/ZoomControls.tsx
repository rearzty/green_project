"use client";

import { useEffect, useState } from "react";
import type { Map as LeafletMap } from "leaflet";

const iconProps = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round", "aria-hidden": true } as const;

/** Зум «+/−» в правом нижнем углу карты — вместо стандартного контрола
 * Leaflet (он стоял слева сверху и съедал клики по соседям, см. историю
 * кнопки панели в app/page.tsx). Карта приходит готовым объектом из
 * MapView (`onMapReady`), своего состояния зума у компонента нет, кроме
 * признака «упёрлись в предел» для disabled. */
export function ZoomControls({ map }: { map: LeafletMap | null }) {
  const [limits, setLimits] = useState({ atMin: false, atMax: false });

  useEffect(() => {
    if (!map) return;
    const update = () => setLimits({ atMin: map.getZoom() <= map.getMinZoom(), atMax: map.getZoom() >= map.getMaxZoom() });
    update();
    map.on("zoomend", update);
    return () => {
      map.off("zoomend", update);
    };
  }, [map]);

  if (!map) return null;
  const button = "inline-flex h-[38px] w-[38px] items-center justify-center text-gp-text-2 transition-colors hover:bg-[#343A36] hover:text-gp-text disabled:opacity-40";
  return (
    <div data-map-overlay className="pointer-events-auto absolute bottom-3 right-3 z-[1050] flex flex-col overflow-hidden rounded-lg border border-gp-border bg-gp-panel shadow-lg">
      <button className={button} onClick={() => map.zoomIn()} disabled={limits.atMax} aria-label="Приблизить" title="Приблизить">
        <svg {...iconProps}>
          <path d="M8 3v10M3 8h10" />
        </svg>
      </button>
      <span className="h-px bg-gp-border" aria-hidden />
      <button className={button} onClick={() => map.zoomOut()} disabled={limits.atMin} aria-label="Отдалить" title="Отдалить">
        <svg {...iconProps}>
          <path d="M3 8h10" />
        </svg>
      </button>
    </div>
  );
}
