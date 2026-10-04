import type { Job } from "./types";

export class ApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
  }
}
const errors: Record<number, string> = {
  401: "La credencial es inválida o venció. Revisala y volvé a ingresar.",
  403: "Esta cuenta no tiene permiso para esta acción.",
  404: "Este elemento ya no está disponible.",
  409: "La tarea cambió. Actualizá antes de volver a intentar.",
  422: "Revisá los datos y volvé a intentar.",
  503: "El servicio de soporte no está disponible temporalmente.",
};
export function createApi(credential: string, logout: () => void) {
  async function request<T>(
    path: string,
    signal: AbortSignal,
    body?: unknown,
  ): Promise<T> {
    const requestController = new AbortController();
    const cancel = () => requestController.abort();
    signal.addEventListener("abort", cancel, { once: true });
    if (signal.aborted) cancel();
    const timeout = setTimeout(cancel, 15_000);
    try {
      let response: Response;
      try {
        response = await fetch(path, {
          signal: requestController.signal,
          method: body === undefined ? "GET" : "POST",
          headers: {
            "X-API-Key": credential,
            ...(body === undefined
              ? {}
              : { "Content-Type": "application/json" }),
          },
          ...(body === undefined ? {} : { body: JSON.stringify(body) }),
        });
      } catch (error) {
        if (signal.aborted) throw error;
        throw new Error(
          "No pudimos conectar con soporte. Revisá tu conexión y actualizá.",
        );
      }
      if (signal.aborted)
        throw new DOMException("Request cancelled", "AbortError");
      if (!response.ok) {
        if (response.status === 401) logout();
        throw new ApiError(
          response.status,
          errors[response.status] ||
            "No pudimos completar la solicitud. Volvé a intentar.",
        );
      }
      try {
        return (await response.json()) as T;
      } catch {
        throw new Error(
          "El servidor devolvió una respuesta no válida. Actualizá para volver a consultar.",
        );
      }
    } finally {
      clearTimeout(timeout);
      signal.removeEventListener("abort", cancel);
    }
  }
  return Object.assign(request, {
    streamJob: async (
      id: string,
      signal: AbortSignal,
      update: (job: Job) => void,
    ) => {
      const connection = new AbortController();
      const abort = () => connection.abort();
      signal.addEventListener("abort", abort, { once: true });
      if (signal.aborted) abort();
      const timeout = setTimeout(abort, 210_000);
      try {
        const response = await fetch(`/jobs/${id}/stream`, {
          signal: connection.signal,
          headers: { "X-API-Key": credential, Accept: "text/event-stream" },
        });
        if (!response.ok) {
          if (response.status === 401) logout();
          throw new ApiError(
            response.status,
            errors[response.status] || "No pudimos conectar con soporte.",
          );
        }
        if (
          !response.headers
            .get("Content-Type")
            ?.startsWith("text/event-stream") ||
          !response.body
        )
          throw new Error("Streaming unavailable");
        const reader = response.body.getReader();
        const decoder = new TextDecoder();
        const cancel = () => {
          void reader.cancel().catch(() => {});
        };
        connection.signal.addEventListener("abort", cancel, { once: true });
        if (connection.signal.aborted) cancel();
        let buffer = "";
        try {
          while (!connection.signal.aborted) {
            const { value, done } = await reader.read();
            if (done) break;
            buffer += decoder.decode(value, { stream: true });
            let boundary: number;
            while ((boundary = buffer.indexOf("\n\n")) >= 0) {
              const frame = buffer.slice(0, boundary);
              buffer = buffer.slice(boundary + 2);
              if (frame.length > 100_000)
                throw new Error("Invalid streaming response");
              if (frame.startsWith("event: error"))
                throw new Error("Streaming interrupted");
              if (!frame.startsWith("event: job\n")) continue;
              const updated = JSON.parse(
                frame.slice("event: job\ndata: ".length),
              ) as Job;
              if (updated.id !== id)
                throw new Error("Invalid streaming response");
              if (!signal.aborted) update(updated);
              if (!isActive(updated.status)) return;
            }
            if (buffer.length > 100_000)
              throw new Error("Invalid streaming response");
          }
        } finally {
          connection.signal.removeEventListener("abort", cancel);
          await reader.cancel().catch(() => {});
          reader.releaseLock();
        }
      } finally {
        clearTimeout(timeout);
        signal.removeEventListener("abort", abort);
      }
    },
  });
}
export type Api = ReturnType<typeof createApi>;
export const statusLabel = {
  PENDING: "En cola",
  RUNNING: "Procesando",
  WAITING_APPROVAL: "Pendiente de aprobación",
  DONE: "Completado",
  FAILED: "Falló",
  REJECTED: "Rechazado",
};
export const isActive = (status: string) =>
  status === "PENDING" || status === "RUNNING";
