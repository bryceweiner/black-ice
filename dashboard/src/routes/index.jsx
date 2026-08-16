import React, { useEffect, useState } from "react";
import { Navigate, Route, Routes } from "react-router-dom";
import PrivateRoute from "./private-route";
import AppLayout from "../App";
import { routes } from "./layouts-routes";
import Login from "../blackice/pages/Login";
import Loader from "../component/common/loader/loader";
import { api } from "../blackice/api";

const MainRoutes = () => {
  // Session state drives the whole tree: without it the app would render the
  // shell and then have every panel 401 individually.
  const [session, setSession] = useState({ status: "checking", assistant: "Ice" });

  const check = () =>
    api
      .health()
      .then((h) => api.get("/me")
        .then((me) => setSession({ status: "in", assistant: me.assistant, username: me.username }))
        .catch(() => setSession({ status: "out", assistant: h.assistant })))
      .catch(() => setSession({ status: "out", assistant: "Ice" }));

  useEffect(() => { check(); }, []);

  if (session.status === "checking") return <Loader />;
  if (session.status === "out") {
    return <Login assistant={session.assistant} onSignedIn={check} />;
  }

  return (
    <Routes>
      <Route path="/" element={<PrivateRoute />}>
        <Route element={<AppLayout assistant={session.assistant} username={session.username} />}>
          <Route path="" element={<Navigate to="/home" replace />} />
          {routes.map(({ path, Component }, i) => (
            <Route path={path} element={Component} key={i} />
          ))}
        </Route>
      </Route>
    </Routes>
  );
};

export default MainRoutes;
