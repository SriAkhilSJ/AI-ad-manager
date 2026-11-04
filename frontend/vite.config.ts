import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// The dev server proxies /api to the FastAPI backend so the browser never calls
// localhost directly (required for the sandboxed preview environment).
export default defineConfig({
  plugins: [react()],
  server: {
    host: '0.0.0.0',
    port: 5173,
    strictPort: true,
    // Allow any preview host/origin the platform may front this port with.
    allowedHosts: true as unknown as string[],
    proxy: {
      '/api': {
        target: process.env.VITE_API_PROXY ?? 'http://127.0.0.1:8000',
        changeOrigin: true,
      },
    },
  },
  preview: {
    host: '0.0.0.0',
    port: 4173,
    allowedHosts: true as unknown as string[],
  },
  build: {
    outDir: 'dist',
    sourcemap: false,
  },
})
