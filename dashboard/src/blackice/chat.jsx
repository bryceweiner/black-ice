// The conversation, shared by the floating console and the home screen panel.
// Both talk to the same /chat endpoint the voice loop uses, so a question typed
// here and the same question spoken take the identical path through the harness.

import React, { useEffect, useRef, useState } from "react";
import { Button, Input, Spinner } from "reactstrap";
import { Send } from "react-feather";
import { api } from "./api";

export function useChat() {
  const [turns, setTurns] = useState([]);
  const [busy, setBusy] = useState(false);

  const send = async (message) => {
    const text = message.trim();
    if (!text || busy) return;
    setTurns((t) => [...t, { role: "user", text }]);
    setBusy(true);
    try {
      const { reply } = await api.chat(text);
      setTurns((t) => [...t, { role: "assistant", text: reply }]);
    } catch (e) {
      setTurns((t) => [...t, { role: "error", text: e.message }]);
    } finally {
      setBusy(false);
    }
  };

  return { turns, busy, send };
}

export function ChatThread({ turns, busy, hint }) {
  const bottom = useRef(null);
  useEffect(() => {
    // "nearest", and never on an empty thread: scrolling into view from a
    // standing start drags the whole page down past the panels above it.
    if (!turns.length) return;
    bottom.current?.scrollIntoView({ behavior: "smooth", block: "nearest" });
  }, [turns, busy]);

  return (
    <>
      {turns.length === 0 && hint && <p className="text-muted small mt-2 mb-0">{hint}</p>}
      {turns.map((t, i) => (
        <div key={i} className={`mb-2 d-flex ${t.role === "user" ? "justify-content-end" : ""}`}>
          <div
            className={`px-3 py-2 rounded small ${
              t.role === "user"
                ? "bg-primary text-white"
                : t.role === "error"
                ? "bg-danger-subtle text-danger"
                : "border text-body"
            }`}
            style={{ maxWidth: "85%", whiteSpace: "pre-wrap" }}
          >
            {t.text}
          </div>
        </div>
      ))}
      {busy && <Spinner size="sm" className="text-muted" />}
      <div ref={bottom} />
    </>
  );
}

export function ChatComposer({ onSend, busy, assistant }) {
  const [text, setText] = useState("");

  const submit = () => {
    if (!text.trim() || busy) return;
    onSend(text);
    setText("");
  };

  return (
    <div className="d-flex gap-2">
      <Input
        value={text}
        placeholder={`Message ${assistant}…`}
        aria-label={`Message ${assistant}`}
        onChange={(e) => setText(e.target.value)}
        onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && submit()}
        disabled={busy}
      />
      <Button color="primary" onClick={submit} disabled={busy} aria-label="Send">
        <Send size={16} />
      </Button>
    </div>
  );
}
