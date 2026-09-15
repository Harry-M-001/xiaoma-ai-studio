import { useEffect, useRef, useState } from "react";
import { SquarePen, Trash2, SendHorizonal, MessageSquareText, Settings } from "lucide-react";
import { api, cfgNumber, cfgString, chatStream } from "../api";
import { consumeDraftPrompt } from "../promptDraft";
import type { ChatMessage, ChatSession, ConfigMap, ModelOption } from "../types";
import { Empty, ModelSelect, Spinner } from "../components/common";
import { useToast } from "../components/Toast";

type UIMessage = ChatMessage & { error?: boolean };

export default function ChatPage({ onGoSettings }: { onGoSettings: () => void }) {
  const toast = useToast();
  const [models, setModels] = useState<ModelOption[]>([]);
  const [modelKey, setModelKey] = useState(localStorage.getItem("xm_model_text") || "");
  const [sessions, setSessions] = useState<ChatSession[]>([]);
  const [currentId, setCurrentId] = useState<number | null>(null);
  const [messages, setMessages] = useState<UIMessage[]>([]);
  const [input, setInput] = useState("");
  const [sending, setSending] = useState(false);
  const [temperature, setTemperature] = useState(0.7);
  const scrollRef = useRef<HTMLDivElement>(null);
  const textareaRef = useRef<HTMLTextAreaElement>(null);

  const refreshSessions = async () => {
    setSessions(await api.listSessions());
  };

  useEffect(() => {
    (async () => {
      // 文本模型与配置表并行拉取；任一接口失败都回落到内置默认，页面仍可用
      const [ms, ss, cfg] = await Promise.all([
        api.listModels("text").catch(() => [] as ModelOption[]),
        api.listSessions().catch(() => [] as ChatSession[]),
        api.getConfig().catch(() => ({}) as ConfigMap),
      ]);
      setModels(ms);
      setSessions(ss);
      setTemperature(cfgNumber(cfg, "defaults.temperature", 0.7));
      // 未手动选择过模型时，采用配置里的默认文本模型（仅当它确实存在）
      if (!localStorage.getItem("xm_model_text")) {
        const want = cfgString(cfg, "defaults.text_model", "");
        if (want && ms.some((m) => m.key === want)) setModelKey(want);
      }
      if (ss.length > 0) await openSession(ss[0].id);
    })();
    // 从提示词库「去对话」带过来的模板内容
    const draft = consumeDraftPrompt("chat");
    if (draft) setInput(draft);
  }, []);

  useEffect(() => {
    localStorage.setItem("xm_model_text", modelKey);
  }, [modelKey]);

  useEffect(() => {
    const el = scrollRef.current;
    if (el) el.scrollTop = el.scrollHeight;
  }, [messages]);

  useEffect(() => {
    const ta = textareaRef.current;
    if (ta) {
      ta.style.height = "auto";
      ta.style.height = `${Math.min(ta.scrollHeight, 200)}px`;
    }
  }, [input]);

  const openSession = async (id: number) => {
    setCurrentId(id);
    setMessages(await api.listMessages(id));
  };

  const newChat = () => {
    setCurrentId(null);
    setMessages([]);
    setInput("");
  };

  const removeSession = async (id: number) => {
    await api.deleteSession(id);
    if (currentId === id) newChat();
    await refreshSessions();
  };

  const send = async () => {
    const text = input.trim();
    if (!text || sending) return;
    if (!modelKey) {
      toast.error("请先在顶部选择一个文本模型");
      return;
    }

    const history = messages
      .filter((m) => !m.error)
      .map((m) => ({ role: m.role, content: m.content }));
    setMessages((prev) => [
      ...prev,
      { role: "user", content: text },
      { role: "assistant", content: "" },
    ]);
    setInput("");
    setSending(true);

    let acc = "";
    let failed: string | null = null;
    try {
      for await (const ev of chatStream({
        model_key: modelKey,
        messages: [...history, { role: "user", content: text }],
        session_id: currentId,
        temperature,
      })) {
        if (ev.type === "meta") {
          setCurrentId(ev.session_id);
        } else if (ev.type === "delta") {
          acc += ev.text;
          setMessages((prev) => {
            const next = [...prev];
            next[next.length - 1] = { role: "assistant", content: acc };
            return next;
          });
        } else if (ev.type === "error") {
          failed = ev.message || "生成失败";
        }
      }
    } catch (e) {
      failed = e instanceof Error ? e.message : "网络异常";
    }

    if (failed) {
      setMessages((prev) => {
        const next = [...prev];
        const last = next[next.length - 1];
        next[next.length - 1] = {
          role: "assistant",
          content: last.content ? `${last.content}\n\n[请求失败] ${failed}` : `[请求失败] ${failed}`,
          error: true,
        };
        return next;
      });
    }
    setSending(false);
    await refreshSessions();
  };

  if (models.length === 0) {
    return (
      <div className="page">
        <Empty
          icon={<Settings />}
          title="还没有可用的文本模型"
          desc="先到「模型服务」中添加一个 OpenAI 兼容服务（如 OpenAI、DeepSeek、月之暗面、通义千问等），并勾选文本类型模型，即可开始对话。"
          action={
            <button className="btn btn-primary" onClick={onGoSettings}>
              去配置模型服务
            </button>
          }
        />
      </div>
    );
  }

  return (
    <div className="chat-layout">
      <div className="chat-rail">
        <button className="btn btn-primary" onClick={newChat}>
          <SquarePen size={15} />
          新建对话
        </button>
        <div className="chat-rail-list mt8">
          {sessions.map((s) => (
            <div
              key={s.id}
              className={`chat-session ${s.id === currentId ? "active" : ""}`}
              onClick={() => openSession(s.id)}
            >
              <MessageSquareText size={14} style={{ flexShrink: 0 }} />
              <span className="title">{s.title}</span>
              <button
                className="del"
                title="删除会话"
                onClick={(e) => {
                  e.stopPropagation();
                  removeSession(s.id);
                }}
              >
                <Trash2 />
              </button>
            </div>
          ))}
          {sessions.length === 0 && <div className="muted" style={{ fontSize: 12, padding: "8px 10px" }}>暂无历史对话</div>}
        </div>
      </div>

      <div className="chat-main">
        <div className="chat-toolbar">
          <div className="flex1" style={{ maxWidth: 360 }}>
            <ModelSelect models={models} value={modelKey} onChange={setModelKey} placeholder="选择对话模型" />
          </div>
          {sending && (
            <span className="inline-loading">
              <Spinner /> 正在生成…
            </span>
          )}
        </div>

        <div className="chat-scroll" ref={scrollRef}>
          <div className="chat-inner">
            {messages.length === 0 && (
              <div className="empty" style={{ paddingTop: 80 }}>
                <div className="empty-icon">
                  <MessageSquareText />
                </div>
                <div className="empty-title">开始一段新对话</div>
                <div className="empty-desc">
                  连接任意 OpenAI 兼容接口，支持多轮上下文。
                  <br />
                  在下方输入消息，Enter 发送，Shift + Enter 换行。
                </div>
              </div>
            )}
            {messages.map((m, i) => (
              <div key={i} className={`msg ${m.role}`}>
                <div className="msg-avatar">{m.role === "user" ? "我" : "马"}</div>
                <div className="msg-body">
                  <div className="msg-name">{m.role === "user" ? "我" : "小马"}</div>
                  <div className="msg-content" style={m.error ? { color: "var(--danger)" } : undefined}>
                    {m.content}
                    {m.role === "assistant" && sending && i === messages.length - 1 && !m.error && (
                      <span className="cursor-blink" />
                    )}
                  </div>
                </div>
              </div>
            ))}
          </div>
        </div>

        <div className="chat-composer">
          <div className="composer-box">
            <textarea
              ref={textareaRef}
              rows={1}
              placeholder="输入消息…"
              value={input}
              onChange={(e) => setInput(e.target.value)}
              onKeyDown={(e) => {
                if (e.key === "Enter" && !e.shiftKey && !e.nativeEvent.isComposing) {
                  e.preventDefault();
                  send();
                }
              }}
            />
            <div className="composer-bar">
              <span className="composer-hint">Enter 发送 · Shift+Enter 换行</span>
              <button className="btn btn-primary btn-sm" disabled={sending || !input.trim()} onClick={send}>
                <SendHorizonal size={14} />
                发送
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  );
}
