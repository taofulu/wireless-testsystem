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

type MappingState = 'running' | 'succeeded' | 'failed'
type StepMappingStatus = 'mapped' | 'unmapped' | 'manual'

interface MappingError {
  code: string
  detail: string | null
}

interface Reclassification {
  seq: number
  code: string
  detail: string | null
}

interface MappingJob {
  state: MappingState
  error: MappingError | null
  step_count: number | null
  mapped_count: number | null
  unmapped_count: number | null
  reclassifications: Reclassification[]
}

interface StructuredStep {
  id: number
  seq: number
  action_text: string
  aw_operation_id: number | null
  params: Record<string, unknown>
  assertion_text: string
  mapping_status: StepMappingStatus
}

interface TextCase {
  id: number
  title: string
  precondition: string
  steps_text: string
  expected_text: string
  status: string
  elaboration: Elaboration | null
  mapping: MappingJob | null
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
  mapped: '已映射',
}

const ERROR_LABELS: Record<string, string> = {
  timeout: '扩写评估超时',
  cli_failed: 'GLM CLI 执行失败',
  bad_result: 'GLM 输出不符合约定',
}

const MAPPING_ERROR_LABELS: Record<string, string> = {
  timeout: '映射执行超时',
  cli_failed: 'GLM CLI 执行失败',
  bad_result: 'GLM 输出不符合约定（或步骤序列非法）',
  interrupted: '服务重启导致作业中断，请重试',
  internal_error: '映射服务内部错误，请重试',
}

const RECLASSIFY_LABELS: Record<string, string> = {
  missing_operation: '缺少操作目录引用',
  unknown_operation: '引用的操作不存在',
  bad_mml_params: 'MML 参数形状不合法',
  dictionary_missing: '命令字典未导入',
  unknown_command: '命令不在命令字典中',
  bad_type: '参数类型与字典不符',
  out_of_range: '参数超出字典允许范围',
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
  const [steps, setSteps] = useState<StructuredStep[]>([])
  const [error, setError] = useState<string | null>(null)

  const editingId = selected?.id ?? null
  const qa = selected?.elaboration ?? null
  const running = qa?.state === 'running'
  const mapping = selected?.mapping ?? null
  const mappingRunning = mapping?.state === 'running'
  // 映射闸门：扩写评估通过或强制跳过后才允许触发（服务端同样强校验）
  const mappingGatePassed = qa?.state === 'sufficient' || qa?.state === 'skipped'

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

  const loadSteps = useCallback((id: number) => {
    fetch(`/api/text-cases/${id}/steps`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: StructuredStep[]) => setSteps(data))
      .catch(() => {
        /* 步骤拉取失败保留下一次轮询/重试机会，不弹全局错误 */
      })
  }, [])

  // 映射作业轮询：只取轻量作业视图；到终态后拉步骤并刷新列表（状态变为已映射）
  const pollMapping = useCallback(
    (id: number) => {
      fetch(`/api/text-cases/${id}/mapping`)
        .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
        .then((data: MappingJob) => {
          setSelected((prev) =>
            prev && prev.id === id ? { ...prev, mapping: data } : prev,
          )
          if (data.state === 'succeeded') {
            loadSteps(id)
            refreshList()
          }
        })
        .catch(() => {
          /* 单次轮询失败静默，下一拍重试 */
        })
    },
    [loadSteps, refreshList],
  )

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

  // 映射作业异步执行：running 时每秒轮询直到出结论
  useEffect(() => {
    if (editingId === null || !mappingRunning) return
    const timer = setInterval(() => pollMapping(editingId), 1000)
    return () => clearInterval(timer)
  }, [editingId, mappingRunning, pollMapping])

  // 切用例或作业进入成功态时装载结构化步骤；从未触发过映射则清空
  useEffect(() => {
    if (editingId === null) {
      setSteps([])
      return
    }
    if (mapping?.state === 'succeeded') {
      loadSteps(editingId)
    } else if (mapping === null) {
      setSteps([])
    }
  }, [editingId, mapping?.state, loadSteps])

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
      return (await r.json()) as TextCase | Elaboration | MappingJob
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

  async function startMapping() {
    const data = await postAction('map')
    if (data && selected) {
      // 202 返回的是映射作业视图（含 state，与扩写视图形状不同，但路径固定）
      setSelected({ ...selected, mapping: data as MappingJob })
      setSteps([])
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

  function renderMappingPanel() {
    if (!selected) return null

    // 还没碰过扩写流程且没有历史作业：不占用界面（纯草稿阶段）
    if (!qa && !mapping) return null

    if (!mappingGatePassed && !mapping) {
      return (
        <div style={{ ...PANEL_STYLE, borderLeftColor: '#94a3b8', opacity: 0.85 }}>
          <strong>结构化映射</strong>
          <p style={{ margin: '0.5rem 0', color: '#555' }}>
            扩写评估通过（或强制跳过）后，可一键把文本步骤映射为操作目录中的结构化步骤。
          </p>
        </div>
      )
    }

    if (mapping?.state === 'running') {
      return (
        <div style={PANEL_STYLE}>
          <strong>结构化映射进行中…</strong>
          <p style={{ margin: '0.5rem 0', color: '#666' }}>
            GLM 正在按操作目录与命令字典映射，服务端会对映射结果做字典二次校验，状态自动刷新。
          </p>
        </div>
      )
    }

    if (mapping?.state === 'failed') {
      return (
        <div style={{ ...PANEL_STYLE, borderColor: '#dc2626' }}>
          <strong style={{ color: '#b91c1c' }}>
            {MAPPING_ERROR_LABELS[mapping.error?.code ?? ''] ?? '结构化映射失败'}
          </strong>
          {mapping.error?.detail && (
            <pre
              style={{
                whiteSpace: 'pre-wrap',
                wordBreak: 'break-all',
                background: '#fef2f2',
                padding: '0.5rem',
                fontSize: '0.85rem',
              }}
            >
              {mapping.error.detail}
            </pre>
          )}
          <button onClick={startMapping} disabled={busy || !mappingGatePassed}>
            {busy ? '提交中…' : '重试映射'}
          </button>
        </div>
      )
    }

    if (mapping?.state === 'succeeded') {
      const unmapped = mapping.unmapped_count ?? 0
      return (
        <div style={{ ...PANEL_STYLE, borderLeftColor: unmapped > 0 ? '#d97706' : '#16a34a' }}>
          <strong>结构化映射结果</strong>
          <p style={{ margin: '0.5rem 0' }}>
            共 {mapping.step_count ?? 0} 步：
            <span style={{ color: '#15803d', fontWeight: 600 }}>
              {' '}
              已映射 {mapping.mapped_count ?? 0}
            </span>
            <span style={{ color: unmapped > 0 ? '#b91c1c' : '#15803d', fontWeight: 600 }}>
              {' '}
              · 未映射 {unmapped}
            </span>
            {unmapped > 0 && (
              <span style={{ color: '#b91c1c' }}>（以下红底步骤待人工确认，映射不升级）</span>
            )}
          </p>

          {mapping.reclassifications.length > 0 && (
            <div style={{ marginBottom: '0.75rem' }}>
              <strong style={{ fontSize: '0.85rem', color: '#b45309' }}>
                服务端字典对账降级留痕（LLM 自报合法但被服务端拦截）：
              </strong>
              <ul style={{ margin: '0.35rem 0', paddingLeft: '1.25rem', fontSize: '0.85rem' }}>
                {mapping.reclassifications.map((item) => (
                  <li key={`${item.seq}-${item.code}`} style={{ color: '#92400e' }}>
                    步骤 {item.seq}：{RECLASSIFY_LABELS[item.code] ?? item.code}
                    {item.detail ? `（${item.detail}）` : ''}
                  </li>
                ))}
              </ul>
            </div>
          )}

          <ol style={{ margin: 0, paddingLeft: 0, listStyle: 'none' }}>
            {steps.map((step) => {
              const isUnmapped = step.mapping_status === 'unmapped'
              return (
                <li
                  key={step.id}
                  style={{
                    border: '1px solid',
                    borderColor: isUnmapped ? '#fca5a5' : '#cbd5e1',
                    borderLeft: '4px solid',
                    borderLeftColor: isUnmapped ? '#dc2626' : '#16a34a',
                    background: isUnmapped ? '#fef2f2' : '#fff',
                    borderRadius: '4px',
                    padding: '0.5rem 0.75rem',
                    marginBottom: '0.5rem',
                  }}
                >
                  <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
                    <strong>#{step.seq}</strong>
                    <span
                      style={{
                        fontSize: '0.75rem',
                        borderRadius: '4px',
                        padding: '0 0.4rem',
                        color: isUnmapped ? '#b91c1c' : '#15803d',
                        background: isUnmapped ? '#fee2e2' : '#dcfce7',
                        fontWeight: 600,
                      }}
                    >
                      {isUnmapped ? '未映射 · 待确认' : '已映射'}
                    </span>
                    {!isUnmapped && step.aw_operation_id !== null && (
                      <span style={{ fontSize: '0.8rem', color: '#666' }}>
                        操作目录 ID：{step.aw_operation_id}
                      </span>
                    )}
                  </div>
                  <div style={{ marginTop: '0.25rem' }}>{step.action_text}</div>
                  <div style={{ fontSize: '0.85rem', color: '#444', marginTop: '0.25rem' }}>
                    参数：
                    {Object.keys(step.params).length > 0 ? (
                      <code style={{ fontSize: '0.8rem' }}>{JSON.stringify(step.params)}</code>
                    ) : (
                      <span>无</span>
                    )}
                  </div>
                  {step.assertion_text && (
                    <div style={{ fontSize: '0.85rem', color: '#444', marginTop: '0.25rem' }}>
                      断言：{step.assertion_text}
                    </div>
                  )}
                </li>
              )
            })}
          </ol>

          {mappingGatePassed && (
            <button onClick={startMapping} disabled={busy} style={{ marginTop: '0.25rem' }}>
              {busy ? '提交中…' : '重新映射'}
            </button>
          )}
        </div>
      )
    }

    // 闸门通过且从未触发
    return (
      <div style={{ ...PANEL_STYLE, borderLeftColor: '#7c3aed' }}>
        <strong>结构化映射</strong>
        <p style={{ margin: '0.5rem 0', color: '#444' }}>
          由 GLM 按操作目录（含设备目标/操作类型）与命令字典映射为结构化步骤；
          mml_generic 命令落库前经服务端命令字典二次校验，未命中或参数非法的步骤会标为未映射。
        </p>
        <button onClick={startMapping} disabled={busy}>
          {busy ? '提交中…' : '一键映射为结构化步骤'}
        </button>
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
          {selected && <div style={{ marginTop: '1rem' }}>{renderMappingPanel()}</div>}
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
