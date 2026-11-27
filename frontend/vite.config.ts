import react from '@vitejs/plugin-react'
import { defineConfig } from 'vite'

const API = process.env.QWEN_DESKTOP_API ?? 'http://localhost:8300'

export default defineConfig({
  plugins: [react()],
  server: {
    host: true,
    port: 5173,
    proxy: { '/api': { target: API, changeOrigin: true } },
  },
  build: {
    target: 'es2022',
    chunkSizeWarningLimit: 1500,
    rollupOptions: {
      output: {
        // Split heavy, rarely-changing libraries so app updates don't bust their cache.
        manualChunks(id) {
          if (!id.includes('node_modules')) return
          if (/katex/.test(id)) return 'katex'
          if (/highlight\.js|lowlight|rehype-highlight/.test(id)) return 'highlight'
          if (/react-markdown|remark|rehype|micromark|mdast|hast|unified|vfile/.test(id)) return 'markdown'
          if (/react|scheduler|zustand/.test(id)) return 'react'
        },
      },
    },
  },
})
