import { useState } from "react";

export function LoginForm({ onLogin }: { onLogin: (token: string) => void }) {
  const [password, setPassword] = useState("");
  const [error, setError] = useState("");
  const [loading, setLoading] = useState(false);

  const submit = async () => {
    if (!password) return;
    setError("");
    setLoading(true);
    try {
      const r = await fetch("/api/login", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ password }),
      });
      if (r.status === 429) { setError("尝试太频繁，等一分钟再试"); return; }
      if (!r.ok) { setError("密码错误"); return; }
      const data = await r.json();
      onLogin(data.token as string);
    } catch {
      setError("网络错误");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login">
      <div className="login-card">
        <h2>cc-remote</h2>
        <input
          type="password"
          placeholder="密码"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
          disabled={loading}
          autoFocus
        />
        {error && <div className="login-err">{error}</div>}
        <button className="btn send login-btn" onClick={submit} disabled={loading || !password}>
          {loading ? "登录中…" : "登录"}
        </button>
      </div>
    </div>
  );
}
