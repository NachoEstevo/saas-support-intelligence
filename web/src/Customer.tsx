import {
  ArrowUp,
  ChatCircle,
  Plus,
  ArrowClockwise,
} from "@phosphor-icons/react";
import { statusLabel, type Api } from "./api";
import { useCustomer } from "./useCustomer";
import Context from "./Context";
const suggestions = [
  {
    label: "¿Qué puedo encontrar en el dashboard de Rely?",
    prompt: "¿Qué puedo encontrar en el dashboard de Rely?",
  },
  {
    label: "¿Qué falta en el caso de demo CASE-101?",
    prompt: "¿Qué falta en el caso de demo CASE-101 y cómo lo cargo?",
  },
  {
    label: "Preparar un ticket por un error de carga",
    prompt:
      "CASE-101: soy owner. Falla la carga del PDF de domicilio de 2 MB con UploadFailed en tres intentos. Prepará un ticket para soporte humano.",
  },
];
export default function Customer({ api }: { api: Api }) {
  const state = useCustomer(api);
  return (
    <div className="workbench">
      <nav className="sidebar" aria-label="Conversaciones">
        <div className="panel-heading">
          <span className="eyebrow">Tu espacio</span>
          <h2>Conversaciones</h2>
        </div>
        <button
          className="primary new"
          onClick={state.createConversation}
          disabled={state.sending}
        >
          <Plus size={18} /> Nueva conversación
        </button>
        <button
          className="subtle refresh"
          aria-label="Actualizar conversaciones"
          onClick={state.refreshConversations}
          disabled={state.loadingConversations}
        >
          <ArrowClockwise size={16} /> Actualizar
        </button>
        <div className="conversation-list">
          {state.loadingConversations && (
            <p className="muted">Cargando conversaciones…</p>
          )}
          {!state.loadingConversations && !state.conversations.length && (
            <p className="muted">
              Todavía no hay conversaciones. Empezá con una pregunta.
            </p>
          )}
          {state.conversations.map((conversation) => (
            <button
              className={`conversation ${state.selected === conversation.id ? "selected" : ""}`}
              key={conversation.id}
              onClick={() => state.select(conversation.id)}
              disabled={state.sending}
            >
              <ChatCircle size={18} weight="light" />
              <span>
                {conversation.title || "Nueva conversación"}
                {conversation.last_job_status && (
                  <small>{statusLabel[conversation.last_job_status]}</small>
                )}
              </span>
            </button>
          ))}
        </div>
        <div className="sidebar-note">
          <span className="status-dot" /> Entorno educativo
          <p>
            Casos sintéticos. Fuentes públicas.
            <br />
            Sin datos de clientes reales.
          </p>
        </div>
      </nav>
      <section className="chat" aria-label="Conversación de soporte">
        <div className="chat-header">
          <div>
            <span className="eyebrow">Soporte al cliente</span>
            <h2>
              {state.conversations.find((c) => c.id === state.selected)
                ?.title || "Un poco de claridad, cuando la necesitás."}
            </h2>
          </div>
          <button
            className="icon-button"
            aria-label="Actualizar conversación"
            onClick={state.refreshHistory}
            disabled={state.sending}
          >
            <ArrowClockwise size={20} weight="light" />
          </button>
        </div>
        <div className="messages" aria-live="polite">
          {state.loading && <p className="muted">Cargando conversación…</p>}
          {!state.loading && !state.jobs.length && (
            <div className="empty-chat">
              <div className="symbol">
                <ChatCircle size={32} weight="light" />
              </div>
              <span className="eyebrow">
                Una experiencia de soporte transparente
              </span>
              <h1>
                Preguntá. Comprendé.
                <br />
                <span>Avanzá.</span>
              </h1>
              <p>
                Explorá el agente con un caso de prueba o una pregunta. Cada
                respuesta incluye sus fuentes.
              </p>
              <div className="suggestions">
                {suggestions.map((text, i) => (
                  <button
                    key={text.label}
                    onClick={() => state.setDraft(text.prompt)}
                  >
                    <span>0{i + 1}</span>
                    {text.label}
                    <ArrowUp size={16} />
                  </button>
                ))}
              </div>
            </div>
          )}
          {state.jobs.map((job) => (
            <article className="exchange" key={job.id}>
              <div className="message user-message">
                <small>Vos</small>
                <p className="preserve">{job.message}</p>
              </div>
              <div className="message agent-message">
                <small>
                  Support lab{" "}
                  <span className={`job-status ${job.status.toLowerCase()}`}>
                    {statusLabel[job.status]}
                  </span>
                </small>
                {job.response ? (
                  <>
                    <p className="preserve">{job.response.answer}</p>
                    {!!job.response.missing_documents?.length && (
                      <p>
                        Documentos faltantes:{" "}
                        {job.response.missing_documents.join(", ")}
                      </p>
                    )}
                  </>
                ) : (
                  <p className="muted">
                    {job.status === "FAILED"
                      ? "La tarea no pudo completarse. Podés volver a enviar tu pregunta."
                      : job.status === "REJECTED"
                        ? "La persona revisora rechazó este borrador."
                        : "Tu solicitud se está procesando de forma asíncrona."}
                  </p>
                )}
                {job.status === "WAITING_APPROVAL" && (
                  <p className="notice">
                    Una persona debe aprobar el borrador antes de crear el
                    ticket. Actualizá para consultar la decisión.
                  </p>
                )}
                {job.ticket_id && (
                  <p className="ticket">Ticket creado · {job.ticket_id}</p>
                )}
              </div>
            </article>
          ))}
        </div>
        <div className="composer-shell">
          {state.error && (
            <div role="alert" className="error">
              {state.error}
              <button
                disabled={state.sending}
                onClick={() => {
                  state.refreshHistory();
                  void state.refreshConversations();
                }}
              >
                Actualizar estado
              </button>
            </div>
          )}
          <form
            onSubmit={(event) => {
              event.preventDefault();
              void state.send();
            }}
          >
            <label className="sr-only" htmlFor="message">
              Mensaje
            </label>
            <textarea
              id="message"
              placeholder="Hacé una pregunta o describí un caso de prueba…"
              maxLength={4000}
              value={state.draft}
              onChange={(e) => state.setDraft(e.target.value)}
              onKeyDown={(e) => {
                if (
                  e.key === "Enter" &&
                  !e.shiftKey &&
                  !e.nativeEvent.isComposing
                ) {
                  e.preventDefault();
                  void state.send();
                }
              }}
            />
            <div className="composer-footer">
              <span>
                {state.draft.length}/4000 · Shift + Enter para un salto de línea
              </span>
              <button
                className="send"
                type="submit"
                aria-label="Enviar mensaje"
                disabled={state.blocked || !state.draft.trim()}
              >
                <ArrowUp size={20} />
              </button>
            </div>
          </form>
          <small className="disclaimer">
            Demo educativa. Verificá por tu cuenta la información legal y
            financiera.
          </small>
        </div>
      </section>
      <details className="mobile-context">
        <summary>Fuentes y eventos de ejecución</summary>
        <Context job={state.jobs.at(-1)} />
      </details>
      <div className="desktop-context">
        <Context job={state.jobs.at(-1)} />
      </div>
    </div>
  );
}
