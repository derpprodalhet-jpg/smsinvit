import { fileURLToPath } from 'node:url';
import { defineConfig } from 'vite';
export default defineConfig(({ command }) => {
  const configured = process.env.SMSINVIT_ADMIN_PATH || '/admin/';
  if (!/^\/[A-Za-z0-9][A-Za-z0-9_-]{0,63}\/?$/.test(configured)) {
    throw new Error('Invalid SMSINVIT_ADMIN_PATH');
  }
  const base = `${configured.replace(/\/$/, '')}/`;
  return {
    resolve: {
      alias: {
        '@smsinvit/errors': fileURLToPath(
          new URL('../../packages/errors/src/index.ts', import.meta.url),
        ),
      },
    },
    base: command === 'serve' ? base : './',
    plugins: [
      {
        name: 'admin-development-base',
        apply: 'serve',
        transformIndexHtml() {
          return [
            { tag: 'base', attrs: { href: base, 'data-admin-base': '' }, injectTo: 'head-prepend' },
          ];
        },
      },
    ],
    esbuild: { jsx: 'automatic' },
    server: { port: 3001, proxy: { '/api': 'http://127.0.0.1:3003' } },
    build: {
      outDir: 'dist',
      rollupOptions: {
        output: {
          onlyExplicitManualChunks: true,
          manualChunks(id) {
            if (id.includes('/@ant-design/pro-') || id.includes('/@ant-design+pro-'))
              return 'pro-components';
          },
        },
        onwarn(warning, defaultHandler) {
          if (warning.code === 'MODULE_LEVEL_DIRECTIVE' && warning.message.includes('use client'))
            return;
          defaultHandler(warning);
        },
      },
    },
  };
});
