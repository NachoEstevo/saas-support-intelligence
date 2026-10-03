import { BookOpen, Pulse } from "@phosphor-icons/react";
import type { Job } from "./types";
export default function Context({ job }: { job?: Job }) {
  return (
    <aside className="context">
      <div className="panel-heading">
        <span className="eyebrow">Detrás de la respuesta</span>
        <h2>Contexto y evidencia</h2>
      </div>
      <h3>
        <BookOpen weight="light" size={18} /> Fuentes{" "}
        <span className="count">{job?.sources.length || 0}</span>
      </h3>
      {!job?.sources.length && (
        <p className="muted">
          Las referencias consultadas aparecen aquí al completar la tarea.
        </p>
      )}
      {job?.sources.map((source) => (
        <details key={source.id}>
          <summary>
            {source.title}
            <span className={`tag ${source.kind}`}>
              {source.kind === "public" ? "Pública" : "Sintética"}
            </span>
          </summary>
          <p className="preserve">{source.text}</p>
          <small>
            {source.source} · {source.version}
          </small>
          {source.checked_at && (
            <p className="muted">Revisada {source.checked_at}</p>
          )}
          {source.kind === "public" &&
            source.source_url &&
            /^https?:\/\//i.test(source.source_url) && (
              <a href={source.source_url} target="_blank" rel="noreferrer">
                Abrir fuente pública ↗
              </a>
            )}
        </details>
      ))}
      <h3>
        <Pulse weight="light" size={18} /> Eventos de ejecución
      </h3>
      {!job?.events.length && (
        <p className="muted">
          Eventos reales del servidor, no progreso simulado.
        </p>
      )}
      <ol className="events">
        {job?.events.map((event, index) => (
          <li key={index}>
            {Object.entries(event).map(([key, value]) => (
              <div key={key}>
                <span className="muted">{key.replaceAll("_", " ")}</span>
                <span>{String(value)}</span>
              </div>
            ))}
          </li>
        ))}
      </ol>
      {job?.trace_id && <small className="trace">Traza {job.trace_id}</small>}
    </aside>
  );
}
