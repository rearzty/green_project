"use client";

import { useEffect, useMemo, useState } from "react";

import type { ZoneLayers } from "@/components/MapView";
import { getPlanZones, zoneImageUrl, type PlanOut, type PlanZones, type PlantingType } from "@/lib/api";
import { errorMessage } from "@/lib/toast";

/** Слой «Где нормы разрешают сажать» (Zones.dc.html, п. 3): пока он открыт,
 * на карте — растр допустимой площади и закрытых ограничениями зон, а левая
 * панель — ZonesPanel. Доли считает сервер (zones_service.py) тем же расчётом,
 * что «Почему деревьев мало»; здесь только состояние слоя.
 *
 * Слой относится к конкретной версии плана: сменился план — закрываем, а не
 * показываем зоны от чужого рецепта. */
export function useZonesLayer(projectId: string | undefined, plan: PlanOut | null) {
  const planId = plan?.plan_id;
  const [active, setActive] = useState(false);
  const [plantingType, setPlantingType] = useState<PlantingType>("tree");
  const [zones, setZones] = useState<PlanZones | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [visible, setVisible] = useState<ReadonlySet<string>>(new Set());

  // Типы, про которые у плана есть расчёт площади.
  const types = useMemo<PlantingType[]>(
    () => (plan?.area_budget ?? []).map((b) => b.planting_type).filter((t): t is PlantingType => t === "tree" || t === "shrub" || t === "lawn"),
    [plan?.area_budget]
  );

  useEffect(() => {
    if (!active || !projectId || !planId) return;
    let cancelled = false;
    setLoading(true);
    setError(null);
    setZones(null);
    getPlanZones(projectId, planId, plantingType)
      .then((loaded) => {
        if (cancelled) return;
        setZones(loaded);
        setVisible(new Set(loaded.groups.map((g) => g.key)));
      })
      .catch((e) => {
        if (!cancelled) setError(errorMessage(e));
      })
      .finally(() => {
        if (!cancelled) setLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, [active, projectId, planId, plantingType]);

  // Другая версия или другой проект — слой от прежней больше не про этот план.
  useEffect(() => {
    setActive(false);
  }, [projectId, planId]);

  function open(type?: PlantingType) {
    const wanted = type ?? (types.includes("tree") ? "tree" : types[0]);
    if (!wanted) return;
    setPlantingType(wanted);
    setActive(true);
  }

  function close() {
    setActive(false);
  }

  function toggle(key: string, on: boolean) {
    setVisible((prev) => {
      const next = new Set(prev);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  /** «Показать эту полосу»: на карте только это ограничение. Повторный вызов — снова все. */
  function solo(key: string) {
    setVisible((prev) => (prev.size === 1 && prev.has(key) ? new Set(zones?.groups.map((g) => g.key)) : new Set([key])));
  }

  const layers = useMemo<ZoneLayers | null>(() => {
    if (!active || !zones || !projectId || !planId) return null;
    return {
      bounds: zones.bounds,
      layers: zones.groups.filter((g) => visible.has(g.key)).map((g) => ({ key: g.key, url: zoneImageUrl(projectId, planId, zones.planting_type, g.key) })),
    };
  }, [active, zones, visible, projectId, planId]);

  return { active, plantingType, setPlantingType, zones, loading, error, visible, types, layers, open, close, toggle, solo };
}
