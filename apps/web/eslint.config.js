import eslint from "@eslint/js";
import globals from "globals";
import reactHooks from "eslint-plugin-react-hooks";
import reactRefresh from "eslint-plugin-react-refresh";
import tseslint from "typescript-eslint";

export default tseslint.config(
  { ignores: ["dist", "playwright-report", "test-results"] },
  eslint.configs.recommended,
  ...tseslint.configs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2022,
      globals: globals.browser,
    },
    plugins: {
      "react-hooks": reactHooks,
      "react-refresh": reactRefresh,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "react-refresh/only-export-components": ["warn", { allowConstantExport: true }],
    },
  },
  {
    // "There is exactly one definition of the wire shapes and it is FastAPI's"
    // (11-frontend-and-demo.md § Generated API types). A hand-maintained `interface` mirroring
    // a generated schema type is a second source of truth that agrees with the first only
    // until someone changes the server. `type` aliases over `schema.d.ts` remain the way to
    // reference those shapes; this only forbids redeclaring one.
    files: ["src/**/*.{ts,tsx}"],
    ignores: ["src/api/schema.d.ts"],
    rules: {
      "no-restricted-syntax": [
        "error",
        {
          selector: "TSInterfaceDeclaration",
          message:
            "Do not declare interfaces under src/ — every wire shape is a `type` alias over generated schema.d.ts types (see api/types.ts, api/private.ts, api/shareable.ts).",
        },
      ],
    },
  },
  {
    // The private/shareable type boundary (11-frontend-and-demo.md § Exactly three surfaces,
    // "Case + Action"): a shareable-zone component may never import a private wire type, so
    // it can never receive one to begin with — the boundary lives in the type system, not in
    // a runtime filter a component author has to remember to write.
    files: ["src/components/shareable/**/*.{ts,tsx}"],
    rules: {
      "no-restricted-imports": [
        "error",
        {
          patterns: [
            {
              group: ["**/api/private", "**/api/private.ts"],
              message:
                "components/shareable/** may not import api/private — see the private/shareable type boundary in 11-frontend-and-demo.md.",
            },
          ],
        },
      ],
    },
  },
);
