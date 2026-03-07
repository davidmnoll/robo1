import { defineConfig } from 'vite';
import { resolve } from 'node:path';

const rootDir = resolve(__dirname, 'public');
const outDir = resolve(__dirname, 'dist');

export default defineConfig({
  root: rootDir,
  publicDir: false,
  build: {
    outDir,
    emptyOutDir: true,
  },
  server: {
    host: '0.0.0.0',
    port: Number(process.env.WEB_PORT || 4173),
  },
});
