/** @type {import('next').NextConfig} */
const nextConfig = {
  // emit a self-contained server bundle for the container image
  output: "standalone",
  // This monorepo can sit below an unrelated package-lock.json. Pin discovery to
  // web/ so Turbopack does not treat a parent directory as the application root.
  turbopack: { root: process.cwd() },
};

export default nextConfig;
