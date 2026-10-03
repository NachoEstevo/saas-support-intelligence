import { useEffect, useMemo, useRef, useState } from "react";
import { ArrowRight, SignOut, List } from "@phosphor-icons/react";
import { createApi } from "./api";
import type { Identity } from "./types";
import Customer from "./Customer";
import Approver from "./Approver";
export default function App() {
  const [session, setSession] = useState<{
    credential: string;
    identity: Identity;
  } | null>(null);
  const [credential, setCredential] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [menu, setMenu] = useState(false);
  const controller = useRef<AbortController | null>(null);
  const logout = () => {
    controller.current?.abort();
    setSession(null);
    setCredential("");
    setError("");
    setMenu(false);
    setBusy(false);
  };
  const api = useMemo(
    () => (session ? createApi(session.credential, logout) : null),
    [session],
  );
  useEffect(() => () => controller.current?.abort(), []);
  const login = async () => {
    if (busy || !credential.trim()) return;
    setBusy(true);
    setError("");
    const request = new AbortController();
    controller.current = request;
    try {
      const identity = await createApi(credential, () => {})<Identity>(
        "/identity",
        request.signal,
      );
      if (!request.signal.aborted) {
        setSession({ credential, identity });
        setCredential("");
      }
    } catch (e) {
      if (!request.signal.aborted) setError((e as Error).message);
    } finally {
      if (!request.signal.aborted) setBusy(false);
    }
  };
  return (
    <div
      className={`app ${menu ? "menu-open" : ""}`}
      onKeyDown={(e) => {
        if (e.key === "Escape") setMenu(false);
      }}
      onClick={(e) => {
        if ((e.target as HTMLElement).closest(".conversation, .new"))
          setMenu(false);
      }}
    >
      <header className="topbar">
        <a className="brand" href="#">
          <span className="brand-mark">r.</span>
          <span>
            Rely <span className="brand-divider">/</span>{" "}
            <span className="brand-secondary">Support lab</span>
          </span>
        </a>
        <span className="sandbox">Sandbox</span>
        {session && (
          <>
            <span className="account">
              {session.identity.tenant_id} ·{" "}
              {session.identity.role === "approver" ? "revisor" : "cliente"}
            </span>
            <button
              className="mobile-menu icon-button"
              aria-label="Mostrar conversaciones"
              aria-expanded={menu}
              onClick={() => setMenu(!menu)}
            >
              <List size={22} />
            </button>
            <button className="subtle logout" onClick={logout}>
              <SignOut size={18} weight="light" /> Cerrar sesión
            </button>
          </>
        )}
      </header>
      {session && api ? (
        session.identity.role === "approver" ? (
          <Approver api={api} />
        ) : (
          <Customer api={api} />
        )
      ) : (
        <main className="auth">
          <section className="auth-story">
            <span className="eyebrow">Soporte, con transparencia</span>
            <h1>
              Respuestas claras.
              <br />
              <span>Evidencia a la vista.</span>
            </h1>
            <p>
              Un laboratorio para explorar respuestas fundamentadas, agentes
              asíncronos y revisión humana.
            </p>
            <div className="auth-points">
              <span>
                01 <strong>Hacé una pregunta útil</strong>
              </span>
              <span>
                02 <strong>Revisá las fuentes</strong>
              </span>
              <span>
                03 <strong>Conservá la revisión humana</strong>
              </span>
            </div>
            <p className="auth-footnote">
              Entorno educativo. No es un servicio oficial de Rely.
            </p>
          </section>
          <div className="auth-bezel">
            <section className="auth-card">
              <span className="eyebrow">Acceso a la demo local</span>
              <h2>Tu espacio de trabajo.</h2>
              <p>
                Usá una credencial de prueba configurada en tu servidor local.
                No es una API key de OpenAI.
              </p>
              <form
                onSubmit={(e) => {
                  e.preventDefault();
                  void login();
                }}
              >
                <label htmlFor="credential">Credencial de la demo</label>
                <input
                  id="credential"
                  type="password"
                  value={credential}
                  onChange={(e) => setCredential(e.target.value)}
                  autoComplete="off"
                  spellCheck={false}
                  required
                  disabled={busy}
                />
                <button
                  className="primary"
                  disabled={busy || !credential.trim()}
                >
                  {busy ? "Conectando…" : "Ingresar"}
                  <span className="button-island">
                    <ArrowRight size={18} />
                  </span>
                </button>
              </form>
              {error && (
                <p role="alert" className="error">
                  {error}
                </p>
              )}
              <small>
                La credencial queda en memoria solo durante esta sesión. Al
                salir se borra el espacio de trabajo.
              </small>
            </section>
          </div>
        </main>
      )}
    </div>
  );
}
