import React from "react";
import { LiveFullstackShell } from "@/components/LiveFullstackShell";
import type { ClaimReviewScenarioId } from "@/domain/claim-review-models";

const VALID_SCENARIOS = new Set<ClaimReviewScenarioId>([
  "default_grounded",
  "conflict_on_next_mutation",
  "manual_only_llm_blocked",
  "generation_synthetic_ready",
  "policy_blocked",
  "deletion_pending",
  "import_not_ready",
]);

export default async function ClaimReviewPage({
  searchParams,
}: {
  searchParams: Promise<{ event_id?: string; scenario?: string }>;
}) {
  const params = await searchParams;
  const eventId =
    typeof params.event_id === "string" && params.event_id.trim()
      ? params.event_id.trim()
      : "evt_comp_301";
  const scenario =
    typeof params.scenario === "string" &&
    VALID_SCENARIOS.has(params.scenario as ClaimReviewScenarioId)
      ? (params.scenario as ClaimReviewScenarioId)
      : "default_grounded";

  return (
    <LiveFullstackShell
      route="claim-review"
      eventId={eventId}
      initialClaimScenario={scenario}
    />
  );
}
