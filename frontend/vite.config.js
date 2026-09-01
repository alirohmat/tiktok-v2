import { defineConfig } from 'vite';
import { svelte } from '@sveltejs/vite-plugin-svelte';

export default defineConfig({
  plugins: [svelte()],
  server: { proxy: { '/api': 'http://localhost:8000', '/clip': 'http://localhost:8000', '/health': 'http://localhost:8000', '/renders': 'http://localhost:8000', '/jobs': 'http://localhost:8000' } },
  build: { outDir: 'dist', emptyOutDir: true }
});
