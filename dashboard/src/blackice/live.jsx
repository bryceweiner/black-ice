// One websocket for every live update. Components subscribe to a topic and
// re-render when the backend pushes; nothing polls.

import React, { createContext, useContext, useEffect, useRef, useState, useCallback } from "react";

const LiveContext = createContext({ subscribe: () => () => {}, connected: false });

export function LiveProvider({ children }) {
  const [connected, setConnected] = useState(false);
  const handlers = useRef(new Map());
  const socket = useRef(null);
  const retry = useRef(0);

  const subscribe = useCallback((topic, fn) => {
    if (!handlers.current.has(topic)) handlers.current.set(topic, new Set());
    handlers.current.get(topic).add(fn);
    return () => handlers.current.get(topic)?.delete(fn);
  }, []);

  useEffect(() => {
    let closed = false;
    let timer;

    const connect = () => {
      if (closed) return;
      const proto = window.location.protocol === "https:" ? "wss" : "ws";
      const ws = new WebSocket(`${proto}://${window.location.host}/ws`);
      socket.current = ws;

      ws.onopen = () => {
        retry.current = 0;
        setConnected(true);
      };
      ws.onmessage = (e) => {
        let msg;
        try {
          msg = JSON.parse(e.data);
        } catch {
          return;
        }
        handlers.current.get(msg.topic)?.forEach((fn) => fn(msg.payload));
        handlers.current.get("*")?.forEach((fn) => fn(msg.payload, msg.topic));
      };
      ws.onclose = () => {
        setConnected(false);
        if (closed) return;
        // Back off, but keep trying: a dropped socket means a blind dashboard.
        retry.current = Math.min(retry.current + 1, 6);
        timer = setTimeout(connect, 500 * 2 ** retry.current);
      };
      ws.onerror = () => ws.close();
    };

    connect();
    const keepalive = setInterval(() => {
      if (socket.current?.readyState === WebSocket.OPEN) socket.current.send("ping");
    }, 25000);

    return () => {
      closed = true;
      clearTimeout(timer);
      clearInterval(keepalive);
      socket.current?.close();
    };
  }, []);

  return (
    <LiveContext.Provider value={{ subscribe, connected }}>{children}</LiveContext.Provider>
  );
}

export function useLive(topic, fn) {
  const { subscribe } = useContext(LiveContext);
  const ref = useRef(fn);
  ref.current = fn;
  useEffect(() => subscribe(topic, (...a) => ref.current(...a)), [subscribe, topic]);
}

export function useConnected() {
  return useContext(LiveContext).connected;
}
