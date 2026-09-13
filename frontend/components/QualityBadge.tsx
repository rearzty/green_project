/** Small always-visible badge showing the current plan's aggregate quality
 * index (average per-item score, see app/page.tsx). Lives as a sibling of
 * the map/3D view, not inside ControlPanel, so it stays visible regardless
 * of whether the sidebar is collapsed. Positioned top-right specifically to
 * avoid MapView.tsx's own top-center "too many objects in view" hint. */
export function QualityBadge({ value }: { value: number | null }) {
  if (value === null) return null;

  const tone = value >= 70 ? "text-greenery-300" : value >= 40 ? "text-amber-300" : "text-red-300";

  return (
    <div className="pointer-events-none absolute right-4 top-4 z-[1050] rounded-full border border-stone-700 bg-stone-900/90 px-3 py-1.5 shadow backdrop-blur">
      <span className="text-xs text-stone-400">Индекс озеленения </span>
      <span className={`text-sm font-semibold ${tone}`}>{value}/100</span>
    </div>
  );
}
