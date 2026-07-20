import React from "react";
import ReactDOM from "react-dom/client";
import { IBKRDashboard } from "./IBKRDashboard";
import { TradingDashboard } from "./TradingDashboard";
import logoSrc from "./cipherfx-icon.png";
import "./index.css";

const AUTH_KEY = "cipherfx_dashboard_auth_v1";
const AUTH_TTL_MS = 24 * 60 * 60 * 1000;
const DASHBOARDS = {
  ibkr: {
    label: "IBKR",
    title: "IBKR Dashboard",
    path: "/dashboard",
    loginPath: "/api/login",
    tokenKey: "cipherfx_ibkr_dashboard_token",
  },
  mt5: {
    label: "MT5",
    title: "MT5 Dashboard",
    path: "/dashboard/mt5",
    loginPath: "/mt5-api/login",
    tokenKey: "cipherfx_mt5_token",
  },
};

function volatileDashboardStorage() {
  try {
    if (!window.__cipherfxDashboardStorage) {
      window.__cipherfxDashboardStorage = Object.create(null);
    }
    return window.__cipherfxDashboardStorage;
  } catch {
    return Object.create(null);
  }
}

function safeStorageGet(key) {
  const memory = volatileDashboardStorage();
  try {
    const value = window.localStorage?.getItem(key);
    if (value) memory[key] = value;
    return value || memory[key] || "";
  } catch {
    return memory[key] || "";
  }
}

function safeStorageSet(key, value) {
  const normalized = String(value ?? "");
  volatileDashboardStorage()[key] = normalized;
  try {
    window.localStorage?.setItem(key, normalized);
  } catch {
    // Some embedded browsers deny storage; keep this session in memory.
  }
}

function safeStorageRemove(key) {
  delete volatileDashboardStorage()[key];
  try {
    window.localStorage?.removeItem(key);
  } catch {
    // Storage failure must not prevent logout or auth-state recovery.
  }
}

function currentDashboardId() {
  return window.location.pathname.startsWith("/dashboard/mt5") ? "mt5" : "ibkr";
}

function readAuthMeta() {
  try {
    const parsed = JSON.parse(safeStorageGet(AUTH_KEY) || "{}");
    return parsed && typeof parsed === "object" ? parsed : {};
  } catch {
    return {};
  }
}

function hasValidDashboardSession(dashboardId = currentDashboardId()) {
  const meta = readAuthMeta();
  const expiresAt = Number(meta.expiresAt || 0);
  const target = DASHBOARDS[dashboardId] || DASHBOARDS.ibkr;
  return expiresAt > Date.now() && Boolean(safeStorageGet(target.tokenKey));
}

function clearDashboardSession() {
  safeStorageRemove(AUTH_KEY);
  safeStorageRemove("cipherfx_mt5_mode");
  Object.values(DASHBOARDS).forEach((dashboard) => safeStorageRemove(dashboard.tokenKey));
}

async function loginDashboard(dashboard, credentials) {
  const response = await fetch(dashboard.loginPath, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(credentials),
  });
  if (!response.ok) {
    const text = await response.text().catch(() => "");
    throw new Error(text || `${dashboard.label} login failed`);
  }
  const data = await response.json();
  if (!data.token) throw new Error(`${dashboard.label} did not return a session token`);
  safeStorageSet(dashboard.tokenKey, data.token);
  return data;
}

function DashboardSwitcher({ activeDashboard, onLogout }) {
  return (
    <div className="dashboard-session-switcher" aria-label="Dashboard session controls">
      {Object.entries(DASHBOARDS).map(([id, dashboard]) => (
        <button
          key={id}
          type="button"
          className={activeDashboard === id ? "active" : ""}
          onClick={() => {
            if (activeDashboard !== id) window.location.assign(dashboard.path);
          }}
        >
          {dashboard.label}
        </button>
      ))}
      <button type="button" className="logout" onClick={onLogout}>Sign out</button>
    </div>
  );
}

function LoginPage({ activeDashboard, onLogin }) {
  const [email, setEmail] = React.useState("");
  const [password, setPassword] = React.useState("");
  const [busy, setBusy] = React.useState(false);
  const [error, setError] = React.useState("");
  const [warning, setWarning] = React.useState("");
  const [mt5Mode, setMt5Mode] = React.useState(() => safeStorageGet("cipherfx_mt5_mode") || "demo");
  const [mt5Account, setMt5Account] = React.useState("");
  const [mt5AccountPassword, setMt5AccountPassword] = React.useState("");
  const target = DASHBOARDS[activeDashboard] || DASHBOARDS.ibkr;

  async function submit(event) {
    event.preventDefault();
    setBusy(true);
    setError("");
    setWarning("");
    try {
      const credentials = { email: email.trim(), password };
      if (activeDashboard === "mt5") {
        const endpoint = mt5Mode === "live" ? "/mt5-api/live-login" : "/mt5-api/login";
        const body = mt5Mode === "live"
          ? { ...credentials, account: mt5Account.trim(), account_password: mt5AccountPassword }
          : credentials;
        const response = await fetch(endpoint, {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify(body),
        });
        if (!response.ok) throw new Error(await response.text().catch(() => "MT5 login failed"));
        const data = await response.json();
        if (!data.token) throw new Error("MT5 did not return a session token");
        safeStorageSet(DASHBOARDS.mt5.tokenKey, data.token);
        safeStorageSet("cipherfx_mt5_mode", mt5Mode);
        safeStorageSet(AUTH_KEY, JSON.stringify({
          email: credentials.email,
          expiresAt: Date.now() + AUTH_TTL_MS,
          dashboards: ["mt5"],
        }));
        setMt5AccountPassword("");
        onLogin();
      } else {
        await loginDashboard(DASHBOARDS.ibkr, credentials);
        safeStorageSet(AUTH_KEY, JSON.stringify({
          email: credentials.email,
          expiresAt: Date.now() + AUTH_TTL_MS,
          dashboards: ["ibkr"],
        }));
        onLogin();
      }
    } catch (err) {
      setError("Login failed. Check the email and password, then try again.");
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="dashboard-login-shell">
      <section className="dashboard-login-panel">
        <div className="dashboard-login-brand">
          <img src={logoSrc} alt="Cipher FX" className="dashboard-login-logo" />
          <strong>{target.title}</strong>
          <p>{activeDashboard === "mt5" ? "Choose Demo or Live MT5 mode. Live account credentials stay on the protected server." : "Secure access to the IBKR dashboard. Your browser session stays active for 24 hours."}</p>
        </div>
        <div className="dashboard-login-tabs" aria-label="Dashboard chooser">
          {Object.entries(DASHBOARDS).map(([id, dashboard]) => (
            <button
              key={id}
              type="button"
              className={activeDashboard === id ? "active" : ""}
              onClick={() => {
                if (activeDashboard !== id) window.location.assign(dashboard.path);
              }}
            >
              {dashboard.label}
            </button>
          ))}
        </div>
        <form className="dashboard-login-form" onSubmit={submit}>
          <label>
            <span>Email</span>
            <input
              autoComplete="username"
              inputMode="email"
              type="email"
              value={email}
              onChange={(event) => setEmail(event.target.value)}
              required
            />
          </label>
          <label>
            <span>Password</span>
            <input
              autoComplete="current-password"
              type="password"
              value={password}
              onChange={(event) => setPassword(event.target.value)}
              required
            />
          </label>
          {activeDashboard === "mt5" && (
            <>
              <div className="mt5-mode-switch" aria-label="MT5 account mode">
                <button type="button" className={mt5Mode === "demo" ? "active" : ""} onClick={() => { setMt5Mode("demo"); setMt5AccountPassword(""); }}>Demo mode</button>
                <button type="button" className={mt5Mode === "live" ? "active" : ""} onClick={() => setMt5Mode("live")}>Live mode</button>
              </div>
              {mt5Mode === "live" && (
                <div className="mt5-live-fields">
                  <p>Live credentials are sent to the protected server and are never stored in this browser.</p>
                  <label>
                    <span>MT5 account</span>
                    <input inputMode="numeric" type="text" value={mt5Account} onChange={(event) => setMt5Account(event.target.value)} autoComplete="off" required />
                  </label>
                  <label>
                    <span>MT5 account password</span>
                    <input type="password" value={mt5AccountPassword} onChange={(event) => setMt5AccountPassword(event.target.value)} autoComplete="off" required />
                  </label>
                </div>
              )}
            </>
          )}
          {error && <div className="dashboard-login-message error">{error}</div>}
          {warning && <div className="dashboard-login-message">{warning}</div>}
          <button type="submit" disabled={busy}>
            {busy ? "Signing in..." : `Sign in to ${target.label}`}
          </button>
        </form>
      </section>
    </main>
  );
}

function DashboardAuthGate() {
  const [activeDashboard, setActiveDashboard] = React.useState(currentDashboardId);
  const [authed, setAuthed] = React.useState(() => hasValidDashboardSession(activeDashboard));
  const Component = activeDashboard === "mt5" ? TradingDashboard : IBKRDashboard;

  React.useEffect(() => {
    const onExpired = () => {
      clearDashboardSession();
      setAuthed(false);
    };
    window.addEventListener("cipherfx:auth-expired", onExpired);
    return () => window.removeEventListener("cipherfx:auth-expired", onExpired);
  }, []);

  React.useEffect(() => {
    setActiveDashboard(currentDashboardId());
  }, []);

  function handleLogin() {
    setAuthed(hasValidDashboardSession(activeDashboard));
  }

  function handleLogout() {
    clearDashboardSession();
    setAuthed(false);
  }

  if (!authed) {
    return <LoginPage activeDashboard={activeDashboard} onLogin={handleLogin} />;
  }

  return (
    <>
      <Component onLogout={handleLogout} />
    </>
  );
}

function renderFatal(errorLike) {
  const root = document.getElementById("root");
  if (!root) return;
  const message = String(errorLike?.message || errorLike || "Unknown dashboard startup error");
  root.innerHTML = `
    <div style="min-height:100vh;background:#0B0F14;color:#E7EAEE;display:flex;align-items:center;justify-content:center;padding:24px;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">
      <div style="max-width:720px;width:100%;border:1px solid #20252C;border-radius:16px;background:#12151B;padding:24px;">
        <div style="font-size:12px;letter-spacing:.22em;text-transform:uppercase;color:#7A8290;">Cipher FX</div>
        <div style="margin-top:12px;font-size:22px;font-weight:700;">Dashboard failed to start</div>
        <div style="margin-top:12px;color:#FF7B88;white-space:pre-wrap;">${message.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" }[c]))}</div>
        <div style="margin-top:16px;color:#7A8290;font-size:12px;">Refresh the page. If this persists, the startup error is now visible instead of a black screen.</div>
      </div>
    </div>
  `;
}

window.addEventListener("error", (event) => {
  renderFatal(event.error || event.message);
});

window.addEventListener("unhandledrejection", (event) => {
  renderFatal(event.reason);
});

const rootEl = document.getElementById("root");
if (rootEl) {
  rootEl.innerHTML = '<div style="min-height:100vh;background:#0B0F14;color:#E7EAEE;display:flex;align-items:center;justify-content:center;font-family:ui-monospace,SFMono-Regular,Menlo,monospace;">Opening dashboard...</div>';
}

try {
  ReactDOM.createRoot(document.getElementById("root")).render(
    <React.StrictMode>
      <DashboardAuthGate />
    </React.StrictMode>
  );
} catch (error) {
  renderFatal(error);
}
