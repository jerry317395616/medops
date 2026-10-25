import eslint from "@eslint/js";
import prettier from "eslint-config-prettier";
import vue from "eslint-plugin-vue";
import globals from "globals";
import tseslint from "typescript-eslint";

const frappeGlobals = {
	$: "readonly",
	__: "readonly",
	cur_dialog: "readonly",
	cur_frm: "readonly",
	dayjs: "readonly",
	frappe: "readonly",
	jQuery: "readonly",
	moment: "readonly",
};

export default [
	{
		ignores: [
			"**/.venv/**",
			"**/venv/**",
			"**/assets/**",
			"**/build/**",
			"**/dist/**",
			"**/node_modules/**",
			"**/*.bundle.js",
			"**/*.min.js",
		],
	},
	eslint.configs.recommended,
	...tseslint.configs.recommended,
	...vue.configs["flat/recommended"],
	{
		files: ["medops/**/*.{js,ts,vue}"],
		languageOptions: {
			ecmaVersion: "latest",
			globals: {
				...globals.browser,
				...frappeGlobals,
			},
			sourceType: "module",
		},
	},
	{
		files: ["medops/**/*.js"],
		rules: {
			"no-unused-vars": [
				"error",
				{
					argsIgnorePattern: "^_",
					caughtErrorsIgnorePattern: "^_",
					varsIgnorePattern: "^_",
				},
			],
		},
	},
	{
		files: ["medops/**/*.{ts,vue}"],
		rules: {
			"@typescript-eslint/no-unused-vars": [
				"error",
				{
					argsIgnorePattern: "^_",
					caughtErrorsIgnorePattern: "^_",
					varsIgnorePattern: "^_",
				},
			],
			"no-unused-vars": "off",
		},
	},
	{
		files: ["medops/**/*.vue"],
		languageOptions: {
			parserOptions: {
				extraFileExtensions: [".vue"],
				parser: tseslint.parser,
			},
		},
	},
	{
		files: ["*.config.{js,mjs,cjs}", "eslint.config.mjs", "scripts/**/*.{js,ts}"],
		languageOptions: {
			globals: globals.node,
		},
	},
	prettier,
];
