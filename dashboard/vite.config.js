import { defineConfig } from 'vite'
import react from '@vitejs/plugin-react'

// https://vite.dev/config/
export default defineConfig({
  plugins: [react()],
  // Relative asset paths — the production build is loaded via file://
  // in Electron, where absolute paths (the Vite default) resolve
  // against the filesystem root instead of the HTML file and 404.
  base: "./",
})
