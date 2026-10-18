"use client";

import { useEffect, useLayoutEffect, useRef, useState } from "react";
import type { LucideIcon } from "lucide-react";

import { Kbd } from "@/components/ui/kbd";

export type ContextMenuEntry =
  | {
      label: string;
      hotkey?: string;
      icon?: LucideIcon;
      disabled?: boolean;
      danger?: boolean;
      onSelect: () => void;
    }
  | "separator";

/** Right-click menu at the pointer: every action currently possible, with
 * its keyboard shortcut. Kept inside the viewport; closes on a press
 * elsewhere, window blur/resize, or (from the page) Escape / map movement. */
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

  // Клавиатура: меню забирает фокус на первый доступный пункт, стрелки ходят
  // по пунктам, Enter нажимает (это настоящие <button>). Esc закрывает
  // страница (handleEscape) — тут не дублируем.
  useEffect(() => {
    const items = () => Array.from(ref.current?.querySelectorAll<HTMLButtonElement>("button[role=menuitem]:not(:disabled)") ?? []);
    items()[0]?.focus({ preventScroll: true });
    function onKeyDown(e: KeyboardEvent) {
      if (e.key !== "ArrowDown" && e.key !== "ArrowUp" && e.key !== "Home" && e.key !== "End") return;
      const list = items();
      if (list.length === 0) return;
      e.preventDefault();
      const current = list.indexOf(document.activeElement as HTMLButtonElement);
      let next = current;
      if (e.key === "ArrowDown") next = current < 0 ? 0 : (current + 1) % list.length;
      else if (e.key === "ArrowUp") next = current <= 0 ? list.length - 1 : current - 1;
      else if (e.key === "Home") next = 0;
      else next = list.length - 1;
      list[next]?.focus({ preventScroll: true });
    }
    window.addEventListener("keydown", onKeyDown);
    return () => window.removeEventListener("keydown", onKeyDown);
  }, []);

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
      role="menu"
      style={position}
      onContextMenu={(e) => e.preventDefault()}
      className="gp-menu fixed z-[1200] min-w-[16rem] rounded-lg border border-gp-border bg-gp-panel py-1.5 text-sm shadow-xl"
    >
      {title && <div className="px-3 pb-1.5 pt-1 text-[13px] font-medium text-gp-text-2">{title}</div>}
      {entries.map((entry, i) => {
        if (entry === "separator") return <div key={`sep-${i}`} className="my-1.5 border-t border-gp-line" />;
        const Icon = entry.icon;
        return (
          <button
            key={entry.label}
            role="menuitem"
            disabled={entry.disabled}
            onClick={() => {
              onClose();
              entry.onSelect();
            }}
            className={`flex h-9 w-full items-center gap-2.5 px-3 text-left text-sm transition-colors disabled:cursor-default disabled:opacity-40 enabled:hover:bg-gp-hover-strong focus-visible:bg-gp-hover-strong focus-visible:outline-none ${
              entry.danger ? "text-gp-violation-danger" : "text-gp-text"
            }`}
          >
            {Icon ? <Icon className="h-4 w-4 flex-none opacity-80" aria-hidden /> : <span className="w-4 flex-none" />}
            <span className="flex-1">{entry.label}</span>
            {entry.hotkey && <Kbd>{entry.hotkey}</Kbd>}
          </button>
        );
      })}
    </div>
  );
}
