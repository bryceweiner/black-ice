import React, { useState } from "react";
import { Button, Card, CardBody, Form, FormGroup, Input, Label } from "reactstrap";
import { api } from "../api";

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
    <div className="d-flex align-items-center justify-content-center vh-100 bg-light">
      <Card style={{ width: 360 }} className="shadow-sm">
        <CardBody className="p-4">
          <h4 className="mb-1">Black Ice</h4>
          <p className="text-muted small mb-4">Sign in to talk to {assistant}.</p>
          <Form onSubmit={submit}>
            <FormGroup>
              <Label for="u" className="small text-muted">Username</Label>
              <Input id="u" value={username} onChange={(e) => setUsername(e.target.value)} />
            </FormGroup>
            <FormGroup>
              <Label for="p" className="small text-muted">Password</Label>
              <Input
                id="p"
                type="password"
                autoFocus
                value={password}
                onChange={(e) => setPassword(e.target.value)}
              />
            </FormGroup>
            {error && <div className="alert alert-danger py-2 small">{error}</div>}
            <Button color="primary" block disabled={busy} type="submit">
              {busy ? "Signing in…" : "Sign in"}
            </Button>
          </Form>
        </CardBody>
      </Card>
    </div>
  );
}
