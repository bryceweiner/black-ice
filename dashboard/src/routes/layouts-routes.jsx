import React from "react";

import Home from "../blackice/pages/Home";
import Sensors from "../blackice/pages/Sensors";
import SensorDetail from "../blackice/pages/SensorDetail";
import Events from "../blackice/pages/Events";
import Escalations from "../blackice/pages/Escalations";
import Alarms from "../blackice/pages/Alarms";
import Plugins from "../blackice/pages/Plugins";

export const routes = [
  { path: `/home`, Component: <Home /> },
  { path: `/sensors`, Component: <Sensors /> },
  { path: `/sensors/:id`, Component: <SensorDetail /> },
  { path: `/events`, Component: <Events /> },
  { path: `/escalations`, Component: <Escalations /> },
  { path: `/alarms`, Component: <Alarms /> },
  { path: `/plugins`, Component: <Plugins /> },
];
