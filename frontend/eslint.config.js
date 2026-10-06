import js from "@eslint/js";
import globals from "globals";
import tseslint from "typescript-eslint";
import reactHooks from "eslint-plugin-react-hooks";
import jsxA11y from "eslint-plugin-jsx-a11y";
import prettierConfig from "eslint-config-prettier";

export default tseslint.config(
  { ignores: ["dist", "coverage", "node_modules", "src/contracts/generated"] },
  js.configs.recommended,
  ...tseslint.configs.recommendedTypeChecked,
  jsxA11y.flatConfigs.recommended,
  {
    files: ["**/*.{ts,tsx}"],
    languageOptions: {
      ecmaVersion: 2023,
      globals: globals.browser,
      parserOptions: {
        project: ["./tsconfig.app.json", "./tsconfig.node.json"],
        tsconfigRootDir: import.meta.dirname,
      },
    },
    plugins: {
      "react-hooks": reactHooks,
    },
    rules: {
      ...reactHooks.configs.recommended.rules,
      "@typescript-eslint/no-unused-vars": ["warn", { argsIgnorePattern: "^_" }],
    },
  },
  {
    files: ["**/*.config.{js,ts}", "scripts/**/*.mjs"],
    languageOptions: { globals: globals.node },
    extends: [tseslint.configs.disableTypeChecked],
  },
  {
    files: ["**/*.test.{ts,tsx}", "src/setupTests.ts"],
    languageOptions: { globals: globals.node },
    rules: {
      "@typescript-eslint/no-unsafe-assignment": "off",
    },
  },
  prettierConfig,
);
