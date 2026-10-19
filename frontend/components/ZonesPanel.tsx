"use client";

import { ChevronLeft, Loader2 } from "lucide-react";

import { Checkbox } from "@/components/ui/checkbox";
import type { PlanZones, PlantingType } from "@/lib/api";
import { cn } from "@/lib/utils";

const TYPE_NOUNS: Record<PlantingType, { title: string; chip: string }> = {
  tree: { title: "деревья", chip: "Деревья" },
  shrub: { title: "кустарник", chip: "Кустарник" },
  lawn: { title: "газон", chip: "Газон" },
};

const pct = (share: number) => `${(share * 100).toLocaleString("ru-RU", { maximumFractionDigits: share < 0.1 ? 1 : 0 })}%`;

export interface ZonesPanelProps {
  zones: PlanZones | null;
  loading: boolean;
  error: string | null;
  plantingType: PlantingType;
  /** Типы, для которых в плане есть объяснение площади. */
  types: PlantingType[];
  onTypeChange: (type: PlantingType) => void;
  /** Какие слои сейчас на карте. */
  visible: ReadonlySet<string>;
  onToggle: (key: string, on: boolean) => void;
  /** «Показать эту полосу»: на карте только это ограничение. */
  onSolo: (key: string) => void;
  onBack: () => void;
}

/** «Где нормы разрешают сажать» (Zones.dc.html, п. 3): левая панель вместо
 * вкладки «Проверка», пока открыт слой ограничений. Сверху — полоса долей, ниже —
 * переключатели слоёв с процентами и в конце — что даст больше всего посадок.
 * Доли берутся из того же расчёта, что и «Почему деревьев мало» (area_budget). */
export function ZonesPanel({ zones, loading, error, plantingType, types, onTypeChange, visible, onToggle, onSolo, onBack }: ZonesPanelProps) {
  const noun = TYPE_NOUNS[plantingType];
  const blockers = zones?.groups.filter((g) => g.kind === "blocker") ?? [];
  const allowed = zones?.groups.find((g) => g.kind === "allowed");

  // Ограничения перекрываются (одна точка закрыта и теплосетью, и кабелем),
  // поэтому сумма их долей бывает больше «закрытой» площади, а полоса — ровно
  // 100%. Ширины блокеров сжимаются пропорционально до закрытой части; точные
  // проценты — в строках ниже, они от этого не меняются.
  const closed = allowed ? 1 - allowed.share : 1;
  const sum = blockers.reduce((acc, g) => acc + g.share, 0);
  const squeeze = sum > closed && sum > 0 ? closed / sum : 1;

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <div className="px-5 pb-4 pt-5">
          <button onClick={onBack} className="flex min-h-8 items-center gap-1.5 text-[13px] text-gp-text-2 hover:text-gp-text">
            <ChevronLeft className="h-4 w-4" aria-hidden />К плану
          </button>
          <h1 className="mt-2.5 text-[19px] font-semibold leading-[1.3] text-gp-text">Где нормы разрешают {noun.title}</h1>
          <p className="mt-2 text-sm leading-5 text-gp-text-2">Зелёным — где можно сажать. Остальное закрыто отступами, цветом показано чем.</p>
          {types.length > 1 && (
            <div role="group" aria-label="Тип посадки" className="mt-3 inline-flex rounded-lg border border-gp-border p-0.5 text-[13px]">
              {types.map((type) => (
                <button
                  key={type}
                  aria-pressed={type === plantingType}
                  onClick={() => onTypeChange(type)}
                  className={cn("h-8 rounded-md px-3 font-medium", type === plantingType ? "bg-gp-border text-gp-text" : "text-gp-text-2 hover:text-gp-text")}
                >
                  {TYPE_NOUNS[type].chip}
                </button>
              ))}
            </div>
          )}
        </div>

        {loading && (
          <p className="flex items-center gap-2 px-5 py-4 text-sm text-gp-text-2" role="status">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            Считаю, где что закрыто…
          </p>
        )}
        {error && (
          <p className="px-5 py-4 text-sm text-gp-violation-text" role="alert">
            {error}
          </p>
        )}

        {zones && allowed && (
          <>
            <div className="px-5 pb-[18px]">
              <div
                className="flex h-2.5 overflow-hidden rounded-[5px]"
                role="img"
                aria-label={`Можно сажать: ${pct(allowed.share)} участка`}
                title="Ограничения перекрываются, поэтому ширины условны; точные доли — в списке"
              >
                <span style={{ width: `${allowed.share * 100}%`, background: allowed.color }} />
                {blockers.map((g) => (
                  <span key={g.key} style={{ width: `${g.share * squeeze * 100}%`, background: g.color }} />
                ))}
                <span className="flex-1 bg-gp-border" />
              </div>
            </div>

            <div role="group" aria-label="Слои ограничений" className="border-t border-gp-line py-1.5">
              {zones.groups.map((g) => (
                <div key={g.key} className="px-5 hover:bg-gp-hover">
                  <Checkbox checked={visible.has(g.key)} onChange={(on) => onToggle(g.key, on)} className="min-h-11 text-sm">
                    <span className="h-3 w-3 flex-none rounded-[3px]" style={{ background: g.color }} aria-hidden />
                    <span className={cn("flex-1", g.kind === "allowed" ? "font-medium" : visible.has(g.key) ? "" : "text-gp-text-2")}>{g.label}</span>
                    <span className={cn("font-mono text-[13px] tabular-nums", g.kind === "allowed" ? "text-gp-text" : "text-gp-text-2")}>{pct(g.share)}</span>
                  </Checkbox>
                </div>
              ))}
            </div>

            {zones.advice && (
              <div className="mx-5 mb-5 mt-2 border-t border-gp-line pt-3.5">
                <p className="text-[13px] font-medium text-gp-text">Что даст больше всего {noun.title === "деревья" ? "деревьев" : noun.title === "кустарник" ? "кустов" : "газона"}</p>
                <p className="mt-1.5 text-sm leading-5 text-gp-text-2">
                  Больше всего места, которое закрыто только им, занимает «{zones.advice.label}» —{" "}
                  <span className="font-mono tabular-nums text-gp-text">{Math.round(zones.advice.exclusive_m2).toLocaleString("ru-RU")} м²</span>. Если сократить эту
                  зону или вынести сеть, при интервале{" "}
                  <span className="font-mono tabular-nums text-gp-text">{zones.interval_m.toLocaleString("ru-RU")} м</span> там встало бы до{" "}
                  <span className="font-mono tabular-nums text-gp-text">{zones.advice.extra_items.toLocaleString("ru-RU")}</span> {plantingType === "tree" ? "деревьев" : "посадок"}.
                  Это оценка по площади, а не проект: настоящая расстановка идёт рядами и теряет на отступах.
                </p>
                <button className="mt-2.5 min-h-9 text-[13px] font-medium text-gp-accent hover:text-gp-accent-hover" onClick={() => onSolo(zones.advice!.key)}>
                  Показать эту полосу
                </button>
              </div>
            )}
          </>
        )}
      </div>
    </div>
  );
}
