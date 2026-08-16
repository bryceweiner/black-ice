// Persistent text console. Shares the harness with voice, so anything you can
// type here you can also say.

import React, { useEffect, useRef, useState } from "react";
import { Button, Input, Spinner } from "reactstrap";
import { MessageSquare, Send, X } from "react-feather";
import { api } from "./api";
import { useConnected } from "./live";

export default function Console({ assistant = "Ice" }) {
  const [open, setOpen] = useState(false);
  const [turns, setTurns] = useState([]);
  const [text, setText] = useState("");
  const [busy, setBusy] = useState(false);
  const bottom = useRef(null);
  const connected = useConnected();

  useEffect(() => {
    bottom.current?.scrollIntoView({ behavior: "smooth" });
  }, [turns, open]);

  const send = async () => {
    const message = text.trim();
    if (!message || busy) return;
    setText("");
    setTurns((t) => [...t, { role: "user", text: message }]);
    setBusy(true);
    try {
      const { reply } = await api.chat(message);
      setTurns((t) => [...t, { role: "assistant", text: reply }]);
    } catch (e) {
      setTurns((t) => [...t, { role: "error", text: e.message }]);
    } finally {
      setBusy(false);
    }
  };

  if (!open) {
    return (
      <Button
        color="primary"
        className="position-fixed rounded-circle shadow"
        style={{ right: 24, bottom: 24, width: 52, height: 52, zIndex: 1040 }}
        onClick={() => setOpen(true)}
        aria-label={`Open ${assistant} console`}
      >
        <MessageSquare size={20} />
      </Button>
    );
  }

  return (
    <div
      className="position-fixed bg-body border rounded shadow-lg d-flex flex-column"
      style={{ right: 24, bottom: 24, width: 380, height: 520, zIndex: 1040 }}
    >
      <div className="d-flex justify-content-between align-items-center border-bottom px-3 py-2">
        <strong>
          {assistant}
          <span
            className={`ms-2 badge ${connected ? "bg-success" : "bg-secondary"}`}
            title={connected ? "Live updates connected" : "Reconnecting"}
            style={{ width: 8, height: 8, padding: 0, borderRadius: "50%" }}
          />
        </strong>
        <Button close onClick={() => setOpen(false)} aria-label="Close console">
          <X size={16} />
        </Button>
      </div>

      <div className="flex-grow-1 overflow-auto px-3 py-2">
        {turns.length === 0 && (
          <p className="text-muted small mt-2">
            Ask about sensors, search events, or arm and disarm alarms.
            Everything here works by voice too.
          </p>
        )}
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
      </div>

      <div className="border-top p-2 d-flex gap-2">
        <Input
          value={text}
          placeholder={`Message ${assistant}…`}
          onChange={(e) => setText(e.target.value)}
          onKeyDown={(e) => e.key === "Enter" && !e.shiftKey && send()}
          disabled={busy}
        />
        <Button color="primary" onClick={send} disabled={busy} aria-label="Send">
          <Send size={16} />
        </Button>
      </div>
    </div>
  );
}
