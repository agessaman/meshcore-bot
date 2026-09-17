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
