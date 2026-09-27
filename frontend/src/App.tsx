import { useCallback, useEffect, useState } from 'react'

type HealthState = 'checking' | 'ok' | 'down'

interface TextCase {
  id: number
  title: string
  precondition: string
  steps_text: string
  expected_text: string
  status: string
  created_at: string
}

/** 三栏录入表单：新建与继续编辑共用此形状 */
type TextCaseForm = Pick<TextCase, 'title' | 'precondition' | 'steps_text' | 'expected_text'>

const EMPTY_FORM: TextCaseForm = { title: '', precondition: '', steps_text: '', expected_text: '' }

const FIELD_STYLE: React.CSSProperties = {
  width: '100%',
  minHeight: '8rem',
  fontFamily: 'inherit',
  fontSize: '0.95rem',
  padding: '0.5rem',
  boxSizing: 'border-box',
}

function ColumnField({
  label,
  value,
  onChange,
}: {
  label: string
  value: string
  onChange: (next: string) => void
}) {
  return (
    <label style={{ display: 'block' }}>
      {label}
      <textarea value={value} onChange={(e) => onChange(e.target.value)} style={FIELD_STYLE} />
    </label>
  )
}

export function App() {
  const [health, setHealth] = useState<HealthState>('checking')
  const [cases, setCases] = useState<TextCase[]>([])
  const [editingId, setEditingId] = useState<number | null>(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refreshList = useCallback(() => {
    fetch('/api/text-cases?status=draft')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: TextCase[]) => setCases(data))
      .catch((e) => setError(`加载列表失败：${e.message}`))
  }, [])

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

  useEffect(() => {
    refreshList()
  }, [refreshList])

  function startNew() {
    setEditingId(null)
    setForm(EMPTY_FORM)
    setError(null)
  }

  function openCase(id: number) {
    fetch(`/api/text-cases/${id}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: TextCase) => {
        setEditingId(data.id)
        setForm({
          title: data.title,
          precondition: data.precondition,
          steps_text: data.steps_text,
          expected_text: data.expected_text,
        })
        setError(null)
      })
      .catch((e) => setError(`打开用例失败：${e.message}`))
  }

  function save() {
    setSaving(true)
    setError(null)
    const request =
      editingId === null
        ? fetch('/api/text-cases', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(form),
          })
        : fetch(`/api/text-cases/${editingId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(form),
          })
    request
      .then(async (r) => {
        if (!r.ok) throw new Error(`HTTP ${r.status}`)
        const data: TextCase = await r.json()
        setEditingId(data.id)
        refreshList()
      })
      .catch((e) => setError(`保存失败：${e.message}`))
      .finally(() => setSaving(false))
  }

  return (
    <main style={{ fontFamily: 'system-ui, sans-serif', padding: '2rem', maxWidth: '72rem' }}>
      <h1>Wireless Test System</h1>
      <p>
        后端状态：
        {health === 'checking' && <span>检查中…</span>}
        {health === 'ok' && <span style={{ color: 'green' }}>● 已连接</span>}
        {health === 'down' && <span style={{ color: 'crimson' }}>● 不可用</span>}
      </p>

      <section style={{ display: 'flex', gap: '2rem', alignItems: 'flex-start' }}>
        <aside style={{ width: '16rem', flexShrink: 0 }}>
          <h2>草稿列表</h2>
          <button onClick={startNew}>新建文本用例</button>
          <ul style={{ listStyle: 'none', padding: 0, marginTop: '1rem' }}>
            {cases.map((c) => (
              <li key={c.id} style={{ marginBottom: '0.5rem' }}>
                <button
                  onClick={() => openCase(c.id)}
                  style={{
                    width: '100%',
                    textAlign: 'left',
                    padding: '0.5rem',
                    border: c.id === editingId ? '2px solid #2563eb' : '1px solid #ccc',
                    borderRadius: '4px',
                    background: c.id === editingId ? '#eff6ff' : '#fff',
                    cursor: 'pointer',
                  }}
                >
                  <div style={{ fontWeight: 600 }}>{c.title}</div>
                  <div style={{ fontSize: '0.8rem', color: '#666' }}>
                    {c.status} · {new Date(c.created_at).toLocaleString()}
                  </div>
                </button>
              </li>
            ))}
            {cases.length === 0 && <li style={{ color: '#666' }}>暂无草稿</li>}
          </ul>
        </aside>

        <section style={{ flex: 1 }}>
          <h2>{editingId === null ? '新建文本用例' : `编辑文本用例 #${editingId}`}</h2>
          <label style={{ display: 'block', marginBottom: '1rem' }}>
            标题
            <input
              value={form.title}
              onChange={(e) => setForm({ ...form, title: e.target.value })}
              style={{ width: '100%', padding: '0.5rem', fontSize: '1rem', boxSizing: 'border-box' }}
            />
          </label>
          <div style={{ display: 'grid', gridTemplateColumns: '1fr 1fr 1fr', gap: '1rem' }}>
            <ColumnField
              label="预知条件"
              value={form.precondition}
              onChange={(v) => setForm({ ...form, precondition: v })}
            />
            <ColumnField
              label="测试步骤"
              value={form.steps_text}
              onChange={(v) => setForm({ ...form, steps_text: v })}
            />
            <ColumnField
              label="预期结果"
              value={form.expected_text}
              onChange={(v) => setForm({ ...form, expected_text: v })}
            />
          </div>
          <div style={{ marginTop: '1rem', display: 'flex', gap: '1rem', alignItems: 'center' }}>
            <button onClick={save} disabled={saving || form.title.trim() === ''}>
              {saving ? '保存中…' : editingId === null ? '保存草稿' : '保存修改'}
            </button>
            {error && <span style={{ color: 'crimson' }}>{error}</span>}
          </div>
        </section>
      </section>
    </main>
  )
}
