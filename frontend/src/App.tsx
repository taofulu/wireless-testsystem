import { useCallback, useEffect, useState } from 'react'

type HealthState = 'checking' | 'ok' | 'down'

type ElaborationField = 'precondition' | 'steps_text' | 'expected_text'
type ElaborationState =
  | 'running'
  | 'awaiting_answers'
  | 'answered'
  | 'sufficient'
  | 'skipped'
  | 'failed'

interface MissingPoint {
  field: ElaborationField
  question: string
}

interface ElaborationAnswer {
  field: string
  question: string
  answer: string
}

interface ElaborationRound {
  round: number
  missing_points: MissingPoint[]
  answers: ElaborationAnswer[]
}

interface ElaborationError {
  code: string
  detail: string | null
}

interface Elaboration {
  state: ElaborationState
  round: number
  missing_points: MissingPoint[]
  rounds: ElaborationRound[]
  error: ElaborationError | null
}

interface TextCase {
  id: number
  title: string
  precondition: string
  steps_text: string
  expected_text: string
  status: string
  elaboration: Elaboration | null
  created_at: string
}

/** 三栏录入表单：新建与继续编辑共用此形状 */
type TextCaseForm = Pick<TextCase, 'title' | 'precondition' | 'steps_text' | 'expected_text'>

const EMPTY_FORM: TextCaseForm = { title: '', precondition: '', steps_text: '', expected_text: '' }

const FIELD_LABELS: Record<ElaborationField, string> = {
  precondition: '预知条件',
  steps_text: '测试步骤',
  expected_text: '预期结果',
}

const STATUS_LABELS: Record<string, string> = {
  draft: '草稿',
  elaborating: '扩写中',
}

const ERROR_LABELS: Record<string, string> = {
  timeout: '扩写评估超时',
  cli_failed: 'GLM CLI 执行失败',
  bad_result: 'GLM 输出不符合约定',
}

function statusLabel(status: string): string {
  return STATUS_LABELS[status] ?? status
}

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
  const [selected, setSelected] = useState<TextCase | null>(null)
  const [form, setForm] = useState(EMPTY_FORM)
  const [saving, setSaving] = useState(false)
  const [busy, setBusy] = useState(false)
  const [answerDraft, setAnswerDraft] = useState<Record<number, string>>({})
  const [error, setError] = useState<string | null>(null)

  const editingId = selected?.id ?? null
  const qa = selected?.elaboration ?? null
  const running = qa?.state === 'running'

  const syncCase = useCallback((data: TextCase) => {
    setSelected(data)
    setForm({
      title: data.title,
      precondition: data.precondition,
      steps_text: data.steps_text,
      expected_text: data.expected_text,
    })
  }, [])

  const refreshList = useCallback(() => {
    fetch('/api/text-cases')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: TextCase[]) => setCases(data))
      .catch((e) => setError(`加载列表失败：${e.message}`))
  }, [])

  const refreshSelected = useCallback(
    (id: number) => {
      fetch(`/api/text-cases/${id}`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((data: TextCase) => syncCase(data))
        .catch((e) => setError(`刷新用例失败：${e.message}`))
    },
    [syncCase],
  )

  // 作业轮询只取扩写状态（轻量专用资源），避免每秒回灌整个用例文本
  const pollElaboration = useCallback((id: number) => {
    fetch(`/api/text-cases/${id}/elaboration`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: Elaboration) =>
        setSelected((prev) => (prev && prev.id === id ? { ...prev, elaboration: data } : prev)),
      )
      .catch(() => {
        /* 单次轮询失败静默，下一拍重试；终态后定时器自动清除 */
      })
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

  // 扩写作业异步执行：running 时每秒轮询直到出结论
  useEffect(() => {
    if (editingId === null || !running) return
    const timer = setInterval(() => pollElaboration(editingId), 1000)
    return () => clearInterval(timer)
  }, [editingId, running, qa?.round, pollElaboration])

  function startNew() {
    setSelected(null)
    setForm(EMPTY_FORM)
    setAnswerDraft({})
    setError(null)
  }

  function openCase(id: number) {
    fetch(`/api/text-cases/${id}`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: TextCase) => {
        syncCase(data)
        setAnswerDraft({})
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
        syncCase(data)
        refreshList()
      })
      .catch((e) => setError(`保存失败：${e.message}`))
      .finally(() => setSaving(false))
  }

  async function postAction(path: string, options?: { body?: unknown }) {
    if (editingId === null) return
    setBusy(true)
    setError(null)
    try {
      const r = await fetch(`/api/text-cases/${editingId}/${path}`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: options?.body ? JSON.stringify(options.body) : undefined,
      })
      if (r.status === 409) {
        const detail = (await r.json().catch(() => ({}))) as { detail?: string }
        setError(detail.detail ?? `当前状态不允许该操作（HTTP 409）`)
        refreshSelected(editingId)
        return
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      return (await r.json()) as TextCase | Elaboration
    } catch (e) {
      setError(`请求失败：${(e as Error).message}`)
    } finally {
      setBusy(false)
    }
  }

  async function elaborate() {
    const data = await postAction('elaborate')
    if (data && 'state' in data && selected) {
      // 202 返回的是扩写作业视图
      setSelected({ ...selected, elaboration: data as Elaboration })
      setAnswerDraft({})
      refreshList()
    }
  }

  async function skipElaboration() {
    const data = await postAction('elaboration/skip')
    if (data && 'status' in data) {
      syncCase(data as TextCase)
      refreshList()
    }
  }

  async function submitAnswers() {
    if (!qa) return
    const answers = qa.missing_points.map((point, index) => ({
      field: point.field,
      question: point.question,
      answer: answerDraft[index] ?? '',
    }))
    const data = await postAction('elaboration/answers', { body: { answers } })
    if (data && 'status' in data) {
      syncCase(data as TextCase)
      setAnswerDraft({})
      refreshList()
    }
  }

  const pendingPoints = qa?.missing_points ?? []
  const allAnswersFilled =
    pendingPoints.length > 0 &&
    pendingPoints.every((_, index) => (answerDraft[index] ?? '').trim().length > 0)

  function renderElaborationPanel() {
    if (!selected) return null

    if (selected.status === 'draft') {
      return (
        <div style={PANEL_STYLE}>
          <strong>扩写评估</strong>
          <p style={{ margin: '0.5rem 0', color: '#444' }}>
            提交后由 GLM 评估用例充分性并给出针对性追问；紧急场景可强制跳过。
          </p>
          <div style={{ display: 'flex', gap: '0.75rem' }}>
            <button onClick={elaborate} disabled={busy}>
              {busy ? '提交中…' : '开始扩写评估'}
            </button>
            <button onClick={skipElaboration} disabled={busy}>
              强制跳过（接受质量风险）
            </button>
          </div>
        </div>
      )
    }

    if (!qa) return null

    if (qa.state === 'running') {
      return (
        <div style={PANEL_STYLE}>
          <strong>扩写评估进行中…（第 {qa.round} 轮）</strong>
          <p style={{ margin: '0.5rem 0', color: '#666' }}>GLM 正在阅读用例并生成追问，状态自动刷新。</p>
        </div>
      )
    }

    if (qa.state === 'awaiting_answers') {
      return (
        <div style={PANEL_STYLE}>
          <strong>系统追问（第 {qa.round} 轮，请逐条补全）</strong>
          <ol style={{ margin: '0.75rem 0', paddingLeft: '1.25rem' }}>
            {pendingPoints.map((point, index) => (
              <li key={`${point.field}-${index}`} style={{ marginBottom: '0.75rem' }}>
                <div>
                  <span style={FIELD_TAG_STYLE}>{FIELD_LABELS[point.field]}</span>{' '}
                  <span>{point.question}</span>
                </div>
                <textarea
                  value={answerDraft[index] ?? ''}
                  onChange={(e) => setAnswerDraft({ ...answerDraft, [index]: e.target.value })}
                  placeholder="输入补充信息，提交后合并回原文"
                  style={{ ...FIELD_STYLE, minHeight: '4rem', marginTop: '0.35rem' }}
                />
              </li>
            ))}
          </ol>
          <div style={{ display: 'flex', gap: '0.75rem' }}>
            <button onClick={submitAnswers} disabled={busy || !allAnswersFilled}>
              {busy ? '提交中…' : '提交答案并合并到原文'}
            </button>
            <button onClick={skipElaboration} disabled={busy}>
              强制跳过
            </button>
          </div>
        </div>
      )
    }

    if (qa.state === 'answered') {
      // 末轮有答案 → 问答合并；否则是通过/跳过后又编辑了文本，闸门被作废
      const lastRound = qa.rounds.length ? qa.rounds[qa.rounds.length - 1] : undefined
      const lastRoundHasAnswers = (lastRound?.answers.length ?? 0) > 0
      return (
        <div style={PANEL_STYLE}>
          <strong>
            {lastRoundHasAnswers ? '答案已合并为用例新版本' : '文本已修改，扩写结论已失效'}
          </strong>
          <p style={{ margin: '0.5rem 0', color: '#444' }}>
            {lastRoundHasAnswers
              ? `可再次触发评估继续打磨（已进行 ${qa.round} 轮），或强制跳过直接进入映射。`
              : '改动尚未经过充分性评估，请重新触发扩写评估，或强制跳过直接进入映射。'}
          </p>
          <div style={{ display: 'flex', gap: '0.75rem' }}>
            <button onClick={elaborate} disabled={busy}>
              {busy ? '提交中…' : '再次扩写评估'}
            </button>
            <button onClick={skipElaboration} disabled={busy}>
              强制跳过
            </button>
          </div>
        </div>
      )
    }

    if (qa.state === 'sufficient') {
      return (
        <div style={{ ...PANEL_STYLE, borderColor: '#16a34a' }}>
          <strong style={{ color: '#15803d' }}>扩写评估通过：用例信息充分</strong>
          <p style={{ margin: '0.5rem 0', color: '#444' }}>下一步可进入结构化映射。</p>
        </div>
      )
    }

    if (qa.state === 'skipped') {
      return (
        <div style={{ ...PANEL_STYLE, borderColor: '#d97706' }}>
          <strong style={{ color: '#b45309' }}>已强制跳过扩写评估</strong>
          <p style={{ margin: '0.5rem 0', color: '#444' }}>
            可进入结构化映射，请知悉：低质量文本可能产生更多未映射步骤。
          </p>
        </div>
      )
    }

    // failed
    return (
      <div style={{ ...PANEL_STYLE, borderColor: '#dc2626' }}>
        <strong style={{ color: '#b91c1c' }}>
          {ERROR_LABELS[qa.error?.code ?? ''] ?? '扩写评估失败'}
        </strong>
        {qa.error?.detail && (
          <pre
            style={{
              whiteSpace: 'pre-wrap',
              wordBreak: 'break-all',
              background: '#fef2f2',
              padding: '0.5rem',
              fontSize: '0.85rem',
            }}
          >
            {qa.error.detail}
          </pre>
        )}
        <div style={{ display: 'flex', gap: '0.75rem' }}>
          <button onClick={elaborate} disabled={busy}>
            {busy ? '提交中…' : '重试扩写评估'}
          </button>
          <button onClick={skipElaboration} disabled={busy}>
            强制跳过
          </button>
        </div>
      </div>
    )
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
          <h2>用例列表</h2>
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
                    {statusLabel(c.status)} · {new Date(c.created_at).toLocaleString()}
                  </div>
                </button>
              </li>
            ))}
            {cases.length === 0 && <li style={{ color: '#666' }}>暂无用例</li>}
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
            {selected && (
              <span style={{ fontSize: '0.85rem', color: '#666' }}>
                当前状态：{statusLabel(selected.status)}
              </span>
            )}
            {error && <span style={{ color: 'crimson' }}>{error}</span>}
          </div>

          {selected && <div style={{ marginTop: '1rem' }}>{renderElaborationPanel()}</div>}
        </section>
      </section>
    </main>
  )
}

const PANEL_STYLE: React.CSSProperties = {
  border: '1px solid #cbd5e1',
  borderLeft: '4px solid #2563eb',
  borderRadius: '6px',
  padding: '0.9rem 1rem',
  background: '#f8fafc',
}

const FIELD_TAG_STYLE: React.CSSProperties = {
  display: 'inline-block',
  background: '#dbeafe',
  color: '#1d4ed8',
  borderRadius: '4px',
  padding: '0 0.4rem',
  fontSize: '0.8rem',
}
