// The floating console: the same conversation as the home screen, kept within
// reach on every other page. Anything you can type here you can also say.

import React, { useState } from "react";
import { Button } from "reactstrap";
import { MessageSquare, X } from "react-feather";
import { ChatComposer, ChatThread, useChat } from "./chat";
import { useConnected } from "./live";

export default function Console({ assistant = "Ice" }) {
  const [open, setOpen] = useState(false);
  const { turns, busy, send } = useChat();
  const connected = useConnected();

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
        <ChatThread
          turns={turns}
          busy={busy}
          hint="Ask about sensors, search events, or arm and disarm alarms. Everything here works by voice too."
        />
      </div>

      <div className="border-top p-2">
        <ChatComposer onSend={send} busy={busy} assistant={assistant} />
      </div>
    </div>
  );
}
