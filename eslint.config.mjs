import html from "eslint-plugin-html";
import globals from "globals";

export default [
  {
    files: ["**/*.{html,js}"],
    plugins: { html },
    languageOptions: {
      ecmaVersion: 2021,
      sourceType: "script",
      globals: {
        ...globals.browser,
        io: "readonly",
        bootstrap: "readonly",
        Chart: "readonly",
        L: "readonly",
        escapeHtml: "writable",
        socket: "writable",
        // Declared in base.html; page scripts that extend it read it.
        IS_ADMIN: "readonly",
      },
    },
    settings: {
      "html/html-extensions": [".html"],
    },
    rules: {
      "no-undef": "warn",
      "no-unused-vars": "warn",
      "no-console": "off",
      semi: ["warn", "always"],
      eqeqeq: ["warn", "always"],
      "no-eval": "error",
      "no-implied-eval": "error",
    },
  },
];
