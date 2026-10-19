"use client";

import { ArrowRight, ChevronDown, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import type { PlanSummary } from "@/lib/api";
import { cn } from "@/lib/utils";

export interface VersionStats {
  trees: number;
  /** null — проверка нормативов ещё не закончилась. */
  violations: number | null;
  /** Доля участка под кронами, 0..1; null — площади участка нет. */
  canopyShare: number | null;
}

const MODE_LABELS = { heuristic: "формула", ml: "ML-модель" } as const;

/** Подпись версии: «Версия 4 · формула». Номер — по времени создания среди версий проекта. */
export function versionName(plans: PlanSummary[], planId: string): string {
  const ordered = [...plans].sort((a, b) => a.created_at.localeCompare(b.created_at));
  const index = ordered.findIndex((p) => p.plan_id === planId);
  const plan = ordered[index];
  return index < 0 || !plan ? "Версия" : `Версия ${index + 1} · ${MODE_LABELS[plan.scoring_mode]}`;
}

export function versionNumber(plans: PlanSummary[], planId: string): number {
  return [...plans].sort((a, b) => a.created_at.localeCompare(b.created_at)).findIndex((p) => p.plan_id === planId) + 1;
}

function VersionSelect({ label, value, plans, onChange, disabled }: { label: string; value: string; plans: PlanSummary[]; onChange: (planId: string) => void; disabled?: boolean }) {
  const ordered = [...plans].sort((a, b) => a.created_at.localeCompare(b.created_at));
  return (
    <label className="relative inline-flex">
      <span className="sr-only">{label}</span>
      <select
        value={value}
        disabled={disabled}
        onChange={(e) => onChange(e.target.value)}
        className="h-[34px] cursor-pointer appearance-none rounded-lg border border-gp-border bg-transparent pl-2.5 pr-8 text-[13px] font-medium text-gp-text hover:bg-gp-hover disabled:opacity-50"
      >
        {ordered.map((p) => (
          <option key={p.plan_id} value={p.plan_id} className="bg-gp-panel">
            {versionName(plans, p.plan_id)}
          </option>
        ))}
      </select>
      <ChevronDown className="pointer-events-none absolute right-2 top-1/2 h-4 w-4 -translate-y-1/2 text-gp-text-3" aria-hidden />
    </label>
  );
}

/** Одна цифра сравнения: «61 → 68 +7». `better` — в какую сторону хорошо:
 * больше (деревья, покрытие) или меньше (нарушения). */
function Stat({ title, from, to, format, better }: { title: string; from: number | null; to: number | null; format: (n: number) => string; better: "up" | "down" }) {
  const delta = from !== null && to !== null ? to - from : null;
  const good = delta !== null && delta !== 0 && (better === "up" ? delta > 0 : delta < 0);
  const bad = delta !== null && delta !== 0 && !good;
  const sign = delta !== null && delta > 0 ? "+" : "−";
  return (
    <div className="flex flex-col gap-0.5 pr-5 md:border-l md:border-gp-line md:px-[18px]">
      <span className="text-xs text-gp-text-2">{title}</span>
      <span className="font-mono text-[15px] tabular-nums text-gp-text">
        {from === null ? "…" : format(from)} → {to === null ? "…" : format(to)}
        {delta !== null && delta !== 0 && (
          <span className={cn("ml-1.5", good && "text-gp-ok", bad && "text-gp-violation-text")}>
            {sign}
            {format(Math.abs(delta))}
          </span>
        )}
      </span>
    </div>
  );
}

export interface CompareBarProps {
  plans: PlanSummary[];
  baseId: string;
  currentId: string;
  /** Данные прежней версии ещё грузятся. */
  loading: boolean;
  base: VersionStats | null;
  current: VersionStats | null;
  busy: boolean;
  onBaseChange: (planId: string) => void;
  onCurrentChange: (planId: string) => void;
  /** «Вернуться к N»: открыть прежнюю версию и выйти из сравнения. */
  onRevert: () => void;
  /** «Оставить версию M»: выйти из сравнения, оставив текущую. */
  onKeep: () => void;
}

/** Полоса сравнения версий под шапкой (Compare.dc.html, п. 5): две версии,
 * дельты деревьев, нарушений и покрытия кронами и два решения. Выход из режима
 * ничего не удаляет — вторая версия остаётся в списке «Версии плана». */
export function CompareBar({ plans, baseId, currentId, loading, base, current, busy, onBaseChange, onCurrentChange, onRevert, onKeep }: CompareBarProps) {
  const baseNumber = versionNumber(plans, baseId);
  const currentNumber = versionNumber(plans, currentId);
  return (
    <div className="flex flex-none flex-wrap items-center gap-y-2.5 border-b border-gp-line bg-gp-panel px-4 py-2.5 md:gap-y-3 md:py-3" role="region" aria-label="Сравнение версий плана">
      <div className="flex items-center gap-2 pr-[18px]">
        <span className="text-[13px] text-gp-text-2">Сравнить</span>
        <VersionSelect label="Прежняя версия" value={baseId} plans={plans} onChange={onBaseChange} disabled={busy} />
        <ArrowRight className="h-4 w-4 flex-none text-gp-text-3" aria-hidden />
        <VersionSelect label="Новая версия" value={currentId} plans={plans} onChange={onCurrentChange} disabled={busy} />
      </div>
      {loading ? (
        <span className="flex items-center gap-2 text-[13px] text-gp-text-2 md:border-l md:border-gp-line md:px-[18px]" role="status">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          Сравниваю…
        </span>
      ) : (
        base &&
        current && (
          <>
            <Stat title="Деревья" from={base.trees} to={current.trees} format={(n) => n.toLocaleString("ru-RU")} better="up" />
            <Stat title="Нарушения" from={base.violations} to={current.violations} format={(n) => n.toLocaleString("ru-RU")} better="down" />
            {base.canopyShare !== null && current.canopyShare !== null && (
              <Stat title="Покрытие кронами" from={base.canopyShare} to={current.canopyShare} format={(n) => `${Math.round(n * 100)}%`} better="up" />
            )}
          </>
        )
      )}
      <span className="flex-1" />
      <div className="flex gap-2">
        <Button variant="outline" size="sm" disabled={busy || baseNumber <= 0} onClick={onRevert}>
          Вернуться к {baseNumber}
        </Button>
        <Button size="sm" disabled={busy} onClick={onKeep}>
          Оставить версию {currentNumber}
        </Button>
      </div>
    </div>
  );
}
