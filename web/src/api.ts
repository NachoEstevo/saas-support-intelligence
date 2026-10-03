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
  return async function request<T>(
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
  };
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
