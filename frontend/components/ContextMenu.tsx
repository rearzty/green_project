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
      className="gp-menu fixed z-[1200] min-w-[16rem] rounded-lg border border-stone-200 bg-white/95 py-1 text-sm shadow-xl backdrop-blur"
    >
      {title && <div className="px-3 pb-1 pt-1.5 text-xs font-medium text-stone-500">{title}</div>}
      {entries.map((entry, i) => {
        if (entry === "separator") return <div key={`sep-${i}`} className="my-1 border-t border-stone-100" />;
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
            className={`flex w-full items-center gap-2.5 px-3 py-1.5 text-left transition-colors disabled:cursor-default disabled:opacity-40 ${
              entry.danger ? "text-red-700 enabled:hover:bg-red-50" : "text-stone-800 enabled:hover:bg-greenery-50"
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
