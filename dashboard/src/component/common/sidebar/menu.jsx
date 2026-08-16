import React from "react";

export const MENUITEMS = [
  {
    title: "Monitoring",
    icon: <i className="pe-7s-shield pe-lg"></i>,
    path: "/sensors",
    type: "sub",
    active: true,
    bookmark: true,
    children: [
      { title: "Overview", type: "sub" },
      { title: "Sensors", type: "link", path: "/sensors" },
      { title: "Events", type: "link", path: "/events" },
      { title: "Escalations", type: "link", path: "/escalations" },
      { title: "Alarms", type: "link", path: "/alarms" },
    ],
  },
  {
    title: "System",
    icon: <i className="pe-7s-config"></i>,
    path: "/plugins",
    type: "sub",
    active: false,
    children: [
      { title: "System", type: "sub" },
      { title: "Plugins", type: "link", path: "/plugins" },
    ],
  },
];
