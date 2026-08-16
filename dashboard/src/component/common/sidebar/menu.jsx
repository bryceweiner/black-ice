// Feather icons, not the theme's icon font: react-feather is already a
// dependency and is what every other panel draws with, so the sidebar now
// matches the rest of the UI at the same stroke weight.

import React from "react";
import { AlertTriangle, Home, Package, Radio } from "react-feather";

export const MENUITEMS = [
  {
    title: "Home",
    icon: <Home size={22} />,
    path: "/home",
    type: "link",
    active: false,
  },
  {
    title: "Monitoring",
    icon: <Radio size={22} />,
    path: "/sensors",
    type: "sub",
    active: true,
    children: [
      { title: "Monitoring", type: "sub" },
      { title: "Sensors", type: "link", path: "/sensors" },
      { title: "Events", type: "link", path: "/events" },
    ],
  },
  {
    title: "Response",
    icon: <AlertTriangle size={22} />,
    path: "/escalations",
    type: "sub",
    active: false,
    children: [
      { title: "Response", type: "sub" },
      { title: "Escalations", type: "link", path: "/escalations" },
      { title: "Alarms", type: "link", path: "/alarms" },
    ],
  },
  {
    title: "System",
    icon: <Package size={22} />,
    path: "/plugins",
    type: "sub",
    active: false,
    children: [
      { title: "System", type: "sub" },
      { title: "Plugins", type: "link", path: "/plugins" },
    ],
  },
];
