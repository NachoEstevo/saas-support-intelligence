import { useEffect, useRef, useState } from "react";
import { ArrowClockwise, ShieldCheck } from "@phosphor-icons/react";
import { isActive, statusLabel, type Api } from "./api";
import type { AcceptedJob, Job } from "./types";
import Context from "./Context";
export default function Approver({ api }: { api: Api }) {
  const [items, setItems] = useState<Job[]>([]);
  const [selected, setSelected] = useState<Job | null>(null);
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(true);
  const [busy, setBusy] = useState(false);
  const [confirmation, setConfirmation] = useState<boolean | null>(null);
  const controller = useRef(new AbortController());
  const timer = useRef<ReturnType<typeof setTimeout> | undefined>(undefined);
  const locked = useRef(false);
  const sequence = useRef(0);
  const refresh = async () => {
    const version = ++sequence.current;
    const signal = controller.current.signal;
    setLoading(true);
    setError("");
    try {
      const result = await api<Job[]>("/approvals", signal);
      const removedSelection =
        selected && !result.some((item) => item.id === selected.id)
          ? await api<Job>(`/jobs/${selected.id}`, signal)
          : null;
      if (!signal.aborted && version === sequence.current) {
        setItems(result);
        setSelected((current) =>
          current
            ? result.find((item) => item.id === current.id) ||
              (removedSelection?.id === current.id ? removedSelection : current)
            : null,
        );
      }
    } catch (e) {
      if (!signal.aborted && version === sequence.current)
        setError((e as Error).message);
    } finally {
      if (!signal.aborted && version === sequence.current) setLoading(false);
    }
  };
  useEffect(() => {
    const request = new AbortController();
    controller.current = request;
    void refresh();
    return () => {
      request.abort();
      clearTimeout(timer.current);
    };
  }, [api]);
  const poll = async (id: string, started: number) => {
    try {
      const updated = await api<Job>(`/jobs/${id}`, controller.current.signal);
      if (controller.current.signal.aborted) return;
      setSelected((current) => (current?.id === id ? updated : current));
      if (isActive(updated.status)) {
        if (Date.now() - started > 210_000) {
          setError(
            "Esta tarea demora más de lo esperado. Actualizá la decisión para consultar su estado.",
          );
          setBusy(false);
          locked.current = false;
        } else timer.current = setTimeout(() => void poll(id, started), 1500);
      } else {
        setBusy(false);
        locked.current = false;
        void refresh();
      }
    } catch (e) {
      if (!controller.current.signal.aborted) {
        setError((e as Error).message);
        setBusy(false);
        locked.current = false;
      }
    }
  };
  const decide = async () => {
    if (!selected || confirmation === null || locked.current) return;
    locked.current = true;
    setBusy(true);
    setError("");
    const id = selected.id;
    const approved = confirmation;
    setConfirmation(null);
    try {
      const result = await api<AcceptedJob>(
        `/jobs/${id}/approve`,
        controller.current.signal,
        { approved },
      );
      if (!controller.current.signal.aborted) {
        setSelected((current) =>
          current?.id === id ? { ...current, status: result.status } : current,
        );
        void poll(id, Date.now());
      }
    } catch (e) {
      if (!controller.current.signal.aborted) {
        setError((e as Error).message);
        setBusy(false);
        locked.current = false;
      }
    }
  };
  return (
    <div className="workbench approvals">
      <nav className="sidebar" aria-label="Bandeja de aprobación">
        <div className="panel-heading">
          <span className="eyebrow">Revisión humana</span>
          <h2>Bandeja de aprobación</h2>
        </div>
        <button
          className="subtle refresh"
          aria-label="Actualizar aprobaciones"
          onClick={refresh}
          disabled={loading}
        >
          <ArrowClockwise size={16} /> Actualizar bandeja
        </button>
        {loading && <p className="muted">Cargando borradores…</p>}
        {!loading && !items.length && (
          <p className="muted inbox-empty">
            Todo al día. No hay borradores pendientes.
          </p>
        )}
        <div className="conversation-list">
          {items.map((job) => (
            <button
              className={`conversation ${selected?.id === job.id ? "selected" : ""}`}
              key={job.id}
              onClick={() => setSelected(job)}
              disabled={busy}
            >
              <ShieldCheck size={20} weight="light" />
              <span>
                {job.response?.ticket?.subject || "Borrador de soporte"}
                <small>{statusLabel[job.status]}</small>
              </span>
            </button>
          ))}
        </div>
        <div className="sidebar-note">
          Acceso de revisión
          <p>El servidor valida los permisos y crea los tickets.</p>
        </div>
      </nav>
      <main className="review">
        <div className="chat-header">
          <div>
            <span className="eyebrow">Espacio de revisión</span>
            <h2>Tomá la decisión final.</h2>
          </div>
        </div>
        {error && (
          <div role="alert" className="error">
            {error}
            <button
              onClick={() =>
                selected && isActive(selected.status)
                  ? void poll(selected.id, Date.now())
                  : void refresh()
              }
            >
              Actualizar decisión
            </button>
          </div>
        )}
        {selected ? (
          <div className="review-body">
            <span className="job-status">{statusLabel[selected.status]}</span>
            <h1>
              {selected.response?.ticket?.subject || "Solicitud de soporte"}
            </h1>
            <h3>Solicitud del cliente</h3>
            <p className="preserve">{selected.message}</p>
            <h3>Ticket propuesto</h3>
            <p className="preserve">
              {selected.response?.ticket?.description ||
                "No hay un borrador de ticket disponible."}
            </p>
            {selected.response?.ticket?.case_id && (
              <p className="muted">Caso {selected.response.ticket.case_id}</p>
            )}
            <h3>Respuesta del agente</h3>
            <p className="preserve">{selected.response?.answer}</p>
            {selected.ticket_id && (
              <p className="ticket">Ticket creado · {selected.ticket_id}</p>
            )}
            {selected.status === "WAITING_APPROVAL" && (
              <div className="review-actions">
                <button
                  className="primary"
                  disabled={busy}
                  onClick={() => setConfirmation(true)}
                >
                  Aprobar borrador
                </button>
                <button
                  className="secondary"
                  disabled={busy}
                  onClick={() => setConfirmation(false)}
                >
                  Rechazar borrador
                </button>
              </div>
            )}
          </div>
        ) : (
          <div className="empty-chat">
            <ShieldCheck size={42} weight="light" />
            <h1>Un punto de revisión.</h1>
            <p>
              Seleccioná un borrador para revisar la evidencia y aprobar o
              rechazar el ticket propuesto.
            </p>
          </div>
        )}
      </main>
      <div className="desktop-context">
        <Context job={selected || undefined} />
      </div>
      <details className="mobile-context">
        <summary>Fuentes y eventos de ejecución</summary>
        <Context job={selected || undefined} />
      </details>
      {confirmation !== null && (
        <div className="modal-backdrop">
          <section
            className="confirmation"
            role="dialog"
            aria-modal="true"
            aria-labelledby="confirmation-title"
            onKeyDown={(e) => {
              if (e.key === "Escape") setConfirmation(null);
              if (e.key === "Tab") {
                const buttons = e.currentTarget.querySelectorAll("button");
                const first = buttons[0];
                const last = buttons[buttons.length - 1];
                if (e.shiftKey && document.activeElement === first) {
                  e.preventDefault();
                  last.focus();
                } else if (!e.shiftKey && document.activeElement === last) {
                  e.preventDefault();
                  first.focus();
                }
              }
            }}
          >
            <span className="eyebrow">Decisión humana explícita</span>
            <h2 id="confirmation-title">
              {confirmation
                ? "¿Aprobar este borrador de ticket?"
                : "¿Rechazar este borrador de ticket?"}
            </h2>
            <p>
              {confirmation
                ? "El servidor reanudará esta tarea y podrá crear el ticket propuesto."
                : "Se rechazará el borrador. No se creará un ticket."}
            </p>
            <button autoFocus className="primary" onClick={decide}>
              {confirmation ? "Confirmar aprobación" : "Confirmar rechazo"}
            </button>
            <button className="secondary" onClick={() => setConfirmation(null)}>
              Cancelar
            </button>
          </section>
        </div>
      )}
    </div>
  );
}
