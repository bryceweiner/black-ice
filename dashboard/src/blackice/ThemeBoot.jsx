// Applies the theme to the DOM on boot. This is all that survives of the
// template's customizer panel: Black Ice ships one palette and one layout, so
// the six-swatch picker, the box/RTL layouts and the sidebar variants were
// choices nobody needed. The light/dark toggle lives in the header.

import React, { useEffect } from "react";
import { useDispatch } from "react-redux";
import ConfigDB from "../data/customizer/config";
import {
  ADD_COLOR,
  ADD_COSTOMIZER,
  ROUTER_ANIMATION,
} from "../redux/customizer/CustomizerSlice";

// The theme is persisted in localStorage, so a browser that ever saw the old
// purple-on-light default would keep it forever. Bumping this rev clears the
// stored theme once, letting config.js win again.
const THEME_REV = "blackice-1";
const KEYS = ["primary_color", "secondary_color", "color", "layout_version", "mix_layout"];

if (localStorage.getItem("theme_rev") !== THEME_REV) {
  KEYS.forEach((k) => localStorage.removeItem(k));
  localStorage.setItem("theme_rev", THEME_REV);
}

export default function ThemeBoot() {
  const dispatch = useDispatch();
  const { color, settings, router_animation } = ConfigDB.data;

  useEffect(() => {
    // Anything the user has chosen wins; otherwise fall back to the shipped
    // config. The template used to write the literal string "null" onto the
    // body when localStorage was empty.
    const version = localStorage.getItem("layout_version") || color.layout_version;
    const scheme = localStorage.getItem("color") || color.color;
    const primary = localStorage.getItem("primary_color") || color.primary_color;
    const secondary = localStorage.getItem("secondary_color") || color.secondary_color;
    const animation = localStorage.getItem("animation") || router_animation;

    localStorage.setItem("layout_version", version);
    localStorage.setItem("color", scheme);
    localStorage.setItem("primary_color", primary);
    localStorage.setItem("secondary_color", secondary);

    document.body.setAttribute("main-theme-layout", settings.layout_type);
    document.documentElement.dir = settings.layout_type;
    document.documentElement.className = scheme;
    document.body.className = version;

    const wrapper = document.querySelector(".page-wrapper");
    if (wrapper) wrapper.className = `page-wrapper ${settings.sidebar.type}`;

    dispatch(ADD_COSTOMIZER());
    dispatch(ADD_COLOR({ color: scheme, primary_color: primary,
      secondary_color: secondary, layout_version: version }));
    dispatch(ROUTER_ANIMATION(animation));
  }, [dispatch, color, settings, router_animation]);

  return null;
}
