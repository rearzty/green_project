"use client";

import { useEffect, useRef, useState } from "react";
import { Check, Loader2 } from "lucide-react";

import { Button } from "@/components/ui/button";
import { Checkbox } from "@/components/ui/checkbox";
import { getExportSummary, type ExportLayerKey, type ExportOptions, type ExportSummary } from "@/lib/api";
import { pluralRu } from "@/lib/format";
import { useDialog } from "@/lib/hooks/useDialog";
import { errorMessage } from "@/lib/toast";
import { cn } from "@/lib/utils";

const fmt = (n: number) => Math.round(n).toLocaleString("ru-RU");

/** Кружок-метка пункта чек-листа: «!» нарушение, «?» неясность, галочка — всё в порядке. */
function Mark({ tone, children }: { tone: "violation" | "warn" | "ok" | "info"; children: React.ReactNode }) {
  const style = {
    violation: "bg-gp-violation-soft text-gp-violation-text",
    warn: "bg-[#3F3620] text-gp-warn",
    ok: "bg-[#2A3B2C] text-gp-ok",
    info: "bg-gp-line text-gp-text-2",
  }[tone];
  return <span className={cn("flex h-5 w-5 flex-none items-center justify-center rounded-full text-xs font-semibold", style)}>{children}</span>;
}

function CheckRow({
  mark,
  title,
  note,
  action,
  children,
}: {
  mark: React.ReactNode;
  title: string;
  note: string;
  action?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div className="border-t border-gp-line py-3">
      <div className="flex gap-3">
        {mark}
        <div className="min-w-0 flex-1">
          <p className="text-sm font-medium text-gp-text">{title}</p>
          <p className="mt-0.5 text-[13px] text-gp-text-2">{note}</p>
        </div>
        {action}
      </div>
      {children}
    </div>
  );
}

const linkButton = "min-h-7 flex-none text-[13px] font-medium text-gp-accent hover:text-gp-accent-hover";

export interface ExportDialogProps {
  projectId: string;
  planId: string;
  /** «Версия 4» — подзаголовок: какая версия плана уходит в файл. */
  versionLabel: string;
  exporting: boolean;
  onClose: () => void;
  /** «Исправить» — на вкладку «Проверка» к первому нарушению. */
  onFixViolations: () => void;
  /** «Выделить на карте» у несверенной ссылки: посадки, чьё решающее
   * ограничение держится на ней. */
  onShowItems: (ids: string[]) => void;
  /** Скачать. Диалог закрывается сразу — ход экспорта виден на кнопке «Экспорт». */
  onExport: (options: ExportOptions) => Promise<void>;
}

/** Диалог «Перед экспортом» (Export.dc.html, п. 6): что уйдёт в файл и что
 * стоит знать до скачивания. Экспорт с нарушениями не блокируется — они
 * попадают в отдельный слой и в отчёт. Числа считает сервер тем же
 * обоснованием, что пишет файл (`GET .../export-summary`), так что в диалоге и
 * в скачанном DXF они совпадают. */
export function ExportDialog({ projectId, planId, versionLabel, exporting, onClose, onFixViolations, onShowItems, onExport }: ExportDialogProps) {
  const rootRef = useRef<HTMLDivElement>(null);
  useDialog(rootRef, onClose);

  const [summary, setSummary] = useState<ExportSummary | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [layers, setLayers] = useState<ReadonlySet<ExportLayerKey>>(new Set());
  const [includeBase, setIncludeBase] = useState(true);
  const [includeReport, setIncludeReport] = useState(true);
  const [showUnverified, setShowUnverified] = useState(false);

  useEffect(() => {
    let cancelled = false;
    setSummary(null);
    setLoadError(null);
    getExportSummary(projectId, planId)
      .then((next) => {
        if (cancelled) return;
        setSummary(next);
        setLayers(new Set(next.layers.map((layer) => layer.key)));
        setIncludeBase(next.base_available);
      })
      .catch((e) => {
        if (!cancelled) setLoadError(errorMessage(e));
      });
    return () => {
      cancelled = true;
    };
  }, [projectId, planId, attempt]);

  function toggleLayer(key: ExportLayerKey, on: boolean) {
    setLayers((prev) => {
      const next = new Set(prev);
      if (on) next.add(key);
      else next.delete(key);
      return next;
    });
  }

  const violationsInFile = layers.has("violations");
  const violationNote = violationsInFile
    ? includeReport
      ? "Попадут в отдельный слой и в отчёт"
      : "Попадут в отдельный слой; без отчёта списка не будет"
    : includeReport
      ? "Слоя нарушений не будет — они останутся в отчёте"
      : "Ни слоя, ни отчёта — нарушения останутся только в панели «Проверка»";

  return (
    <div className="fixed inset-0 z-[1140] flex items-start justify-center overflow-y-auto bg-[rgba(16,19,17,0.62)] px-4 py-8 sm:items-center">
      <div
        ref={rootRef}
        role="dialog"
        aria-modal="true"
        aria-labelledby="export-title"
        className="w-full max-w-[560px] rounded-xl border border-gp-border bg-gp-panel shadow-[0_24px_60px_rgba(0,0,0,0.5)]"
      >
        <div className="px-6 pb-1.5 pt-[22px]">
          <h2 id="export-title" className="text-[19px] font-semibold text-gp-text">
            Перед экспортом
          </h2>
          <p className="mt-1.5 text-sm text-gp-text-2">{versionLabel} · что уйдёт в файл</p>
        </div>

        {!summary && !loadError && (
          <p className="flex items-center gap-2 px-6 py-8 text-sm text-gp-text-2" role="status">
            <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            Считаю, что попадёт в файл…
          </p>
        )}

        {loadError && (
          <div className="px-6 py-6">
            <p className="text-sm text-gp-violation-text" role="alert">
              Не удалось подготовить сводку: {loadError}
            </p>
            <div className="mt-3 flex gap-2">
              <Button variant="outline" size="sm" onClick={() => setAttempt((n) => n + 1)}>
                Повторить
              </Button>
              {/* Сводка — подсказка, а не условие: скачать можно и без неё. */}
              <Button size="sm" disabled={exporting} onClick={() => void onExport({}).then(onClose)}>
                Скачать как есть
              </Button>
            </div>
          </div>
        )}

        {summary && (
          <>
            <div className="px-6 pb-1 pt-2">
              {summary.violations > 0 ? (
                <CheckRow
                  mark={<Mark tone="violation">!</Mark>}
                  title={`${summary.violations} ${pluralRu(summary.violations, ["нарушение норм", "нарушения норм", "нарушений норм"])}`}
                  note={violationNote}
                  action={
                    <button className={linkButton} onClick={onFixViolations}>
                      Исправить
                    </button>
                  }
                />
              ) : (
                <CheckRow
                  mark={
                    <Mark tone="ok">
                      <Check className="h-3 w-3" strokeWidth={2.6} aria-hidden />
                    </Mark>
                  }
                  title="Нарушений норм нет"
                  note="Все посадки проходят проверку отступов"
                />
              )}

              {summary.unverified_citations.length > 0 && (
                <CheckRow
                  mark={<Mark tone="warn">?</Mark>}
                  title={`${summary.unverified_citations.length} ${pluralRu(summary.unverified_citations.length, ["ссылка на нормы не сверена", "ссылки на нормы не сверены", "ссылок на нормы не сверено"])} с актом`}
                  note="Число взято не из текста акта — сверьте перед сдачей"
                  action={
                    <button className={linkButton} aria-expanded={showUnverified} onClick={() => setShowUnverified((v) => !v)}>
                      {showUnverified ? "Скрыть" : "Посмотреть"}
                    </button>
                  }
                >
                  {showUnverified && (
                    <ul className="mt-2 flex flex-col gap-1.5 pl-8">
                      {summary.unverified_citations.map((c) => (
                        <li key={c.citation} className="flex items-start justify-between gap-3 text-[13px]">
                          <span className="min-w-0 text-gp-text-2">
                            {c.citation} <span className="font-mono tabular-nums text-gp-text-3">· {fmt(c.items)}</span>
                          </span>
                          <button className={linkButton} onClick={() => onShowItems(c.item_ids)}>
                            На карте
                          </button>
                        </li>
                      ))}
                    </ul>
                  )}
                </CheckRow>
              )}

              {includeBase && summary.base_available ? (
                <CheckRow
                  mark={
                    <Mark tone="ok">
                      <Check className="h-3 w-3" strokeWidth={2.6} aria-hidden />
                    </Mark>
                  }
                  title="Координаты как в исходном чертеже"
                  note="План ляжет на подоснову без сдвига"
                />
              ) : (
                <CheckRow
                  mark={<Mark tone="info">i</Mark>}
                  title="Отдельный чертёж без подосновы"
                  note={summary.base_available ? "Исходный чертёж в файл не войдёт — включите его ниже" : "Исходного чертежа у проекта нет (загружен не DXF/DWG)"}
                />
              )}
            </div>

            <div className="mx-6 mt-1 border-t border-gp-line pt-3.5">
              <p className="mb-1 text-[13px] text-gp-text-2">Слои в файле</p>
              {summary.layers.map((layer) => (
                <Checkbox key={layer.key} checked={layers.has(layer.key)} onChange={(on) => toggleLayer(layer.key, on)}>
                  <span className="flex-1 font-mono">{layer.name}</span>
                  <span className="font-mono tabular-nums text-gp-text-3">{layer.area_m2 !== null ? `${fmt(layer.area_m2)} м²` : fmt(layer.count)}</span>
                </Checkbox>
              ))}
              <Checkbox checked={includeBase} onChange={setIncludeBase} disabled={!summary.base_available}>
                <span className="flex-1 text-gp-text-2">Исходный чертёж целиком</span>
                {summary.base_layer_count !== null && <span className="text-xs text-gp-text-3">{summary.base_layer_count} слоёв</span>}
              </Checkbox>
            </div>
          </>
        )}

        <div className="mt-3 flex flex-wrap items-center gap-2 border-t border-gp-line px-6 pb-[22px] pt-[18px]">
          <Checkbox checked={includeReport} onChange={setIncludeReport} className="min-w-[200px] flex-1" disabled={!summary}>
            Приложить отчёт обоснования
          </Checkbox>
          <Button variant="outline" onClick={onClose}>
            Отмена
          </Button>
          <Button
            disabled={!summary || layers.size === 0 || exporting}
            title={summary && layers.size === 0 ? "Выберите хотя бы один слой" : undefined}
            onClick={() => {
              if (!summary) return;
              void onExport({ layers: [...layers], includeBase, includeReport });
              onClose();
            }}
          >
            {exporting && <Loader2 className="h-4 w-4 animate-spin" aria-hidden />}
            Скачать DXF
          </Button>
        </div>
      </div>
    </div>
  );
}
