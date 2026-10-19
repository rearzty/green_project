"use client";

import { useEffect, useRef, useState } from "react";
import { ChevronDown, RotateCcw } from "lucide-react";

import { Button } from "@/components/ui/button";
import { cn } from "@/lib/utils";

/** Этапы работы: загрузить чертёж → построить план → проверить и выгрузить.
 * Заменяют шаги-аккордеон прежней панели. */
export type Stage = "draft" | "plan" | "check";

const STAGES: { key: Stage; label: string }[] = [
  { key: "draft", label: "Чертёж" },
  { key: "plan", label: "План" },
  { key: "check", label: "Проверка" },
];

export interface HeaderProps {
  stage: Stage;
  onStageChange: (stage: Stage) => void;
  /** Вкладки «План» и «Проверка» неактивны, пока нет проекта / плана. */
  hasProject: boolean;
  hasPlan: boolean;
  projectName?: string;
  /** Подпись версии плана после имени проекта («· 3 версии»), необязательна. */
  versionLabel?: string;
  /** Число посадок с нарушениями; null — план ещё не проверен. 0 — бейджа нет. */
  violationCount: number | null;
  assistantOpen: boolean;
  /** Юна ответила, пока чат был закрыт. */
  assistantUnread: boolean;
  onToggleAssistant: () => void;
  /** Как выглядит клавиша палитры команд на этой машине: «Ctrl K» или «⌘K». */
  paletteHint?: string;
  onExport: () => void;
  exportDisabled: boolean;
  exporting: boolean;
  /** Забыть текущий проект в этом браузере и начать заново. */
  onNewProject: () => void;
}

/** Шапка: лого и «Проекты / <проект ▾>», вкладки этапов по центру, Юна и
 * «Экспорт» справа. Высота 48px, как в макете (Header.dc.html). */
export function Header({
  stage,
  onStageChange,
  hasProject,
  hasPlan,
  projectName,
  versionLabel,
  violationCount,
  assistantOpen,
  assistantUnread,
  onToggleAssistant,
  paletteHint,
  onExport,
  exportDisabled,
  exporting,
  onNewProject,
}: HeaderProps) {
  const [menuOpen, setMenuOpen] = useState(false);
  const menuRef = useRef<HTMLDivElement>(null);

  useEffect(() => {
    if (!menuOpen) return;
    function onPointerDown(e: PointerEvent) {
      if (!menuRef.current?.contains(e.target as Node)) setMenuOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === "Escape") setMenuOpen(false);
    }
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("keydown", onKeyDown);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("keydown", onKeyDown);
    };
  }, [menuOpen]);

  const enabled: Record<Stage, boolean> = { draft: true, plan: hasProject, check: hasPlan };

  return (
    <header className="flex h-12 flex-none items-center gap-1 border-b border-gp-line bg-gp-panel pl-3 pr-2 md:gap-4 md:pl-4 md:pr-3">
      {/* Телефон: имя проекта в шапке не помещается (есть в панели «Чертёж»),
          вкладки и кнопки сжаты, чтобы «Проверка» с бейджем не наезжала на Юну. */}
      <div className="flex min-w-0 flex-none items-center gap-2.5 md:flex-1 md:basis-0">
        <svg width="18" height="18" viewBox="0 0 16 16" fill="none" stroke="#B5E07A" strokeWidth="1.6" strokeLinecap="round" strokeLinejoin="round" role="img" aria-label="GreenProject" className="hidden flex-none min-[400px]:block">
          <path d="M3 13C3 7 7 3 13 3c0 6-4 10-10 10z" />
          <path d="M3 13l5-5" />
        </svg>
        <span className="hidden text-sm text-gp-text-2 md:inline">Проекты</span>
        <span className="hidden text-gp-text-4 md:inline" aria-hidden>
          /
        </span>
        {hasProject ? (
          <div ref={menuRef} className="relative hidden min-w-0 md:block">
            <button
              onClick={() => setMenuOpen((v) => !v)}
              aria-haspopup="menu"
              aria-expanded={menuOpen}
              className="flex h-8 max-w-full items-center gap-1 rounded-md px-1.5 text-sm font-medium text-gp-text hover:bg-gp-hover"
            >
              <span className="truncate">{projectName || "Проект"}</span>
              <ChevronDown className="h-4 w-4 flex-none text-gp-text-3" aria-hidden />
            </button>
            {menuOpen && (
              <div role="menu" className="gp-menu absolute left-0 top-9 z-[1130] min-w-[16rem] rounded-lg border border-gp-border bg-gp-panel py-1 shadow-xl">
                <button
                  role="menuitem"
                  onClick={() => {
                    setMenuOpen(false);
                    onNewProject();
                  }}
                  title="Забыть текущий проект в этом браузере (на сервере ничего не удаляется)"
                  className="flex h-9 w-full items-center gap-2.5 px-3 text-left text-sm text-gp-text hover:bg-gp-hover-strong"
                >
                  <RotateCcw className="h-4 w-4 text-gp-text-3" aria-hidden />
                  Новый проект
                </button>
              </div>
            )}
          </div>
        ) : (
          <span className="hidden text-sm text-gp-text-3 md:inline">Новый проект</span>
        )}
        {versionLabel && <span className="hidden whitespace-nowrap text-[13px] text-gp-text-3 lg:inline">· {versionLabel}</span>}
      </div>

      <nav aria-label="Этапы" className="flex h-12 min-w-0 flex-1 items-stretch justify-center md:flex-none">
        {STAGES.map(({ key, label }) => {
          const active = stage === key;
          const badge = key === "check" && violationCount !== null && violationCount > 0;
          return (
            <button
              key={key}
              onClick={() => onStageChange(key)}
              disabled={!enabled[key]}
              aria-current={active ? "page" : undefined}
              className={cn(
                "relative flex h-12 items-center gap-1.5 px-2 text-[13px] font-medium transition-colors disabled:cursor-not-allowed md:gap-2 md:px-3.5 md:text-sm",
                !enabled[key] ? "text-gp-text-4" : active ? "text-gp-text" : "text-gp-text-2 hover:text-gp-text"
              )}
            >
              {label}
              {badge && (
                <span
                  aria-label={`Нарушений: ${violationCount}`}
                  className="h-[18px] min-w-[18px] rounded-full bg-gp-violation-soft px-1.5 text-center font-mono text-[11px] font-medium leading-[18px] tabular-nums text-gp-violation-text"
                >
                  {violationCount}
                </span>
              )}
              {active && enabled[key] && <span className="absolute inset-x-2 -bottom-px h-0.5 bg-gp-accent md:inset-x-3.5" aria-hidden />}
            </button>
          );
        })}
      </nav>

      <div className="flex min-w-0 flex-none items-center justify-end gap-1.5 md:flex-1 md:basis-0 md:gap-2">
        <button
          onClick={onToggleAssistant}
          aria-pressed={assistantOpen}
          title="Юна — помощник по плану"
          className={cn(
            "relative flex h-[34px] items-center gap-2 rounded-lg border pl-2 pr-2.5 text-[13px] font-medium text-gp-text transition-colors sm:pr-3",
            assistantOpen ? "border-gp-accent-soft-border bg-gp-accent-soft" : "border-gp-border bg-transparent hover:bg-gp-hover"
          )}
        >
          <span className="flex h-5 w-5 items-center justify-center rounded-full bg-[#3A4A2C] text-[11px] font-semibold text-gp-accent-text">Ю</span>
          <span className="hidden sm:inline">Юна</span>
          {paletteHint && <span className="hidden text-[11px] font-normal text-gp-text-3 lg:inline">{paletteHint}</span>}
          {assistantUnread && !assistantOpen && (
            <span className="absolute -right-0.5 -top-0.5 h-2.5 w-2.5 rounded-full border-2 border-gp-panel bg-gp-warn" aria-label="Есть новый ответ" />
          )}
        </button>
        <Button size="sm" className="h-[34px] px-2.5 text-[13px] font-semibold md:px-3.5" disabled={exportDisabled || exporting} onClick={onExport}>
          {exporting ? "Экспорт…" : "Экспорт"}
        </Button>
      </div>
    </header>
  );
}
