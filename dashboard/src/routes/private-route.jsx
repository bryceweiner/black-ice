import React, { useEffect } from "react";
import { Outlet } from "react-router-dom";

const PrivateRoute = () => {
  // eslint-disable-next-line
  const abortController = new AbortController();

  useEffect(() => {
    const color = localStorage.getItem("color");
    const el = document.getElementById("color");
    if (el) el.setAttribute("href", `/assets/css/${color || "color-1"}.css`);
    console.ignoredYellowBox = ["Warning: Each", "Warning: Failed"];
    console.disableYellowBox = true;
    return function cleanup() {
      abortController.abort();
    };
  }, [abortController]);

  return <Outlet />;
};

export default PrivateRoute;
