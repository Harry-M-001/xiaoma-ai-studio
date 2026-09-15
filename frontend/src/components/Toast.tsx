import { createContext, useCallback, useContext, useState, type ReactNode } from "react";
import { CheckCircle2, AlertCircle, Info } from "lucide-react";

type ToastKind = "info" | "success" | "error";
interface ToastItem {
  id: number;
  kind: ToastKind;
  text: string;
}

interface ToastApi {
  show: (text: string, kind?: ToastKind) => void;
  info: (text: string) => void;
  success: (text: string) => void;
  error: (text: string) => void;
}

const ToastContext = createContext<ToastApi>({ show() {}, info() {}, success() {}, error() {} });

export function useToast() {
  return useContext(ToastContext);
}

let seq = 1;

export function ToastProvider({ children }: { children: ReactNode }) {
  const [items, setItems] = useState<ToastItem[]>([]);

  const show = useCallback((text: string, kind: ToastKind = "info") => {
    const id = seq++;
    setItems((prev) => [...prev, { id, kind, text }]);
    setTimeout(() => setItems((prev) => prev.filter((t) => t.id !== id)), 3600);
  }, []);

  const api: ToastApi = {
    show,
    info: (t) => show(t, "info"),
    success: (t) => show(t, "success"),
    error: (t) => show(t, "error"),
  };

  return (
    <ToastContext.Provider value={api}>
      {children}
      <div className="toast-wrap">
        {items.map((t) => (
          <div key={t.id} className={`toast ${t.kind === "info" ? "" : t.kind}`}>
            {t.kind === "success" && <CheckCircle2 size={15} />}
            {t.kind === "error" && <AlertCircle size={15} />}
            {t.kind === "info" && <Info size={15} />}
            {t.text}
          </div>
        ))}
      </div>
    </ToastContext.Provider>
  );
}
