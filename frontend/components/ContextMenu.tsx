"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import { ChevronRight, type LucideIcon } from "lucide-react";

import { Kbd } from "@/components/ui/kbd";
import { cn } from "@/lib/utils";

export type ContextMenuItem = {
  label: string;
  /** Справа: клавиша или число объектов (в подменю «Выделить похожие»). */
  hotkey?: string;
  icon?: LucideIcon;
  disabled?: boolean;
  danger?: boolean;
  /** Обычный пункт. У пункта с `submenu` не нужен: он только открывает подменю. */
  onSelect?: () => void;
  submenu?: ContextMenuEntry[];
};

export type ContextMenuEntry = ContextMenuItem | "separator";

/** Один список пунктов — верхний уровень меню или подменю. Фокус и стрелки
 * живут здесь же: ↑↓ Home End ходят по своим пунктам, → открывает подменю и
 * переводит в него фокус, ← закрывает подменю и возвращает фокус на родителя.
 * Подменю — потомок строки-родителя, поэтому мышь, ушедшая из пункта в
 * подменю, не «уходит» из строки и оно не закрывается. */
function MenuList({
  entries,
  onCloseAll,
  onBack,
  autoFocus = false,
  label,
}: {
  entries: ContextMenuEntry[];
  onCloseAll: () => void;
  /** Только у подменю: ← возвращает фокус родителю. */
  onBack?: () => void;
  autoFocus?: boolean;
  label?: string;
}) {
  const [open, setOpen] = useState<number | null>(null);
  // Подменю открыли с клавиатуры — фокус на его первый пункт после отрисовки.
  const [focusSubmenu, setFocusSubmenu] = useState(false);
  const buttons = useRef<(HTMLButtonElement | null)[]>([]);
  const rootRef = useRef<HTMLDivElement>(null);

  const enabledIndexes = () => entries.flatMap((entry, i) => (entry !== "separator" && !entry.disabled ? [i] : []));

  useEffect(() => {
    if (autoFocus) buttons.current[enabledIndexes()[0]]?.focus({ preventScroll: true });
    // фокус на первый пункт — один раз при открытии меню
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  function moveFocus(from: number, key: string) {
    const list = enabledIndexes();
    if (list.length === 0) return;
    const at = list.indexOf(from);
    let next = at;
    if (key === "ArrowDown") next = at < 0 ? 0 : (at + 1) % list.length;
    else if (key === "ArrowUp") next = at <= 0 ? list.length - 1 : at - 1;
    else if (key === "Home") next = 0;
    else next = list.length - 1;
    buttons.current[list[next]]?.focus({ preventScroll: true });
  }

  function onKeyDown(e: React.KeyboardEvent, index: number, entry: ContextMenuItem) {
    if (["ArrowDown", "ArrowUp", "Home", "End"].includes(e.key)) {
      e.preventDefault();
      e.stopPropagation();
      moveFocus(index, e.key);
    } else if ((e.key === "ArrowRight" || e.key === "Enter" || e.key === " ") && entry.submenu && !entry.disabled) {
      e.preventDefault();
      e.stopPropagation();
      setOpen(index);
      setFocusSubmenu(true);
    } else if (e.key === "ArrowLeft" && onBack) {
      e.preventDefault();
      e.stopPropagation();
      onBack();
    }
  }

  return (
    <div ref={rootRef} role="menu" aria-label={label}>
      {entries.map((entry, i) => {
        if (entry === "separator") return <div key={`sep-${i}`} role="separator" className="my-1.5 border-t border-gp-line" />;
        const Icon = entry.icon;
        const hasSubmenu = Boolean(entry.submenu);
        const isOpen = open === i && hasSubmenu;
        return (
          <div key={entry.label} className="relative" onMouseEnter={() => setOpen(hasSubmenu && !entry.disabled ? i : null)}>
            <button
              ref={(el) => {
                buttons.current[i] = el;
              }}
              role="menuitem"
              aria-haspopup={hasSubmenu ? "menu" : undefined}
              aria-expanded={hasSubmenu ? isOpen : undefined}
              disabled={entry.disabled}
              onKeyDown={(e) => onKeyDown(e, i, entry)}
              onClick={() => {
                if (hasSubmenu) {
                  setOpen(i);
                  return;
                }
                onCloseAll();
                entry.onSelect?.();
              }}
              className={cn(
                "flex h-9 w-full items-center gap-2.5 px-3 text-left text-sm transition-colors focus-visible:outline-none disabled:cursor-default disabled:opacity-40",
                "enabled:hover:bg-gp-hover-strong focus-visible:bg-gp-hover-strong",
                isOpen && "bg-gp-hover-strong",
                entry.danger ? "text-gp-violation-danger" : "text-gp-text"
              )}
            >
              {Icon ? <Icon className="h-4 w-4 flex-none opacity-80" aria-hidden /> : <span className="w-4 flex-none" />}
              <span className="flex-1">{entry.label}</span>
              {entry.hotkey && <Kbd>{entry.hotkey}</Kbd>}
              {hasSubmenu && <ChevronRight className="h-4 w-4 flex-none text-gp-text-3" aria-hidden />}
            </button>
            {isOpen && entry.submenu && (
              <Submenu
                entries={entry.submenu}
                label={entry.label}
                focusFirst={focusSubmenu}
                onFocused={() => setFocusSubmenu(false)}
                onCloseAll={onCloseAll}
                onBack={() => {
                  setOpen(null);
                  buttons.current[i]?.focus({ preventScroll: true });
                }}
              />
            )}
          </div>
        );
      })}
    </div>
  );
}

/** Подменю: справа от пункта-родителя, а если справа нет места — слева. */
function Submenu({
  entries,
  label,
  focusFirst,
  onFocused,
  onCloseAll,
  onBack,
}: {
  entries: ContextMenuEntry[];
  label: string;
  focusFirst: boolean;
  onFocused: () => void;
  onCloseAll: () => void;
  onBack: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [flip, setFlip] = useState(false);

  useLayoutEffect(() => {
    const rect = ref.current?.getBoundingClientRect();
    if (rect && rect.right > window.innerWidth - 8) setFlip(true);
  }, []);

  useEffect(() => {
    if (!focusFirst) return;
    ref.current?.querySelector<HTMLButtonElement>("button[role=menuitem]:not(:disabled)")?.focus({ preventScroll: true });
    onFocused();
    // фокус переводим один раз на каждое открытие с клавиатуры
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [focusFirst]);

  return (
    <div
      ref={ref}
      className={cn(
        "gp-menu absolute top-[-6px] min-w-[15.5rem] rounded-lg border border-gp-border bg-gp-panel py-1.5 shadow-xl",
        flip ? "right-full mr-0.5" : "left-full ml-0.5"
      )}
    >
      <MenuList entries={entries} label={label} onCloseAll={onCloseAll} onBack={onBack} />
    </div>
  );
}

/** Right-click menu at the pointer: every action currently possible, with
 * its keyboard shortcut, плюс подменю («Выделить похожие»). Kept inside the
 * viewport; closes on a press elsewhere, window blur/resize, or (from the
 * page) Escape / map movement. */
export function ContextMenu({
  x,
  y,
  title,
  entries,
  onClose,
}: {
  x: number;
  y: number;
  title?: string;
  entries: ContextMenuEntry[];
  onClose: () => void;
}) {
  const ref = useRef<HTMLDivElement>(null);
  const [position, setPosition] = useState({ left: x, top: y });

  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const { width, height } = el.getBoundingClientRect();
    setPosition({
      left: Math.max(8, Math.min(x, window.innerWidth - width - 8)),
      top: Math.max(8, Math.min(y, window.innerHeight - height - 8)),
    });
  }, [x, y]);

  useEffect(() => {
    function onPointerDown(e: PointerEvent) {
      if (!ref.current?.contains(e.target as Node)) onClose();
    }
    window.addEventListener("pointerdown", onPointerDown, true);
    window.addEventListener("blur", onClose);
    window.addEventListener("resize", onClose);
    return () => {
      window.removeEventListener("pointerdown", onPointerDown, true);
      window.removeEventListener("blur", onClose);
      window.removeEventListener("resize", onClose);
    };
  }, [onClose]);

  return (
    <div
      ref={ref}
      style={position}
      onContextMenu={(e) => e.preventDefault()}
      className="gp-menu fixed z-[1200] min-w-[16rem] rounded-lg border border-gp-border bg-gp-panel py-1.5 text-sm shadow-xl"
    >
      {title && <div className="px-3 pb-1.5 pt-1 text-[13px] font-medium text-gp-text-2">{title}</div>}
      <MenuList entries={entries} onCloseAll={onClose} autoFocus />
    </div>
  );
}
