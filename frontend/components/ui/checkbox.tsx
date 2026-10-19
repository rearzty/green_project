import { Check } from "lucide-react";

import { cn } from "@/lib/utils";

/** Чекбокс графитовой темы: настоящий `<input type="checkbox">` (его клавиатура,
 * фокус и доступность) под нарисованным квадратом. Выбранный — акцентный с
 * галочкой, пустой — рамка `gp-text-4`. Подпись — `children`; вся строка
 * кликабельна. */
export function Checkbox({
  checked,
  onChange,
  disabled = false,
  children,
  className,
}: {
  checked: boolean;
  onChange: (checked: boolean) => void;
  disabled?: boolean;
  children: React.ReactNode;
  className?: string;
}) {
  return (
    <label className={cn("flex min-h-9 cursor-pointer items-center gap-3 text-[13px] text-gp-text", disabled && "cursor-not-allowed opacity-40", className)}>
      <input type="checkbox" checked={checked} disabled={disabled} onChange={(e) => onChange(e.target.checked)} className="peer sr-only" />
      <span
        aria-hidden
        className={cn(
          "flex h-4 w-4 flex-none items-center justify-center rounded-[4px] transition-colors peer-focus-visible:outline peer-focus-visible:outline-2 peer-focus-visible:outline-offset-2 peer-focus-visible:outline-gp-selection",
          checked ? "bg-gp-accent text-gp-on-accent" : "border-[1.5px] border-gp-text-4"
        )}
      >
        {checked && <Check className="h-2.5 w-2.5" strokeWidth={3.2} />}
      </span>
      {children}
    </label>
  );
}
