import React from "react";
import { LiveFullstackShell } from "@/components/LiveFullstackShell";
import type { MemoryOpsScenarioId } from "@/domain/memory-ops-models";

const VALID_SCENARIOS = new Set<MemoryOpsScenarioId>([
  "default_active",
  "index_lag_filtered",
  "cursor_version_drift",
  "quota_warning_80",
  "quota_cutoff_95_paused",
  "deletion_scope_changed_409",
  "deletion_partial_pending",
  "deletion_completed",
]);

export default async function MemoryOpsSearchPage({
  searchParams,
}: {
  searchParams: Promise<{ scenario?: string; q?: string }>;
}) {
  const params = await searchParams;
  const scenario =
    typeof params.scenario === "string" &&
    VALID_SCENARIOS.has(params.scenario as MemoryOpsScenarioId)
      ? (params.scenario as MemoryOpsScenarioId)
      : "default_active";
  const initialQuery =
    typeof params.q === "string" && params.q.trim()
      ? params.q.trim()
      : "webhook";

  return (
    <LiveFullstackShell
      route="search"
      initialMemoryOpsScenario={scenario}
      initialQuery={initialQuery}
    />
  );
}
