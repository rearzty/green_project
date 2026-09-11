import { cn } from "@/lib/utils";

/** A keyboard-shortcut hint next to a button/menu label. */
export function Kbd({ children, className }: { children: React.ReactNode; className?: string }) {
  return (
    <kbd
      className={cn(
        "rounded border border-stone-200 bg-stone-50 px-1.5 py-px font-sans text-[11px] font-medium leading-4 text-stone-500",
        className
      )}
    >
      {children}
    </kbd>
  );
}
