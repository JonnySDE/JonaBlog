import { defineConfig } from 'astro/config';

import tailwindcss from '@tailwindcss/vite';

// Sitio 100% estático: Vercel solo sirve archivos HTML ya generados.
export default defineConfig({
  output: 'static',
  trailingSlash: 'always',

  vite: {
    plugins: [tailwindcss()],
  },
});