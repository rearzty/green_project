"use client";

import dynamic from "next/dynamic";
import { useState } from "react";

import { ControlPanel } from "@/components/ControlPanel";
import {
  exportDxfUrl,
  generatePlan,
  getProject,
  uploadProject,
  validatePlan,
  type PlanOut,
  type PlantingType,
  type ProjectOut,
  type ScoringMode,
  type ValidationViolation,
} from "@/lib/api";

// Leaflet touches `window` on import, so the map must never render on the server.
const MapView = dynamic(() => import("@/components/MapView"), { ssr: false });

export default function Home() {
  const [project, setProject] = useState<ProjectOut | null>(null);
  const [plan, setPlan] = useState<PlanOut | null>(null);
  const [violations, setViolations] = useState<ValidationViolation[] | null>(null);

  async function handleUpload(file: File, name: string, sourceCrs: string) {
    const { project_id } = await uploadProject(name, file, sourceCrs || undefined);
    const fetched = await getProject(project_id);
    setProject(fetched);
    setPlan(null);
    setViolations(null);
  }

  async function handleGenerate(plantingTypes: PlantingType[], scoringMode: ScoringMode) {
    if (!project) return;
    const generated = await generatePlan(project.id, plantingTypes, scoringMode);
    setPlan(generated);
    setViolations(null);
  }

  async function handleValidate() {
    if (!project || !plan) return;
    const result = await validatePlan(project.id, plan.plan_id);
    setViolations(result.violations);
  }

  return (
    <main className="flex h-full w-full">
      <ControlPanel
        hasProject={project !== null}
        hasPlan={plan !== null}
        violations={violations}
        onUpload={handleUpload}
        onGenerate={handleGenerate}
        onValidate={handleValidate}
        exportHref={project && plan ? exportDxfUrl(project.id, plan.plan_id) : undefined}
      />
      <div className="flex-1">
        <MapView layers={project?.layers} plan={plan?.features} />
      </div>
    </main>
  );
}
