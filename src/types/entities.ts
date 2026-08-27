// src/types/entities.ts
export type WorkspaceId = string;
export type CommitmentId = string;
export type TaskId = string;
export type BlockId = string;
export type ConstraintId = string;
export type QuestionId = string;
export type RunId = string;

export type CommitmentKind = "client" | "course" | "personal" | "admin";
export type CommitmentStatus = "active" | "paused" | "done" | "dropped";

export interface Commitment {
  id: CommitmentId;
  workspaceId: WorkspaceId;
  title: string;
  kind: CommitmentKind;
  stake: 1 | 2 | 3 | 4 | 5;
  deadline: string | null; // ISO string
  openEnded: boolean;
  status: CommitmentStatus;
  estimationBias: number; // default 1.0
  sourceRef?: Record<string, unknown>;
  createdAt: string;
  updatedAt: string;
}

export type TaskEnergy = "deep" | "shallow" | "admin";
export type TaskStatus = "draft" | "ready" | "scheduled" | "in_progress" | "done" | "dropped";

export interface Task {
  id: TaskId;
  workspaceId: WorkspaceId;
  commitmentId: CommitmentId;
  title: string;
  notes?: string;
  estimateMinutes: number | null; // null triggers Question
  minBlockMinutes: number; // default 30
  energy: TaskEnergy;
  deadline: string | null;
  status: TaskStatus;
  dependsOn: TaskId[];
  orderIndex: number;
  actualMinutes?: number | null;
  confidence?: number;
  sourceSpan?: string;
  createdAt: string;
  updatedAt: string;
}

export type ConstraintKind = "recurring" | "one_off";
export type ConstraintHardness = "hard" | "soft";

export interface Constraint {
  id: ConstraintId;
  workspaceId: WorkspaceId;
  title: string;
  kind: ConstraintKind;
  rrule?: string; // RFC 5545 for recurring
  startsAt: string; // ISO string or time-of-day HH:mm
  endsAt: string;   // ISO string or time-of-day HH:mm
  hardness: ConstraintHardness;
  createdAt: string;
}

export type BlockStatus = "planned" | "done" | "partial" | "missed" | "cancelled";

export interface Block {
  id: BlockId;
  workspaceId: WorkspaceId;
  taskId: TaskId;
  startsAt: string; // ISO string
  endsAt: string;   // ISO string
  status: BlockStatus;
  actualMinutes?: number | null;
  planVersion: number;
  createdAt: string;
}

export type QuestionType =
  | "OVERLOAD"
  | "MISSING_ESTIMATE"
  | "MISSING_DEADLINE"
  | "PRIORITY_TIE"
  | "HARD_CONFLICT"
  | "IMPLAUSIBLE_DENSITY"
  | "DEPENDENCY_CYCLE"
  | "CHRONIC_MISS";

export type QuestionStatus = "open" | "answered" | "expired" | "superseded";

export interface QuestionOption {
  id: string;
  label: string;
  value: unknown;
}

export interface Question {
  id: QuestionId;
  workspaceId: WorkspaceId;
  type: QuestionType;
  entityRef?: Record<string, unknown>;
  prompt: string;
  options: QuestionOption[];
  blocking: boolean;
  status: QuestionStatus;
  answer?: unknown;
  createdAt: string;
  answeredAt?: string;
}

export interface Memory {
  workspaceId: WorkspaceId;
  content: string; // Markdown document
  version: number;
  updatedAt: string;
}
