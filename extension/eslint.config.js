import js from '@eslint/js';
import globals from 'globals';
import tseslint from 'typescript-eslint';

// CONTRIBUTING.md security rules: the extension must never read Instagram cookies or
// network traffic. Enforced here so a violation fails CI instead of review.
const forbiddenApis = [
  { object: 'document', property: 'cookie', message: 'Reading cookies is forbidden (CONTRIBUTING.md).' },
  { object: 'chrome', property: 'cookies', message: 'chrome.cookies is forbidden (CONTRIBUTING.md).' },
  {
    object: 'chrome',
    property: 'webRequest',
    message: 'chrome.webRequest is forbidden: no network interception (CONTRIBUTING.md).',
  },
  {
    object: 'chrome',
    property: 'debugger',
    message: 'chrome.debugger is forbidden: no network interception (CONTRIBUTING.md).',
  },
];

export default tseslint.config(
  { ignores: ['dist/', 'node_modules/'] },
  js.configs.recommended,
  ...tseslint.configs.strict,
  {
    languageOptions: { globals: { ...globals.browser, chrome: 'readonly' } },
    rules: {
      'no-restricted-properties': ['error', ...forbiddenApis],
    },
  },
  {
    files: ['*.config.js', '*.config.ts'],
    languageOptions: { globals: globals.node },
  },
  {
    // The performance benchmark runs in Node and sends functions into the browser page it drives.
    files: ['bench/*.mjs'],
    languageOptions: { globals: { ...globals.node, ...globals.browser } },
  },
);
