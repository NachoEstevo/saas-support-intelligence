import { useEffect, useRef, useState } from "react";
import { ApiError, isActive, type Api } from "./api";
import type { AcceptedJob, Conversation, Job } from "./types";

export function useCustomer(api: Api) {
  const [conversations, setConversations] = useState<Conversation[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [jobs, setJobs] = useState<Job[]>([]);
  const [draft, setDraft] = useState("");
  const [loading, setLoading] = useState(false);
  const [loadingConversations, setLoadingConversations] = useState(true);
  const [sending, setSending] = useState(false);
  const [error, setError] = useState("");
  const [refresh, setRefresh] = useState(0);
  const lifetime = useRef(new AbortController());
  const generation = useRef(0);
  const busy = useRef(false);
  const historySelection = useRef<string | null>(null);
  const createdJob = useRef<Job | null>(null);
  const listSequence = useRef(0);
  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => controller.abort();
  }, []);
  const refreshConversations = async () => {
    const signal = lifetime.current.signal;
    const version = ++listSequence.current;
    setLoadingConversations(true);
    try {
      const list = await api<Conversation[]>("/conversations", signal);
      if (!signal.aborted && version === listSequence.current)
        setConversations(list);
    } catch (e) {
      if (!signal.aborted && version === listSequence.current)
        setError((e as Error).message);
    } finally {
      if (!signal.aborted && version === listSequence.current)
        setLoadingConversations(false);
    }
  };
  useEffect(() => {
    void refreshConversations();
  }, [api]);
  useEffect(() => {
    const controller = new AbortController();
    generation.current++;
    setError("");
    if (historySelection.current !== selected) {
      setJobs(
        createdJob.current?.conversation_id === selected
          ? [createdJob.current]
          : [],
      );
      createdJob.current = null;
      historySelection.current = selected;
    }
    if (!selected) {
      setLoading(false);
      return () => controller.abort();
    }
    setLoading(true);
    api<Job[]>(`/conversations/${selected}/jobs`, controller.signal)
      .then((data) => {
        if (!controller.signal.aborted) setJobs(data);
      })
      .catch((e) => {
        if (!controller.signal.aborted) setError(e.message);
      })
      .finally(() => {
        if (!controller.signal.aborted) setLoading(false);
      });
    return () => controller.abort();
  }, [selected, refresh, api]);
  const active = jobs.find((job) => isActive(job.status));
  useEffect(() => {
    if (!active) return;
    const controller = new AbortController();
    const started = Date.now();
    let timer: ReturnType<typeof setTimeout>;
    let finished = false;
    const applyUpdate = (updated: Job) => {
      if (controller.signal.aborted) return;
      setJobs((items) =>
        items.map((item) => (item.id === updated.id ? updated : item)),
      );
      if (!isActive(updated.status)) {
        finished = true;
        void refreshConversations();
      }
    };
    const poll = async () => {
      try {
        const updated = await api<Job>(`/jobs/${active.id}`, controller.signal);
        if (controller.signal.aborted) return;
        applyUpdate(updated);
        if (isActive(updated.status)) {
          if (Date.now() - started > 210_000)
            setError(
              "Esta tarea demora más de lo esperado. Actualizá para consultar su estado.",
            );
          else timer = setTimeout(poll, 1500);
        }
      } catch (e) {
        if (!controller.signal.aborted) setError((e as Error).message);
      }
    };
    void api
      .streamJob(active.id, controller.signal, applyUpdate)
      .catch((e) => {
        if (e instanceof ApiError && e.status === 401) controller.abort();
      })
      .finally(() => {
        if (!controller.signal.aborted && !finished)
          timer = setTimeout(poll, 1500);
      });
    return () => {
      controller.abort();
      clearTimeout(timer);
    };
  }, [active?.id, api, refresh]);
  const createConversation = async () => {
    if (busy.current) return;
    busy.current = true;
    setSending(true);
    setError("");
    const originalDraft = draft;
    try {
      const result = await api<{ id: string }>(
        "/conversations",
        lifetime.current.signal,
        {},
      );
      if (!lifetime.current.signal.aborted) {
        setSelected(result.id);
        setDraft((current) => (current === originalDraft ? "" : current));
        void refreshConversations();
      }
    } catch (e) {
      if (!lifetime.current.signal.aborted) setError((e as Error).message);
    } finally {
      busy.current = false;
      if (!lifetime.current.signal.aborted) setSending(false);
    }
  };
  const send = async () => {
    const text = draft.trim();
    if (
      busy.current ||
      !text ||
      text.length > 4000 ||
      jobs.some((j) => isActive(j.status) || j.status === "WAITING_APPROVAL") ||
      loading
    )
      return;
    busy.current = true;
    setSending(true);
    setError("");
    const originalDraft = draft;
    const version = generation.current;
    try {
      const conversationId =
        selected ||
        (
          await api<{ id: string }>(
            "/conversations",
            lifetime.current.signal,
            {},
          )
        ).id;
      const accepted = await api<AcceptedJob>(
        `/conversations/${conversationId}/messages`,
        lifetime.current.signal,
        { message: text },
      );
      if (lifetime.current.signal.aborted || generation.current !== version)
        return;
      setDraft((current) => (current === originalDraft ? "" : current));
      const acceptedMessage = {
        ...accepted,
        tenant_id: "",
        message: text,
        response: null,
        sources: [],
        events: [],
        trace_id: null,
        ticket_id: null,
        error: null,
        resume: false,
        approved: null,
      };
      if (!selected) {
        createdJob.current = acceptedMessage;
        setSelected(conversationId);
      } else setJobs((items) => [...items, acceptedMessage]);
      void refreshConversations();
    } catch (e) {
      if (!lifetime.current.signal.aborted && generation.current === version)
        setError((e as Error).message);
    } finally {
      busy.current = false;
      if (!lifetime.current.signal.aborted) setSending(false);
    }
  };
  return {
    conversations,
    selected,
    select: (id: string) => {
      setSelected(id);
      setDraft("");
    },
    jobs,
    draft,
    setDraft,
    loading,
    loadingConversations,
    sending,
    error,
    refreshConversations,
    createConversation,
    send,
    refreshHistory: () => setRefresh((value) => value + 1),
    blocked:
      sending ||
      loading ||
      jobs.some((j) => isActive(j.status) || j.status === "WAITING_APPROVAL"),
  };
}
