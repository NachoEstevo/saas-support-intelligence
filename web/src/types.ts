export type JobStatus =
  "PENDING" | "RUNNING" | "WAITING_APPROVAL" | "DONE" | "FAILED" | "REJECTED";
export type Identity = { tenant_id: string; role: "customer" | "approver" };
export type Conversation = {
  id: string;
  tenant_id: string;
  title: string;
  updated_at: number;
  last_job_status: JobStatus | null;
};
export type Source = {
  id: string;
  title: string;
  source: string;
  version: string;
  text: string;
  kind: "public" | "synthetic";
  source_url: string | null;
  checked_at: string | null;
};
export type Job = {
  id: string;
  conversation_id: string;
  tenant_id: string;
  message: string;
  status: JobStatus;
  response: null | {
    status: string;
    answer: string;
    citations: string[];
    case_id: string | null;
    missing_documents: string[];
    ticket: null | {
      subject: string;
      description: string;
      case_id: string | null;
    };
  };
  draft_answer?: string;
  sources: Source[];
  events: Record<string, string | number>[];
  trace_id: string | null;
  ticket_id: string | null;
  error: string | null;
  resume: boolean;
  approved: boolean | null;
};
export type AcceptedJob = Pick<Job, "id" | "conversation_id" | "status">;
