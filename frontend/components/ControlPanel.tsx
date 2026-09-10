"use client";

import { useState } from "react";

import { Button } from "@/components/ui/button";
import type { PlantingType, ScoringMode, ValidationViolation } from "@/lib/api";

const PLANTING_TYPE_LABELS: Record<PlantingType, string> = {
  tree: "Деревья",
  shrub: "Кустарники",
  lawn: "Газон",
};

export interface ControlPanelProps {
  hasProject: boolean;
  hasPlan: boolean;
  /** null = validation hasn't been run yet for the current plan. */
  violations: ValidationViolation[] | null;
  onUpload: (file: File, name: string, sourceCrs: string) => Promise<void>;
  onGenerate: (plantingTypes: PlantingType[], scoringMode: ScoringMode) => Promise<void>;
  onValidate: () => Promise<void>;
  exportHref?: string;
}

export function ControlPanel({ hasProject, hasPlan, violations, onUpload, onGenerate, onValidate, exportHref }: ControlPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("Тестовая территория");
  const [sourceCrs, setSourceCrs] = useState("");
  const [plantingTypes, setPlantingTypes] = useState<PlantingType[]>(["tree", "shrub", "lawn"]);
  const [scoringMode, setScoringMode] = useState<ScoringMode>("heuristic");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  async function guarded(action: () => Promise<void>) {
    setBusy(true);
    setError(null);
    try {
      await action();
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  }

  function toggleType(type: PlantingType) {
    setPlantingTypes((prev) => (prev.includes(type) ? prev.filter((t) => t !== type) : [...prev, type]));
  }

  return (
    <aside className="flex h-full w-80 flex-col gap-4 overflow-y-auto border-r border-stone-200 bg-white p-4">
      <div>
        <h1 className="text-lg font-semibold text-greenery-700">GreenProject</h1>
        <p className="text-xs text-stone-500">Автопроектирование озеленения территории</p>
      </div>

      <section className="flex flex-col gap-2">
        <h2 className="text-sm font-medium">1. Загрузить территорию</h2>
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Название проекта"
          className="rounded border border-stone-300 px-2 py-1 text-sm"
        />
        <input
          type="text"
          value={sourceCrs}
          onChange={(e) => setSourceCrs(e.target.value)}
          placeholder="CRS (необязательно, напр. EPSG:32637)"
          className="rounded border border-stone-300 px-2 py-1 text-sm"
        />
        <input
          type="file"
          accept=".dxf,.geojson,.json,.shp"
          onChange={(e) => setFile(e.target.files?.[0] ?? null)}
          className="text-sm"
        />
        <Button
          disabled={!file || busy}
          onClick={() => file && guarded(() => onUpload(file, name, sourceCrs))}
        >
          Загрузить
        </Button>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <h2 className="text-sm font-medium">2. Сгенерировать план</h2>
        <div className="flex flex-col gap-1 text-sm">
          {(Object.keys(PLANTING_TYPE_LABELS) as PlantingType[]).map((type) => (
            <label key={type} className="flex items-center gap-2">
              <input type="checkbox" checked={plantingTypes.includes(type)} onChange={() => toggleType(type)} />
              {PLANTING_TYPE_LABELS[type]}
            </label>
          ))}
        </div>
        <div className="flex gap-3 text-sm">
          <label className="flex items-center gap-1">
            <input
              type="radio"
              checked={scoringMode === "heuristic"}
              onChange={() => setScoringMode("heuristic")}
            />
            Эвристика
          </label>
          <label className="flex items-center gap-1">
            <input type="radio" checked={scoringMode === "ml"} onChange={() => setScoringMode("ml")} />
            ML
          </label>
        </div>
        <Button
          disabled={!hasProject || plantingTypes.length === 0 || busy}
          onClick={() => guarded(() => onGenerate(plantingTypes, scoringMode))}
        >
          Сгенерировать план
        </Button>
      </section>

      <section className="flex flex-col gap-2 border-t border-stone-200 pt-3">
        <h2 className="text-sm font-medium">3. Проверить и экспортировать</h2>
        <Button variant="outline" disabled={!hasPlan || busy} onClick={() => guarded(onValidate)}>
          Проверить нормативы
        </Button>
        {violations !== null && violations.length > 0 && (
          <ul className="rounded border border-amber-300 bg-amber-50 p-2 text-xs text-amber-800">
            {violations.map((v) => (
              <li key={v.item_id}>{v.message}</li>
            ))}
          </ul>
        )}
        {violations !== null && violations.length === 0 && (
          <p className="text-xs text-greenery-700">Нарушений отступов не найдено.</p>
        )}
        <a href={exportHref} aria-disabled={!hasPlan}>
          <Button variant="outline" disabled={!hasPlan} className="w-full">
            Экспорт в DXF
          </Button>
        </a>
      </section>

      {error && <p className="rounded border border-red-300 bg-red-50 p-2 text-xs text-red-700">{error}</p>}
    </aside>
  );
}
