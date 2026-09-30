import globals from 'globals';

// Rules that catch bugs, not style; intentional unused catch/argument names are ignored.
const bugs = [
  'no-undef', 'no-unreachable', 'no-dupe-keys', 'no-dupe-else-if', 'no-duplicate-case',
  'no-fallthrough', 'no-self-assign', 'no-self-compare', 'no-cond-assign',
  'no-constant-condition', 'getter-return', 'no-unsafe-finally',
  'no-unsafe-optional-chaining', 'no-unsafe-negation', 'valid-typeof', 'use-isnan',
  'no-async-promise-executor', 'no-loss-of-precision', 'no-sparse-arrays',
  'no-import-assign', 'no-const-assign', 'no-func-assign', 'no-global-assign',
];

export default [{
  files: ['**/*.js'],
  languageOptions: {
    ecmaVersion: 'latest',
    sourceType: 'module',
    globals: { ...globals.browser, ...globals.serviceworker },
  },
  rules: {
    ...Object.fromEntries(bugs.map(rule => [rule, 'error'])),
    'no-unused-vars': ['error', { caughtErrors: 'none', args: 'none' }],
  },
}];
