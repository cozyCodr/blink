// src/core/scoring/priority-score.ts
import { clamp, round } from "../utils/math-utils.js";

export interface ScoreInputs {
  estimateMinutes: number;
  estimationBias: number;
  deadline: Date | null;
  now: Date;
  stake: 1 | 2 | 3 | 4 | 5;
  depDepth?: number;
}

/**
 * Priority Score calculation as defined in Architecture §6.3:
 * remaining   = estimate_minutes * commitment.estimation_bias
 * slack_min   = minutes_until_deadline - remaining
 * urgency     = 1 / max(slack_min, 1)
 * dep_depth   = length of longest chain this task unblocks
 * score       = urgency * (stake ^ 1.5) * (1 + 0.2 * dep_depth)
 */
export function calculatePriorityScore(inputs: ScoreInputs): number {
  const { estimateMinutes, estimationBias, deadline, now, stake, depDepth = 0 } = inputs;
  const remaining = estimateMinutes * (estimationBias || 1.0);

  let urgency = 1.0;
  if (deadline) {
    const minutesUntilDeadline = (deadline.getTime() - now.getTime()) / 60_000;
    const slackMin = minutesUntilDeadline - remaining;
    // Clamped so overdue or very low slack has high urgency, while far-future has low urgency
    urgency = 1 / Math.max(slackMin, 1);
  } else {
    // Open-ended commitment baseline urgency
    urgency = 0.0001;
  }

  const stakeWeight = Math.pow(stake, 1.5);
  const depFactor = 1 + 0.2 * Math.max(0, depDepth);

  const rawScore = urgency * stakeWeight * depFactor * 1000;
  return round(rawScore, 4);
}
