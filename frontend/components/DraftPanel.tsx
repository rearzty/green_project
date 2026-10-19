"use client";

import { useState } from "react";
import { AlertTriangle, FileArchive, Loader2, Upload } from "lucide-react";

import { Button } from "@/components/ui/button";
import { PanelTitle, ProgressBar, SectionLabel } from "@/components/panel/primitives";
import type { JobProgress } from "@/lib/hooks/useProjectSession";
import type { LayerLegendEntry } from "@/lib/mapStyle";
import { errorMessage, toast } from "@/lib/toast";
import { cn } from "@/lib/utils";

const CRS_PRESETS = [
  { value: "EPSG:4326", label: "WGS84 (EPSG:4326)" },
  { value: "EPSG:3857", label: "Web Mercator (EPSG:3857)" },
  { value: "EPSG:32636", label: "UTM 36N (EPSG:32636)" },
  { value: "EPSG:32637", label: "UTM 37N (EPSG:32637)" },
] as const;
const CUSTOM_CRS = "__custom__";

export interface DraftPanelProps {
  hasProject: boolean;
  projectName?: string;
  /** Upload warnings of the current project (ProjectOut.warnings). */
  projectWarnings?: string[];
  /** True only during the brief client-side restore-from-localStorage pass. */
  restoring?: boolean;
  uploadProgress?: JobProgress | null;
  onUpload: (file: File, name: string, sourceCrs: string) => Promise<void>;
  /** Что реально лежит в загруженном чертеже (группы легенды растра). */
  layerLegend: LayerLegendEntry[];
  /** Перейти к вкладке «План» — следующий шаг после загрузки. */
  onNext: () => void;
}

/** Вкладка «Чертёж»: загрузка и сводка того, что прочиталось. Прежний шаг 1
 * аккордеона ControlPanel, вместе с его логикой загрузки (guarded, форма
 * «Другой чертёж», CRS). */
export function DraftPanel({
  hasProject,
  projectName,
  projectWarnings = [],
  restoring = false,
  uploadProgress = null,
  onUpload,
  layerLegend,
  onNext,
}: DraftPanelProps) {
  const [file, setFile] = useState<File | null>(null);
  const [name, setName] = useState("");
  // Defaults to the CRS scripts/generate_synthetic_data.py writes test data
  // in; real DXF/DWG carries no CRS, and the backend ignores this for them
  // unless the user sets it explicitly (see project_service.py).
  const [sourceCrs, setSourceCrs] = useState("EPSG:32637");
  const [crsPreset, setCrsPreset] = useState<string>("EPSG:32637");
  const [busy, setBusy] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [uploadFormOpen, setUploadFormOpen] = useState(false);

  async function guarded(action: () => Promise<void>) {
    setBusy(true);
    try {
      await action();
    } catch (e) {
      toast.error(errorMessage(e));
    } finally {
      setBusy(false);
    }
  }

  const showUploadForm = !hasProject || uploadFormOpen;
  const total = layerLegend.reduce((sum, entry) => sum + entry.count, 0);

  const uploadForm = (
    <div className="flex flex-col gap-3 px-5 pb-5 pt-3">
      <p className="text-[13px] leading-5 text-gp-text-3">
        Заархивируйте папку улицы целиком в <span className="text-gp-text-2">.zip</span>: главный чертёж и папка внешних ссылок
        рядом (<span className="font-mono text-gp-text-2">Xrefs</span>, <span className="font-mono text-gp-text-2">Ссылки</span> и т. п.).
        Граница участка обычно лежит именно в ссылках.
      </p>
      <label
        className={cn(
          "flex cursor-pointer flex-col items-center gap-1.5 rounded-lg border border-dashed px-3 py-5 text-center transition-colors",
          file ? "border-gp-accent-soft-border bg-gp-accent-soft/40" : "border-gp-border hover:border-gp-text-4 hover:bg-gp-hover"
        )}
      >
        <FileArchive className={cn("h-6 w-6", file ? "text-gp-accent" : "text-gp-text-3")} aria-hidden />
        <span className="max-w-full truncate text-sm text-gp-text">{file ? file.name : "Выберите .zip, .dwg или .dxf"}</span>
        <span className="text-xs text-gp-text-3">
          {file ? <span className="font-mono tabular-nums">{(file.size / 1024 / 1024).toFixed(1)} МБ</span> : "один чертёж — если граница участка в нём"}
        </span>
        <input
          type="file"
          accept=".dxf,.dwg,.zip,.geojson,.json"
          className="sr-only"
          onChange={(e) => {
            const picked = e.target.files?.[0] ?? null;
            setFile(picked);
            if (picked && !name) setName(picked.name.replace(/\.[^.]+$/, ""));
          }}
        />
      </label>
      <label className="flex flex-col gap-1.5 text-[13px] text-gp-text-2">
        Название проекта
        <input
          type="text"
          value={name}
          onChange={(e) => setName(e.target.value)}
          placeholder="Например, ул. Бутырская, уч. 3"
          className="h-10 rounded-lg border border-gp-border bg-gp-field px-3 text-sm text-gp-text placeholder:text-gp-text-4 focus:border-gp-accent-soft-border focus:outline-none"
        />
      </label>
      <details className="text-[13px] text-gp-text-3">
        <summary className="cursor-pointer select-none hover:text-gp-text-2">Система координат — обычно менять не нужно</summary>
        <div className="mt-2 flex flex-col gap-2">
          <p>Определяется по самому файлу. Указывайте вручную, только если точно её знаете.</p>
          <select
            value={crsPreset}
            onChange={(e) => {
              setCrsPreset(e.target.value);
              if (e.target.value !== CUSTOM_CRS) setSourceCrs(e.target.value);
            }}
            aria-label="Система координат"
            className="h-10 rounded-lg border border-gp-border bg-gp-field px-2 text-sm text-gp-text"
          >
            {CRS_PRESETS.map((p) => (
              <option key={p.value} value={p.value}>
                {p.label}
              </option>
            ))}
            <option value={CUSTOM_CRS}>Свой…</option>
          </select>
          {crsPreset === CUSTOM_CRS && (
            <input
              type="text"
              value={sourceCrs}
              onChange={(e) => setSourceCrs(e.target.value)}
              placeholder="напр. EPSG:32637"
              aria-label="Код системы координат"
              className="h-10 rounded-lg border border-gp-border bg-gp-field px-3 font-mono text-sm text-gp-text placeholder:text-gp-text-4"
            />
          )}
        </div>
      </details>
      <Button
        size="lg"
        disabled={!file || busy}
        onClick={() =>
          file &&
          guarded(async () => {
            setUploading(true);
            try {
              await onUpload(file, name || file.name, sourceCrs);
              setUploadFormOpen(false);
              setFile(null);
              setName("");
            } finally {
              setUploading(false);
            }
          })
        }
      >
        {uploading ? <Loader2 className="h-4 w-4 animate-spin" aria-hidden /> : <Upload className="h-4 w-4" aria-hidden />}
        {uploading ? "Загружаю…" : "Загрузить"}
      </Button>
      {uploadProgress && <ProgressBar label={uploadProgress.stage} progress={uploadProgress.progress} />}
    </div>
  );

  return (
    <div className="flex h-full min-h-0 flex-col">
      <div className="min-h-0 flex-1 overflow-y-auto">
        <PanelTitle title="Чертёж" hint={hasProject ? "Что прочиталось из загруженных файлов" : "Загрузите чертёж улицы — по нему строится план"} />
        {restoring && <p className="px-5 pt-2 text-[13px] text-gp-text-3">Восстановление сессии…</p>}

        {hasProject && !showUploadForm && (
          <section aria-labelledby="draft-loaded" className="px-5 pb-4 pt-4">
            <SectionLabel id="draft-loaded">Загружено</SectionLabel>
            <div className="mt-2 flex items-center justify-between gap-3">
              <p className="min-w-0 truncate text-[15px] font-medium text-gp-text" title={projectName}>
                {projectName}
              </p>
              <Button variant="outline" size="sm" onClick={() => setUploadFormOpen(true)}>
                Другой чертёж
              </Button>
            </div>
          </section>
        )}

        {hasProject && projectWarnings.length > 0 && (
          <ul className="mx-5 mb-4 flex flex-col gap-2 rounded-lg border border-gp-warn-bar/50 bg-gp-warn-bar/10 p-3 text-[13px] leading-5 text-gp-warn">
            {projectWarnings.map((warning) => (
              <li key={warning} className="flex gap-2">
                <AlertTriangle className="mt-0.5 h-4 w-4 flex-none" aria-hidden />
                <span>{warning}</span>
              </li>
            ))}
          </ul>
        )}

        {showUploadForm && uploadForm}

        {hasProject && !showUploadForm && layerLegend.length > 0 && (
          <section aria-labelledby="draft-layers" className="border-t border-gp-line px-5 pb-4 pt-4">
            <SectionLabel id="draft-layers">В чертеже</SectionLabel>
            <ul className="mt-1">
              {layerLegend.map((entry) => (
                <li key={entry.key} className="flex items-center gap-2.5 py-1.5 text-sm">
                  <span className="h-2.5 w-2.5 flex-none rounded-sm border border-white/10" style={{ backgroundColor: entry.color }} aria-hidden />
                  <span className="min-w-0 flex-1 truncate text-gp-text">{entry.label}</span>
                  <span className="font-mono text-[13px] tabular-nums text-gp-text-2">{entry.count.toLocaleString("ru-RU")}</span>
                </li>
              ))}
              <li className="flex items-center gap-2.5 border-t border-gp-line pt-2 text-[13px] text-gp-text-3">
                <span className="w-2.5 flex-none" />
                <span className="flex-1">Всего объектов</span>
                <span className="font-mono tabular-nums">{total.toLocaleString("ru-RU")}</span>
              </li>
            </ul>
          </section>
        )}
      </div>

      {hasProject && !showUploadForm && (
        <div className="flex-none border-t border-gp-line px-5 pb-5 pt-4">
          <Button size="lg" className="w-full" onClick={onNext}>
            Дальше — построить план
          </Button>
        </div>
      )}
    </div>
  );
}
