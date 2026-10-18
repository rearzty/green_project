"use client";

import { PanelLeftClose, PanelLeftOpen } from "lucide-react";

import { cn } from "@/lib/utils";

function ToolButton({
  label,
  hotkey,
  active,
  disabled = false,
  onClick,
  children,
}: {
  label: string;
  hotkey?: string;
  /** Только у переключаемых инструментов (выбор/рамка): aria-pressed у обычных кнопок неверен. */
  active?: boolean;
  disabled?: boolean;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      aria-label={hotkey ? `${label} (${hotkey})` : label}
      aria-pressed={active}
      title={hotkey ? `${label} · ${hotkey}` : label}
      className={cn(
        "inline-flex h-9 w-9 items-center justify-center rounded-md transition-colors disabled:opacity-40",
        active ? "bg-gp-border text-gp-text" : "text-gp-text-2 hover:bg-[#343A36] hover:text-gp-text"
      )}
    >
      {children}
    </button>
  );
}

const iconProps = { width: 16, height: 16, viewBox: "0 0 16 16", fill: "none", stroke: "currentColor", strokeWidth: 1.5, strokeLinecap: "round", strokeLinejoin: "round", "aria-hidden": true } as const;

export interface MapToolbarProps {
  panelOpen: boolean;
  onTogglePanel: () => void;
  /** Инструменты правки есть только в 2D при построенном плане. */
  showTools: boolean;
  selectMode: boolean;
  /** «Выбор» = обычный режим: карта двигается, клик выделяет. */
  onSelectTool: () => void;
  /** «Рамка» = режим выделения рамкой (клавиша S). */
  onBoxTool: () => void;
  boxDisabled: boolean;
  canUndo: boolean;
  canRedo: boolean;
  /** Правка сейчас не сохраняется (`editBusy`) — undo/redo недоступны. */
  canEdit: boolean;
  onUndo: () => void;
  onRedo: () => void;
}

/** Горизонтальный тулбар в левом верхнем углу карты: панель | выбор, рамка |
 * отменить, повторить. Переехал из секции «Ручная правка» прежней панели. Лежит
 * поверх карты, поэтому data-map-overlay (MapView.tsx::freeMapRects). */
export function MapToolbar({
  panelOpen,
  onTogglePanel,
  showTools,
  selectMode,
  onSelectTool,
  onBoxTool,
  boxDisabled,
  canUndo,
  canRedo,
  canEdit,
  onUndo,
  onRedo,
}: MapToolbarProps) {
  return (
    <div
      data-map-overlay
      role="toolbar"
      aria-label="Инструменты"
      className="pointer-events-auto absolute left-3 top-3 z-[1050] flex items-center gap-0.5 rounded-lg border border-gp-border bg-gp-panel p-[3px] shadow-lg"
    >
      <ToolButton label={panelOpen ? "Скрыть панель" : "Показать панель"} onClick={onTogglePanel}>
        {panelOpen ? <PanelLeftClose className="h-4 w-4" aria-hidden /> : <PanelLeftOpen className="h-4 w-4" aria-hidden />}
      </ToolButton>
      {showTools && (
        <>
          <span className="mx-1 h-5 w-px bg-gp-border" aria-hidden />
          <ToolButton label="Выбор" hotkey="V" active={!selectMode} onClick={onSelectTool}>
            <svg {...iconProps}>
              <path d="M3 2l10 5-4.5 1.5L7 13z" />
            </svg>
          </ToolButton>
          <ToolButton label="Рамка" hotkey="S" active={selectMode} disabled={boxDisabled && !selectMode} onClick={onBoxTool}>
            <svg {...iconProps}>
              <rect x="2.5" y="2.5" width="11" height="11" rx="1" strokeDasharray="2 2" />
            </svg>
          </ToolButton>
          <span className="mx-1 h-5 w-px bg-gp-border" aria-hidden />
          <ToolButton label="Отменить" hotkey="Ctrl+Z" disabled={!canUndo || !canEdit} onClick={onUndo}>
            <svg {...iconProps}>
              <path d="M6 4L3 7l3 3" />
              <path d="M3 7h6.5a3.5 3.5 0 010 7H7" />
            </svg>
          </ToolButton>
          <ToolButton label="Повторить" hotkey="Ctrl+Y" disabled={!canRedo || !canEdit} onClick={onRedo}>
            <svg {...iconProps}>
              <path d="M10 4l3 3-3 3" />
              <path d="M13 7H6.5a3.5 3.5 0 000 7H9" />
            </svg>
          </ToolButton>
        </>
      )}
    </div>
  );
}
