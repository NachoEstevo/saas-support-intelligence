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
