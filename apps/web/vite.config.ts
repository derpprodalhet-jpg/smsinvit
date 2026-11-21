import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
export default defineConfig({
  resolve: {
    alias: {
      '@smsinvit/errors': fileURLToPath(
        new URL('../../packages/errors/src/index.ts', import.meta.url),
      ),
    },
  },
  esbuild: { jsx: 'automatic' },
  server: { port: 3000, proxy: { '/api': 'http://127.0.0.1:3003' } },
  build: {
    outDir: 'dist',
    rollupOptions: {
      onwarn(warning, defaultHandler) {
        if (warning.code === 'MODULE_LEVEL_DIRECTIVE' && warning.message.includes('use client'))
          return;
        defaultHandler(warning);
      },
    },
  },
});
