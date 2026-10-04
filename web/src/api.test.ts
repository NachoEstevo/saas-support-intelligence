import { createApi } from "./api";

it("bounds a stalled network request and returns a safe human error", async () => {
  vi.useFakeTimers();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      (_path, options: RequestInit) =>
        new Promise((_resolve, reject) => {
          options.signal?.addEventListener("abort", () =>
            reject(new DOMException("aborted", "AbortError")),
          );
        }),
    ),
  );
  try {
    const pending = createApi("test-only", () => {})(
      "/jobs/j1",
      new AbortController().signal,
    );
    const assertion = expect(pending).rejects.toThrow(
      "No pudimos conectar con soporte",
    );
    await vi.advanceTimersByTimeAsync(15_000);
    await assertion;
  } finally {
    vi.useRealTimers();
  }
});

it("decodes paragraph snapshots across packet and UTF-8 boundaries and uses header auth", async () => {
  const job = { id: "j1", status: "RUNNING", draft_answer: "Párrafo uno.\n\n" };
  const done = {
    ...job,
    status: "DONE",
    draft_answer: "",
    response: { answer: "Respuesta final" },
  };
  const bytes = new TextEncoder().encode(
    `: heartbeat\n\nevent: job\ndata: ${JSON.stringify(job)}\n\nevent: job\ndata: ${JSON.stringify(done)}\n\n`,
  );
  const fetchMock = vi.fn(
    async (_path: string, _options: RequestInit) =>
      new Response(
        new ReadableStream({
          start(controller) {
            for (const byte of bytes)
              controller.enqueue(new Uint8Array([byte]));
            controller.close();
          },
        }),
        { headers: { "Content-Type": "text/event-stream" } },
      ),
  );
  vi.stubGlobal("fetch", fetchMock);
  const update = vi.fn();
  await createApi("private-password", vi.fn()).streamJob(
    "j1",
    new AbortController().signal,
    update,
  );
  expect(update.mock.calls.map(([value]) => value)).toEqual([job, done]);
  expect(fetchMock.mock.calls[0][0]).toBe("/jobs/j1/stream");
  expect(new Headers(fetchMock.mock.calls[0][1].headers).get("X-API-Key")).toBe(
    "private-password",
  );
});

it("cancels a stalled stream on abort without delivering stale paragraphs", async () => {
  let connection!: ReadableStreamDefaultController;
  const cancelled = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          new ReadableStream({
            start(controller) {
              connection = controller;
            },
            cancel: cancelled,
          }),
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    ),
  );
  const abort = new AbortController();
  const update = vi.fn();
  const running = createApi("password", vi.fn()).streamJob(
    "j1",
    abort.signal,
    update,
  );
  await vi.waitFor(() => expect(connection).toBeTruthy());
  abort.abort();
  await running;
  expect(cancelled).toHaveBeenCalledOnce();
  expect(update).not.toHaveBeenCalled();
});

it("bounds a stalled streaming connection so observation can fall back to polling", async () => {
  vi.useFakeTimers();
  const cancel = vi.fn();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(new ReadableStream({ cancel }), {
          headers: { "Content-Type": "text/event-stream" },
        }),
    ),
  );
  try {
    const pending = createApi("password", vi.fn()).streamJob(
      "j1",
      new AbortController().signal,
      vi.fn(),
    );
    await vi.advanceTimersByTimeAsync(210_000);
    await pending;
    expect(cancel).toHaveBeenCalledOnce();
  } finally {
    vi.useRealTimers();
  }
});

it("expires the session on unauthorized streaming and rejects mismatched jobs", async () => {
  const logout = vi.fn();
  const api = createApi("password", logout);
  vi.stubGlobal(
    "fetch",
    vi.fn(async () => new Response("{}", { status: 401 })),
  );
  await expect(
    api.streamJob("j1", new AbortController().signal, vi.fn()),
  ).rejects.toMatchObject({ status: 401 });
  expect(logout).toHaveBeenCalledOnce();
  vi.stubGlobal(
    "fetch",
    vi.fn(
      async () =>
        new Response(
          'event: job\ndata: {"id":"other","status":"RUNNING"}\n\n',
          { headers: { "Content-Type": "text/event-stream" } },
        ),
    ),
  );
  const update = vi.fn();
  await expect(
    api.streamJob("j1", new AbortController().signal, update),
  ).rejects.toThrow("Invalid streaming response");
  expect(update).not.toHaveBeenCalled();
});
