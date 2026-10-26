import type { SimStep } from "@/types/agent-data";

export interface SessionInfo {
  id: string;
  title: string;
  created_at: string;
  updated_at: string;
}

export interface ContentBlock {
  type: string;
  text?: string;
  [key: string]: unknown;
}

export interface HistoryMessage {
  role: string;
  content: string | ContentBlock[];
}

export interface Approval {
  id: string;
  name: string;
  input: Record<string, unknown>;
}

export interface RuntimeTask {
  id?: string | number;
  subject?: string;
  content?: string;
  status: string;
}

export interface RuntimeState {
  tasks: RuntimeTask[];
  todos: RuntimeTask[];
  memory_catalog: string;
  teammates: Record<string, string>;
  crons: Record<string, unknown>[];
}

export interface SessionSnapshot extends SessionInfo {
  history: HistoryMessage[];
  status: "idle" | "running";
  approvals: Approval[];
  cursor: number;
  state: RuntimeState;
}

export interface Health {
  status: string;
  model: string;
  workdir: string;
  interface: string;
}

export interface RuntimeEvent {
  id: number;
  kind: string;
  payload: Record<string, unknown>;
  created_at: string;
}

export interface RuntimeMessage {
  id: string;
  step: SimStep;
  trace: boolean;
}
