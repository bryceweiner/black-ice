import { configureStore } from "@reduxjs/toolkit";
import customizerReducer from "./customizer/CustomizerSlice";

export const store = configureStore({
  reducer: {
    customizerSlice: customizerReducer,
  },
});