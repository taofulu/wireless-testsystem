import { useEffect, useState } from 'react'

type HealthState = 'checking' | 'ok' | 'down'

export function App() {
  const [health, setHealth] = useState<HealthState>('checking')

  useEffect(() => {
    let cancelled = false
    fetch('/api/health')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: { status?: string }) => {
        if (!cancelled) setHealth(data.status === 'ok' ? 'ok' : 'down')
      })
      .catch(() => {
        if (!cancelled) setHealth('down')
      })
    return () => {
      cancelled = true
    }
  }, [])

  return (
    <main style={{ fontFamily: 'system-ui, sans-serif', padding: '2rem' }}>
      <h1>Wireless Test System</h1>
      <p>
        后端状态：
        {health === 'checking' && <span>检查中…</span>}
        {health === 'ok' && <span style={{ color: 'green' }}>● 已连接</span>}
        {health === 'down' && <span style={{ color: 'crimson' }}>● 不可用</span>}
      </p>
    </main>
  )
}
