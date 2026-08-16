import React, { useState } from "react";
import { Button, Form, FormGroup, Input, Label } from "reactstrap";
import { AlertCircle } from "react-feather";
import { api } from "../api";
import Logo from "../Logo";

export default function Login({ onSignedIn, assistant = "Ice" }) {
  const [username, setUsername] = useState("admin");
  const [password, setPassword] = useState("");
  const [error, setError] = useState(null);
  const [busy, setBusy] = useState(false);

  const submit = async (e) => {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await api.login(username, password);
      onSignedIn();
    } catch (err) {
      setError(err.message);
    } finally {
      setBusy(false);
    }
  };

  return (
    <div
      className="d-flex align-items-center justify-content-center vh-100 px-3"
      style={{
        // The sign-in screen renders before the app shell, so it paints its own
        // ground rather than inheriting the dark theme from the body class.
        background:
          "radial-gradient(1200px 600px at 50% -10%, #12212e 0%, #0b0f14 55%, #070a0e 100%)",
      }}
    >
      <div style={{ width: 380, maxWidth: "100%" }}>
        <div className="text-center mb-4">
          <Logo size={44} subtitle="LOCAL-FIRST MONITORING" />
        </div>
        <div
          className="bi-auth rounded-3 p-4"
          style={{
            background: "#131a22",
            border: "1px solid #1f2a37",
            boxShadow: "0 24px 60px rgba(0,0,0,.55)",
          }}
        >
          <h5 className="mb-1 text-white">Sign in</h5>
          <p className="small mb-4" style={{ color: "#7d8b9a" }}>
            {assistant} is listening on this network only.
          </p>
          <Form onSubmit={submit}>
            <FormGroup>
              <Label for="u" className="small" style={{ color: "#7d8b9a" }}>
                Username
              </Label>
              <Input id="u" value={username} onChange={(e) => setUsername(e.target.value)} />
            </FormGroup>
            <FormGroup className="mb-4">
              <Label for="p" className="small" style={{ color: "#7d8b9a" }}>
                Password
              </Label>
              <Input
                id="p"
                type="password"
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </FormGroup>
            {error && (
              <div className="alert alert-danger py-2 small d-flex align-items-center gap-2">
                <AlertCircle size={15} />
                {error}
              </div>
            )}
            <Button color="primary" block disabled={busy} type="submit">
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </Form>
        </div>
      </div>
    </div>
  );
}
