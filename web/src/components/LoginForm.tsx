import { useState } from "react";
import { Icon, ClaudeMark } from "../icons";

export function LoginForm({
  onLogin, theme, onToggleTheme,
}: {
  onLogin: (token: string) => void;
  theme: string;
  onToggleTheme: () => void;
}) {
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
      <button className="iconbtn tt" onClick={onToggleTheme} aria-label="切换主题">
        <Icon name={theme === "dark" ? "sun" : "moon"} />
      </button>
      <div className="login-card">
        <div className="login-brand">
          <span className="brand-mark"><ClaudeMark size={30} /></span>
          <span className="name"><b>cc</b><span>·remote</span></span>
        </div>
        <p className="login-tag serif" style={{ fontSize: 15 }}>你的 Claude Code，随身遥控</p>
        <div className="login-field">
          <Icon name="lock" size={18} />
          <input
            type="password"
            placeholder="访问密码"
            value={password}
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => { if (e.key === "Enter") submit(); }}
            disabled={loading}
            autoFocus
          />
        </div>
        {error && <div className="login-err">{error}</div>}
        <button className="login-btn" onClick={submit} disabled={loading || !password}>
          {loading ? "登录中…" : "进入"}
        </button>
        <div className="login-foot">relay · 127.0.0.1:19191 → GLM</div>
      </div>
    </div>
  );
}
