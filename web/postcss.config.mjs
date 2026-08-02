// Named rather than an anonymous default export, so the module has a stable identity in
// tooling/stack traces (import/no-anonymous-default-export).
const config = {
  plugins: {
    "@tailwindcss/postcss": {},
  },
};

export default config;
