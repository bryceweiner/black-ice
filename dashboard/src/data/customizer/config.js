// Black Ice ships one look: dark, ice cyan, LTR. The template's customizer can
// still flip it at runtime, but this is what a fresh browser gets.
export class ConfigDB {
  static data = {
    settings: {
      layout_type: "ltr",
      sidebar: {
        type: "default",
      },
    },
    color: {
      layout_version: "dark-only",
      color: "color-1",
      primary_color: "#38bdf8",
      secondary_color: "#f59e0b",
      mix_layout: "dark-only",
    },

    router_animation: "fade",
  };
}

export default ConfigDB;
