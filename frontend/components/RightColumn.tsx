import { cn } from "@/lib/utils";

/** Правая колонка 360–400px (инспектор, Юна): карта рядом сужается и
 * пересчитывает размер (MapView.tsx::InvalidateSizeOnResize), а не лежит
 * под плавающей карточкой. На телефоне колонки нет — это нижняя шторка
 * поверх карты, поэтому data-map-overlay (freeMapRects не кладёт под неё
 * рамку «выдели …»; на десктопе колонка вне карты, и атрибут ничего не
 * меняет). z-index выше карточек карты (1050), ниже шторки левой панели
 * (1120). */
export function RightColumn({ label, children, className }: { label: string; children: React.ReactNode; className?: string }) {
  return (
    <aside
      aria-label={label}
      data-map-overlay
      className={cn(
        "flex min-h-0 flex-col bg-gp-panel",
        "fixed inset-x-0 bottom-0 z-[1060] max-h-[62vh] rounded-t-xl border-t border-gp-border shadow-2xl",
        "md:static md:z-auto md:max-h-none md:w-[360px] md:flex-none md:rounded-none md:border-l md:border-t-0 md:border-gp-line md:shadow-none xl:w-[380px]",
        className
      )}
    >
      {children}
    </aside>
  );
}
