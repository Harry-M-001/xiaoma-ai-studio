import { useState } from "react";
import { Lock, KeyRound } from "lucide-react";
import { api, authToken } from "../api";
import { useToast } from "./Toast";

export default function LoginGate({ onSuccess }: { onSuccess: () => void }) {
  const [password, setPassword] = useState("");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState("");
  const toast = useToast();

  const submit = async () => {
    if (!password.trim()) {
      setError("请输入访问口令");
      return;
    }
    setLoading(true);
    setError("");
    try {
      const { token } = await api.login(password.trim());
      if (token) authToken.set(token);
      onSuccess();
    } catch (e) {
      setError(e instanceof Error ? e.message : "登录失败");
    } finally {
      setLoading(false);
    }
  };

  return (
    <div className="login-wrap">
      <div className="card login-card">
        <div className="brand-logo">马</div>
        <h1>小马AI工坊</h1>
        <p>
          <Lock size={13} style={{ verticalAlign: -2, marginRight: 4 }} />
          请输入访问口令以进入工作台
        </p>
        <div className="select-wrap" style={{ position: "relative" }}>
          <KeyRound
            size={15}
            style={{
              position: "absolute",
              left: 12,
              top: "50%",
              transform: "translateY(-50%)",
              color: "var(--text-3)",
              zIndex: 1,
            }}
          />
          <input
            className="input mono"
            type="password"
            placeholder="访问口令"
            style={{ paddingLeft: 36 }}
            value={password}
            autoFocus
            onChange={(e) => setPassword(e.target.value)}
            onKeyDown={(e) => e.key === "Enter" && submit()}
          />
        </div>
        {error && <div className="login-error">{error}</div>}
        <button className="btn btn-primary btn-block mt16" disabled={loading} onClick={submit}>
          {loading ? "校验中…" : "进入"}
        </button>
        <div className="field-hint mt16">
          口令在启动时通过环境变量 <code>APP_PASSWORD</code> 设置；未设置则无需口令。
        </div>
      </div>
    </div>
  );
}
