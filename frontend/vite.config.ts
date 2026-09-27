import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// 开发期把 /api/* 代理到后端；前端统一请求 /api/health 等路径
export default defineConfig({
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      '/api': {
        target: 'http://localhost:8000',
        changeOrigin: true,
        rewrite: (path) => path.replace(/^\/api/, ''),
      },
    },
  },
})
