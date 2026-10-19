"use client";

import { useEffect, useRef, useState } from "react";
import { X } from "lucide-react";

import { Button } from "@/components/ui/button";
import { countLabel, PLANTING_TYPE_FORMS } from "@/lib/format";
import type { SelectionSummary } from "@/lib/hooks/useSelection";
import type { PlantingType } from "@/lib/api";

const ACTION = "h-[34px] rounded-md px-3 text-[13px] font-medium text-gp-text hover:bg-gp-hover-strong disabled:opacity-40";

/** Панель выделения внизу по центру карты (SmartSelect.dc.html): имя группы
 * («Ряд», «Той же породы · Липа мелколистная»), состав и действия над ней.
 * Для одной посадки её нет — там правая колонка с обоснованием; для группы
 * обоснование не показывается, а действия нужны под рукой. Лежит поверх
 * карты — data-map-overlay. */
export function SelectionBar({
  summary,
  name,
  canEdit,
  onRetype,
  onDelete,
  onClear,
  onChangeInterval,
}: {
  summary: SelectionSummary;
  /** Название выделения, если оно получено из «Выделить похожие». */
  name?: string;
  canEdit: boolean;
  onRetype: (type: "tree" | "shrub") => void;
  onDelete: () => void;
  onClear: () => void;
  /** Есть только когда выделен один ряд — перерасставить его с другим шагом (м). */
  onChangeInterval?: (pitchM: number) => void;
}) {
  const [intervalOpen, setIntervalOpen] = useState(false);
  const [pitch, setPitch] = useState("");
  const inputRef = useRef<HTMLInputElement>(null);
  // Выделение сменилось — вопрос про шаг к нему больше не относится.
  useEffect(() => setIntervalOpen(false), [summary]);
  useEffect(() => {
    if (intervalOpen) inputRef.current?.focus();
  }, [intervalOpen]);

  const pitchValue = Number(pitch.replace(",", "."));
  const pitchValid = pitch.trim() !== "" && Number.isFinite(pitchValue) && pitchValue >= 0.3 && pitchValue <= 30;
  function applyInterval() {
    if (!pitchValid || !onChangeInterval) return;
    onChangeInterval(pitchValue);
    setIntervalOpen(false);
  }

  const composition = (Object.keys(summary.counts) as PlantingType[])
    .filter((type) => summary.counts[type] > 0)
    .map((type) => countLabel(summary.counts[type], PLANTING_TYPE_FORMS[type]))
    .join(", ");
  const hasTrees = summary.counts.tree > 0;
  const hasShrubs = summary.counts.shrub > 0;

  return (
    <div
      data-map-overlay
      role="toolbar"
      aria-label="Действия с выделением"
      className="pointer-events-auto absolute bottom-[18px] left-1/2 z-[1050] flex max-w-[calc(100%-7rem)] -translate-x-1/2 flex-wrap items-center gap-1 rounded-[10px] border border-gp-selection-border bg-gp-panel py-1.5 pl-4 pr-1.5 shadow-[0_12px_30px_rgba(0,0,0,0.4)]"
    >
      <span className="mr-2.5 text-sm">
        {name && <span className="font-medium text-gp-selection-text">{name}</span>}
        {name && <span className="text-gp-text-2"> · </span>}
        <span className={name ? "text-gp-text-2" : "font-medium text-gp-selection-text"}>{composition}</span>
      </span>
      {onChangeInterval && (
        <span className="relative">
          <button className={ACTION} disabled={!canEdit} aria-expanded={intervalOpen} onClick={() => setIntervalOpen((v) => !v)}>
            Изменить интервал
          </button>
          {intervalOpen && (
            <div
              role="dialog"
              aria-label="Новый интервал ряда"
              onKeyDown={(e) => {
                if (e.key === "Escape") {
                  e.stopPropagation();
                  setIntervalOpen(false);
                }
              }}
              className="absolute bottom-[calc(100%+10px)] left-0 z-10 w-[19rem] rounded-lg border border-gp-border bg-gp-panel p-3.5 shadow-xl"
            >
              <label className="flex flex-col gap-1.5 text-[13px] text-gp-text-2">
                Шаг между посадками, м
                <input
                  ref={inputRef}
                  value={pitch}
                  onChange={(e) => setPitch(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") {
                      e.preventDefault();
                      applyInterval();
                    }
                  }}
                  inputMode="decimal"
                  placeholder="например, 8"
                  className="h-10 rounded-lg border border-gp-border bg-gp-field px-3 font-mono text-sm text-gp-text placeholder:font-sans placeholder:text-gp-text-4 focus:border-gp-accent-soft-border focus:outline-none"
                />
              </label>
              <p className="mt-2 text-xs leading-5 text-gp-text-3">
                Ряд раскладывается заново по той же линии. Деревьям — не меньше интервала по классу кроны (3–8 м, МГСН 1.02-02 п. 4.2.9.2). Меньший шаг сервис не примет.
              </p>
              <div className="mt-3 flex justify-end gap-2">
                <Button variant="outline" size="sm" onClick={() => setIntervalOpen(false)}>
                  Отмена
                </Button>
                <Button size="sm" disabled={!pitchValid} onClick={applyInterval}>
                  Применить
                </Button>
              </div>
            </div>
          )}
        </span>
      )}
      {hasTrees && (
        <button className={ACTION} disabled={!canEdit} onClick={() => onRetype("shrub")}>
          Сделать кустами
        </button>
      )}
      {hasShrubs && (
        <button className={ACTION} disabled={!canEdit} onClick={() => onRetype("tree")}>
          Сделать деревьями
        </button>
      )}
      <button className={`${ACTION} text-gp-violation-danger`} disabled={!canEdit} onClick={onDelete}>
        Удалить
      </button>
      <span className="mx-1 h-5 w-px bg-gp-border" aria-hidden />
      <button
        className="flex h-[34px] w-[34px] items-center justify-center rounded-md text-gp-text-2 hover:bg-gp-hover-strong"
        onClick={onClear}
        aria-label="Снять выделение (Esc)"
        title="Снять выделение · Esc"
      >
        <X className="h-4 w-4" aria-hidden />
      </button>
    </div>
  );
}
