"use client";

import { useEffect, useRef, useState } from "react";
import { Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Kbd } from "@/components/ui/kbd";
import { getItemFixes, type FixKind, type FixOption } from "@/lib/api";
import { formatMeters, OBJECT_GENITIVE_RU } from "@/lib/format";
import { errorMessage } from "@/lib/toast";
import { cn } from "@/lib/utils";

/** Что выбрано в «Как исправить» — карта показывает это как предпросмотр
 * (page.tsx → MapView::FixPreviewMark), пока строка раскрыта. */
export interface FixPreview {
  itemId: string;
  option: FixOption;
}

const SETBACK_GENITIVE = OBJECT_GENITIVE_RU;

/** «Отодвинуть от теплосети» — название нарушенного ограничения в родительном
 * падеже; для неизвестного типа — просто «Отодвинуть». */
function moveTitle(violatedType: string | undefined): string {
  const target = violatedType ? SETBACK_GENITIVE[violatedType] : undefined;
  return target ? `Отодвинуть от ${target}` : "Отодвинуть";
}

function describe(option: FixOption): { title: (violatedType: string | undefined) => string; hint: string } {
  if (option.kind === "move") {
    const shift = option.shift_m != null ? `На ${formatMeters(option.shift_m)}` : "Сдвинуть";
    const row = option.keeps_row === true ? ", остаётся в ряду" : option.keeps_row === false ? ", выйдет из ряда" : "";
    return { title: moveTitle, hint: `${shift}${row}` };
  }
  if (option.kind === "retype") {
    return {
      title: () => "Заменить на куст",
      hint: option.required_m != null ? `Кусту нужен ${formatMeters(option.required_m)} — подходит` : "Куст в этом месте проходит нормы",
    };
  }
  return {
    title: () => "Удалить",
    hint: option.gap_m != null ? `В ряду появится пропуск ${formatMeters(option.gap_m)}` : "Посадка исчезнет из плана",
  };
}

/** Раскрытая строка нарушения: «Как исправить» — радиогруппа из вариантов
 * сервера (Fix.dc.html, п. 1), выбранный сразу показывается на карте. Сам
 * ничего не правит: «Применить» отдаёт выбор странице, а та идёт теми же
 * обработчиками, что кнопки панели выделения (usePlanEdits) — поэтому отмена
 * здесь та же Ctrl+Z, а не отдельная. */
export function FixPanel({
  projectId,
  planId,
  planRevision,
  itemId,
  citation,
  violatedType,
  disabled,
  onApply,
  onSkip,
  onPreview,
  onChoiceChange,
}: {
  projectId: string;
  planId: string;
  /** Меняется после любой правки: соседи и расстояния могли сдвинуться. */
  planRevision: number;
  itemId: string;
  /** «СП 82.13330.2016, табл. 4» — на чём держится нарушенная норма. */
  citation?: string;
  violatedType?: string;
  /** Идёт сохранение предыдущей правки — следующую не начинаем. */
  disabled: boolean;
  onApply: (option: FixOption) => void;
  onSkip: () => void;
  onPreview: (preview: FixPreview | null) => void;
  /** Какой вид исправления выбран — от него зависит «Исправить все так же». */
  onChoiceChange: (kind: FixKind | null) => void;
}) {
  const [options, setOptions] = useState<FixOption[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [choice, setChoice] = useState<FixKind | null>(null);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    let cancelled = false;
    setOptions(null);
    setError(null);
    getItemFixes(projectId, planId, itemId)
      .then((found) => {
        if (cancelled) return;
        setOptions(found);
        // Выбор прежней посадки сохраняется, если такой вариант есть и теперь; иначе — лучший.
        setChoice((prev) => (prev && found.some((o) => o.kind === prev) ? prev : (found.find((o) => o.recommended) ?? found[0])?.kind ?? null));
      })
      .catch((e) => {
        if (!cancelled) setError(errorMessage(e));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, planId, itemId, planRevision]);

  const selected = options?.find((o) => o.kind === choice) ?? null;

  useEffect(() => {
    onPreview(selected ? { itemId, option: selected } : null);
    onChoiceChange(selected ? selected.kind : null);
    return () => {
      onPreview(null);
      onChoiceChange(null);
    };
    // колбэки страницы нестабильны по ссылке; предпросмотр зависит только от выбора
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [selected, itemId]);

  // Enter — «Применить». Не там, где у Enter своё значение: на кнопке, в поле
  // ввода, внутри диалога или меню; на самой радиогруппе — наш.
  const applyRef = useRef<() => void>(() => {});
  applyRef.current = () => {
    if (selected && !disabled) onApply(selected);
  };
  useEffect(() => {
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "Enter" || e.repeat || e.shiftKey || e.ctrlKey || e.metaKey || e.altKey) return;
      if (document.querySelector("[role=dialog]")) return;
      const target = e.target as HTMLElement | null;
      const own = Boolean(target?.closest("[data-fix-panel]"));
      if (target && !own && target.closest("button, a, input, textarea, select, [contenteditable=true], [role=menu]")) return;
      if (own && target?.closest("button")) return;
      e.preventDefault();
      applyRef.current();
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

  return (
    <div ref={rootRef} data-fix-panel className="bg-gp-violation-row px-5 pb-4 pt-1">
      {citation && <p className="mb-3 font-mono text-xs leading-5 text-gp-text-3">{citation}</p>}
      <p className="mb-2 text-[13px] font-medium text-gp-text-2">Как исправить</p>
      {options === null && !error && (
        <p className="flex items-center gap-2 py-2 text-sm text-gp-text-2" role="status">
          <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
          Ищу, как исправить…
        </p>
      )}
      {error && (
        <p className="py-2 text-sm text-gp-violation-text" role="alert">
          {error}
        </p>
      )}
      {options && (
        <>
          <div role="radiogroup" aria-label="Как исправить" className="flex flex-col gap-1.5">
            {options.map((option) => {
              const text = describe(option);
              return (
                <label
                  key={option.kind}
                  className={cn(
                    "flex min-h-[3.25rem] cursor-pointer items-start gap-3 rounded-lg border px-3 py-2.5 transition-colors",
                    "has-[:focus-visible]:outline has-[:focus-visible]:outline-2 has-[:focus-visible]:outline-gp-accent",
                    option.kind === choice ? "border-gp-accent-soft-border bg-gp-accent-soft" : "border-gp-border hover:bg-gp-hover"
                  )}
                >
                  <input
                    type="radio"
                    name={`fix-${itemId}`}
                    value={option.kind}
                    checked={option.kind === choice}
                    onChange={() => setChoice(option.kind)}
                    className="peer sr-only"
                  />
                  <span
                    aria-hidden
                    className={cn(
                      "mt-0.5 flex h-4 w-4 flex-none items-center justify-center rounded-full border",
                      option.kind === choice ? "border-gp-accent" : "border-gp-text-3"
                    )}
                  >
                    {option.kind === choice && <span className="h-2 w-2 rounded-full bg-gp-accent" />}
                  </span>
                  <span className="min-w-0 flex-1">
                    <span className="flex items-center gap-2 text-sm font-medium text-gp-text">
                      {text.title(violatedType)}
                      {option.recommended && (
                        <span className="rounded bg-gp-accent-soft px-1.5 py-0.5 text-[11px] font-medium text-gp-accent-text">лучший</span>
                      )}
                    </span>
                    <span className="mt-0.5 block text-[13px] leading-5 text-gp-text-2">{text.hint}</span>
                  </span>
                </label>
              );
            })}
          </div>
          <div className="mt-3 flex items-center gap-2">
            <Button size="sm" disabled={disabled || !selected} onClick={() => selected && onApply(selected)}>
              Применить
              <Kbd className="ml-1.5 text-gp-on-accent/70">Enter</Kbd>
            </Button>
            <Button size="sm" variant="outline" onClick={onSkip}>
              Пропустить
            </Button>
          </div>
        </>
      )}
    </div>
  );
}
