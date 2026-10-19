"use client";

import { useEffect, useRef, useState } from "react";

import { LAYERS_ICON } from "@/components/map/icons";
import { SCENE_THEME_LABELS, SEASON_LABELS, type LayerLegendEntry, type SceneTheme, type Season } from "@/lib/mapStyle";
import { cn } from "@/lib/utils";

export interface ViewControlsProps {
  view3d: boolean;
  /** 3D доступен только при построенном плане. */
  canShow3d: boolean;
  onSetView3d: (on: boolean) => void;
  layerLegend: LayerLegendEntry[];
  hiddenLayerTypes: ReadonlySet<string>;
  onToggleLayerType: (key: string) => void;
  showPlan: boolean;
  onToggleShowPlan: (show: boolean) => void;
  hasPlan: boolean;
  season: Season;
  onSeasonChange: (season: Season) => void;
  sceneTheme: SceneTheme;
  onSceneThemeChange: (theme: SceneTheme) => void;
}

/** Правый верхний угол карты: переключатель 2D/3D и «Слои» с поповером
 * (в 3D сверху — сезон и оформление сцены, ниже слои чертежа и «Сгенерированный план»). */
export function ViewControls({
  view3d,
  canShow3d,
  onSetView3d,
  layerLegend,
  hiddenLayerTypes,
  onToggleLayerType,
  showPlan,
  onToggleShowPlan,
  hasPlan,
  season,
  onSeasonChange,
  sceneTheme,
  onSceneThemeChange,
}: ViewControlsProps) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setOpen(false);
    }
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [open]);

  return (
    <div ref={rootRef} data-map-overlay className="pointer-events-auto absolute right-3 top-3 z-[1050] flex items-start gap-2">
      <div role="group" aria-label="Вид" className="flex rounded-lg border border-gp-border bg-gp-panel p-[3px] shadow-lg">
        {([false, true] as const).map((is3d) => {
          const active = view3d === is3d;
          const disabled = is3d && !view3d && !canShow3d;
          return (
            <button
              key={String(is3d)}
              onClick={() => onSetView3d(is3d)}
              disabled={disabled}
              aria-pressed={active}
              title={is3d ? "Объёмный вид — только просмотр, без выделения и правки" : "Плоская карта"}
              className={cn(
                "h-[30px] rounded-md px-3 text-[13px] font-medium transition-colors disabled:opacity-40",
                active ? "bg-gp-border text-gp-text" : "text-gp-text-2 hover:text-gp-text"
              )}
            >
              {is3d ? "3D" : "2D"}
            </button>
          );
        })}
      </div>

      <div className="relative">
        <button
          onClick={() => setOpen((v) => !v)}
          aria-expanded={open}
          aria-haspopup="dialog"
          className={cn(
            "flex h-[38px] items-center gap-2 rounded-lg border border-gp-border bg-gp-panel px-3 text-[13px] font-medium text-gp-text shadow-lg hover:bg-gp-hover",
            open && "bg-gp-hover"
          )}
        >
          {LAYERS_ICON}
          Слои
        </button>
        {open && (
          <div
            role="dialog"
            aria-label="Слои"
            className="gp-menu absolute right-0 top-[46px] max-h-[min(70vh,34rem)] w-72 overflow-y-auto rounded-lg border border-gp-border bg-gp-panel py-1.5 shadow-xl"
          >
            {view3d && (
              <>
                <p className="px-3 pb-1.5 pt-1 text-[13px] font-medium text-gp-text-2">Сезон</p>
                <div className="grid grid-cols-4 gap-1 px-3 pb-1.5">
                  {(Object.keys(SEASON_LABELS) as Season[]).map((s) => (
                    <button
                      key={s}
                      onClick={() => onSeasonChange(s)}
                      aria-pressed={season === s}
                      className={cn(
                        "h-8 rounded-md border text-xs font-medium",
                        season === s ? "border-gp-accent-soft-border bg-gp-accent-soft text-gp-text" : "border-gp-border text-gp-text-2 hover:bg-gp-hover"
                      )}
                    >
                      {SEASON_LABELS[s]}
                    </button>
                  ))}
                </div>
                <p className="px-3 pb-1.5 pt-1 text-[13px] font-medium text-gp-text-2">Сцена</p>
                <div className="grid grid-cols-2 gap-1 px-3 pb-1.5">
                  {(Object.keys(SCENE_THEME_LABELS) as SceneTheme[]).map((theme) => (
                    <button
                      key={theme}
                      onClick={() => onSceneThemeChange(theme)}
                      aria-pressed={sceneTheme === theme}
                      className={cn(
                        "h-8 rounded-md border text-xs font-medium",
                        sceneTheme === theme ? "border-gp-accent-soft-border bg-gp-accent-soft text-gp-text" : "border-gp-border text-gp-text-2 hover:bg-gp-hover"
                      )}
                    >
                      {SCENE_THEME_LABELS[theme]}
                    </button>
                  ))}
                </div>
                <div className="my-1 border-t border-gp-line" />
              </>
            )}
            {hasPlan && (
              <label className="flex min-h-10 cursor-pointer items-center gap-3 px-3 text-sm text-gp-text hover:bg-gp-hover">
                <input type="checkbox" checked={showPlan} onChange={(e) => onToggleShowPlan(e.target.checked)} className="h-4 w-4 accent-gp-accent" />
                <span className="flex-1">Сгенерированный план</span>
              </label>
            )}
            {layerLegend.length > 0 && (
              <>
                {hasPlan && <div className="my-1 border-t border-gp-line" />}
                <p className="px-3 pb-1 pt-1 text-[13px] font-medium text-gp-text-2">Слои чертежа</p>
                <ul>
                  {layerLegend.map((entry) => (
                    <li key={entry.key}>
                      <label className="flex min-h-10 cursor-pointer items-center gap-3 px-3 text-sm text-gp-text hover:bg-gp-hover">
                        <input
                          type="checkbox"
                          checked={!hiddenLayerTypes.has(entry.key)}
                          onChange={() => onToggleLayerType(entry.key)}
                          className="h-4 w-4 flex-none accent-gp-accent"
                        />
                        <span className="h-2.5 w-2.5 flex-none rounded-sm border border-white/10" style={{ backgroundColor: entry.color }} aria-hidden />
                        <span className="min-w-0 flex-1 truncate">{entry.label}</span>
                        <span className="font-mono text-xs tabular-nums text-gp-text-3">{entry.count.toLocaleString("ru-RU")}</span>
                      </label>
                    </li>
                  ))}
                </ul>
              </>
            )}
            {!hasPlan && layerLegend.length === 0 && <p className="px-3 py-2 text-[13px] text-gp-text-3">Слоёв пока нет — загрузите чертёж.</p>}
          </div>
        )}
      </div>
    </div>
  );
}
