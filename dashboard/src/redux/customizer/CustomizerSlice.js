import { createSlice } from "@reduxjs/toolkit";
import ConfigDB from "../../data/customizer/config";

const initialState = {
  customizer: ConfigDB.data,
  configData: {},
  layout: "ltr",
  settings: "",
  sidebar_types: {},
  color: {},
  mix_layout: "",
  animation: "",
};

const customizerSlice = createSlice({
  name: "customizer",
  initialState,
  reducers: {
    ADD_COSTOMIZER: (state) => {
      state.customizer = ConfigDB.data;
    },
    ADD_LAYOUT: (state, action) => {
      state.customizer.settings.layout_type = action.payload;
      state.layout = action.payload;
    },
    ADD_SIDEBAR_TYPES: (state, action) => {
      state.customizer.settings.sidebar = action.payload;
      state.sidebar_types = action.payload;
    },
    ADD_COLOR: (state, action) => {
      // Guard: on a first visit the template dispatches this before
      // localStorage is seeded, and a null payload used to blank the whole app.
      const { primary_color, secondary_color, color, layout_version } =
        action.payload || {};
      state.customizer.color.primary_color = primary_color;
      state.customizer.color.secondary_color = secondary_color;
      state.customizer.color.color = color;
      state.customizer.color.layout_version = layout_version;
      state.color = action.payload;
    },
    ADD_MIXlAYOUT: (state, action) => {
      state.customizer.color.mix_layout = action.payload;
      state.mix_layout = action.payload;
    },
    ROUTER_ANIMATION: (state, action) => {
      state.customizer.router_animation = action.payload;
      state.animation = action.payload;
    },
  },
});

export const { ADD_COSTOMIZER, ADD_LAYOUT, ADD_SIDEBAR_TYPES, ADD_COLOR, ADD_MIXlAYOUT, ROUTER_ANIMATION } = customizerSlice.actions;

export default customizerSlice.reducer;
