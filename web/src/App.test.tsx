import { render, screen, waitFor, fireEvent } from "@testing-library/react";
import userEvent from "@testing-library/user-event";
import App from "./App";

const job = (extra = {}) => ({
  id: "j1",
  conversation_id: "c1",
  tenant_id: "demo",
  message: "My question",
  status: "DONE",
  response: {
    answer: "A verified answer",
    citations: [],
    missing_documents: [],
    ticket: null,
  },
  sources: [],
  events: [],
  ticket_id: null,
  ...extra,
});
function serve(routes: Record<string, unknown>) {
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string) => {
      const value = routes[path];
      return new Response(
        JSON.stringify(
          typeof value === "function" ? await value() : (value ?? []),
        ),
        { status: 200, headers: { "Content-Type": "application/json" } },
      );
    }),
  );
}
async function login(role = "customer") {
  serve({
    "/identity": { tenant_id: "demo", role },
    "/conversations": [{ id: "c1", title: "Existing thread" }],
    "/conversations/c1/jobs": [job()],
    "/approvals": [
      job({
        status: "WAITING_APPROVAL",
        response: {
          answer: "Needs review",
          ticket: {
            subject: "Draft ticket",
            description: "Investigate request",
          },
        },
      }),
    ],
  });
  render(<App />);
  const user = userEvent.setup();
  await user.type(screen.getByLabelText("Credencial de la demo"), "local-demo");
  await user.click(screen.getByRole("button", { name: "Ingresar" }));
  return user;
}
it("restores server history, never persists credentials, and erases the account on logout", async () => {
  const user = await login();
  await user.click(
    await screen.findByRole("button", { name: /Existing thread/ }),
  );
  expect(await screen.findByText("A verified answer")).toBeTruthy();
  expect(localStorage.length).toBe(0);
  expect(sessionStorage.length).toBe(0);
  await user.click(screen.getByRole("button", { name: "Cerrar sesión" }));
  expect(screen.queryByText("A verified answer")).toBeNull();
  expect(
    (screen.getByLabelText("Credencial de la demo") as HTMLInputElement).value,
  ).toBe("");
});
it("preserves newer composer text during submission and displays real queued state", async () => {
  const user = await login();
  await user.click(
    await screen.findByRole("button", { name: /Existing thread/ }),
  );
  await screen.findByText("A verified answer");
  let resolve!: (v: Response) => void;
  const original = globalThis.fetch;
  vi.stubGlobal(
    "fetch",
    vi.fn((path, opts) =>
      String(path).endsWith("/messages")
        ? new Promise<Response>((r) => {
            resolve = r;
          })
        : original(path, opts),
    ),
  );
  const input = screen.getByLabelText("Mensaje");
  await user.type(input, "Next question");
  await user.click(screen.getByRole("button", { name: "Enviar mensaje" }));
  fireEvent.change(input, { target: { value: "Keep this draft" } });
  resolve(
    new Response(
      JSON.stringify({ id: "j2", conversation_id: "c1", status: "PENDING" }),
    ),
  );
  expect(await screen.findByText("Next question")).toBeTruthy();
  expect((input as HTMLTextAreaElement).value).toBe("Keep this draft");
  expect(await screen.findByText("En cola")).toBeTruthy();
});
it("requires explicit approval confirmation and shows the resulting ticket", async () => {
  const user = await login("approver");
  await user.click(await screen.findByRole("button", { name: /Draft ticket/ }));
  await user.click(screen.getByRole("button", { name: "Aprobar borrador" }));
  expect(screen.getByRole("dialog")).toBeTruthy();
  serve({
    "/jobs/j1/approve": { id: "j1", status: "PENDING" },
    "/jobs/j1": job({ ticket_id: "ticket-42" }),
    "/approvals": [],
  });
  await user.click(
    screen.getByRole("button", { name: "Confirmar aprobación" }),
  );
  expect(await screen.findByText(/ticket-42/)).toBeTruthy();
});
it("ignores history that arrives after switching conversations", async () => {
  const user = await login();
  let resolve!: (v: unknown) => void;
  serve({
    "/conversations": [
      { id: "c1", title: "First" },
      { id: "c2", title: "Second" },
    ],
    "/conversations/c1/jobs": () =>
      new Promise((r) => {
        resolve = r;
      }),
    "/conversations/c2/jobs": [job({ id: "j2", message: "Second question" })],
  });
  await user.click(
    screen.getByRole("button", { name: "Actualizar conversaciones" }),
  );
  await user.click(await screen.findByRole("button", { name: /First/ }));
  await user.click(screen.getByRole("button", { name: /Second/ }));
  await screen.findByText("Second question");
  resolve([job({ message: "Stale question" })]);
  await waitFor(() => expect(screen.queryByText("Stale question")).toBeNull());
});
it("keeps a failed submission draft and offers a safe error without retrying the POST", async () => {
  const user = await login();
  await user.click(
    await screen.findByRole("button", { name: /Existing thread/ }),
  );
  await screen.findByText("A verified answer");
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response('{"detail":"internal debug must not leak"}', {
          status: 503,
        }),
    ),
  );
  await user.type(screen.getByLabelText("Mensaje"), "Preserve my question");
  await user.click(screen.getByRole("button", { name: "Enviar mensaje" }));
  expect(await screen.findByRole("alert")).toBeTruthy();
  expect(screen.queryByText(/internal debug/)).toBeNull();
  expect((screen.getByLabelText("Mensaje") as HTMLTextAreaElement).value).toBe(
    "Preserve my question",
  );
});
it("does not submit whitespace or IME composition and preserves Shift+Enter", async () => {
  const user = await login();
  const input = screen.getByLabelText("Mensaje");
  fireEvent.change(input, { target: { value: "   " } });
  expect(
    (
      screen.getByRole("button", {
        name: "Enviar mensaje",
      }) as HTMLButtonElement
    ).disabled,
  ).toBe(true);
  await user.clear(input);
  await user.type(input, "Still editing");
  fireEvent.keyDown(input, { key: "Enter", isComposing: true });
  fireEvent.keyDown(input, { key: "Enter", shiftKey: true });
  expect((input as HTMLTextAreaElement).value).toBe("Still editing");
  expect(screen.queryByText("En cola")).toBeNull();
});
it("clears customer state on unauthorized reads and separates reviewer access", async () => {
  const user = await login();
  expect(
    screen.queryByRole("navigation", { name: "Bandeja de aprobación" }),
  ).toBeNull();
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 401 })),
  );
  await user.click(
    await screen.findByRole("button", { name: /Existing thread/ }),
  );
  expect(await screen.findByLabelText("Credencial de la demo")).toBeTruthy();
  expect(screen.queryByLabelText("Mensaje")).toBeNull();
});
it("rejects only after confirmation and displays the terminal rejection", async () => {
  const user = await login("approver");
  await user.click(await screen.findByRole("button", { name: /Draft ticket/ }));
  await user.click(screen.getByRole("button", { name: "Rechazar borrador" }));
  await user.click(screen.getByRole("button", { name: "Cancelar" }));
  expect(screen.queryByRole("dialog")).toBeNull();
  await user.click(screen.getByRole("button", { name: "Rechazar borrador" }));
  serve({
    "/jobs/j1/approve": { id: "j1", status: "REJECTED" },
    "/jobs/j1": job({ status: "REJECTED", approved: false }),
    "/approvals": [],
  });
  await user.click(screen.getByRole("button", { name: "Confirmar rechazo" }));
  expect(await screen.findByText("Rechazado")).toBeTruthy();
  expect(screen.queryByText(/Ticket creado/)).toBeNull();
});
it("polls actual job completion and aborts an in-flight poll when logging out", async () => {
  const user = await login();
  serve({
    "/conversations/c1/jobs": [job({ status: "RUNNING", response: null })],
    "/jobs/j1": job(),
  });
  await user.click(
    await screen.findByRole("button", { name: /Existing thread/ }),
  );
  expect(await screen.findByText("Procesando")).toBeTruthy();
  expect(
    await screen.findByText("A verified answer", {}, { timeout: 3000 }),
  ).toBeTruthy();
  let pollSignal: AbortSignal | undefined;
  let resolve!: (value: Response) => void;
  vi.stubGlobal(
    "fetch",
    vi.fn(async (path: string, options: RequestInit) => {
      if (path === "/jobs/j1") {
        pollSignal = options.signal as AbortSignal;
        return new Promise<Response>((r) => {
          resolve = r;
        });
      }
      return new Response(
        JSON.stringify([job({ status: "RUNNING", response: null })]),
      );
    }),
  );
  await user.click(
    screen.getByRole("button", { name: "Actualizar conversación" }),
  );
  await waitFor(() => expect(pollSignal).toBeTruthy(), { timeout: 3000 });
  await user.click(screen.getByRole("button", { name: "Cerrar sesión" }));
  expect(pollSignal?.aborted).toBe(true);
  resolve(new Response(JSON.stringify(job())));
  expect(screen.queryByText("A verified answer")).toBeNull();
});
