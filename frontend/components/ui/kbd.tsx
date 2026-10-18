import { cn } from "@/lib/utils";

/** Подсказка клавиши рядом с названием действия: моно, третичным цветом, без
 * рамки (рамки у каждой клавиши в меню и тулбаре дают визуальный шум). */
export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return <kbd className={cn("font-mono text-[11px] font-normal leading-4 tabular-nums text-gp-text-3", className)}>{children}</kbd>;
}
