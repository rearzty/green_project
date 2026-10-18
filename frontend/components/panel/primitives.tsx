import { ChevronRight } from "lucide-react";

import { cn } from "@/lib/utils";

/** Stage text + a fill bar for a long-running background job (upload/
 * generate) -- without this, a job that can genuinely take 100+ seconds
 * reads as a hung UI, not a busy one. `progress` null (no countable total
 * yet) renders as an animated indeterminate stripe instead of a stuck 0%. */
export function ProgressBar({ label, progress }: { label: string; progress: number | null }) {
  const pct = progress !== null ? Math.round(Math.min(1, Math.max(0, progress)) * 100) : null;
  return (
    <div className="flex flex-col gap-1.5" role="status" aria-live="polite">
      <div className="h-1.5 w-full overflow-hidden rounded-full bg-gp-line">
        {pct !== null ? (
          <div className="h-full rounded-full bg-gp-accent transition-[width] duration-300" style={{ width: `${pct}%` }} />
        ) : (
          <div className="h-full w-1/3 animate-[progress-indeterminate_1.2s_ease-in-out_infinite] rounded-full bg-gp-accent" />
        )}
      </div>
      <p className="text-[13px] text-gp-text-2">
        {label}
        {pct !== null && <span className="font-mono tabular-nums"> · {pct}%</span>}
      </p>
    </div>
  );
}

/** Подпись секции: 13px/500, второй цвет, обычный регистр (не uppercase). */
export function SectionLabel({ id, children, className }: { id?: string; children: React.ReactNode; className?: string }) {
  return (
    <h2 id={id} className={cn("text-[13px] font-medium text-gp-text-2", className)}>
      {children}
    </h2>
  );
}

/** Заголовок панели: 20px/600 и, при необходимости, строка пояснения под ним. */
export function PanelTitle({ title, hint }: { title: string; hint?: React.ReactNode }) {
  return (
    <div className="px-5 pb-1 pt-5">
      <h1 className="text-xl font-semibold leading-7 text-gp-text">{title}</h1>
      {hint && <p className="mt-1 text-[13px] leading-5 text-gp-text-3">{hint}</p>}
    </div>
  );
}

/** Строка-ссылка с шевроном под списком («Почему деревьев мало», «Версии
 * плана»): отделена линией сверху, ≥ 44px, справа — необязательное значение. */
export function LinkRow({
  children,
  value,
  onClick,
  disabled,
  last,
}: {
  children: React.ReactNode;
  value?: React.ReactNode;
  onClick: () => void;
  disabled?: boolean;
  last?: boolean;
}) {
  return (
    <button
      onClick={onClick}
      disabled={disabled}
      className={cn(
        "flex min-h-11 w-full items-center justify-between gap-3 border-t border-gp-line px-5 text-left text-sm text-gp-text hover:bg-gp-hover focus-visible:outline-offset-[-2px] disabled:opacity-40",
        last && "border-b"
      )}
    >
      <span>{children}</span>
      <span className="flex items-center gap-1.5 text-[13px] text-gp-text-3">
        {value}
        <ChevronRight className="h-4 w-4" aria-hidden />
      </span>
    </button>
  );
}
