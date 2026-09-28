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

type OperationKind = 'mml_family' | 'mml_generic' | 'long_running' | 'instrument_primitive' | 'composite'
type Simulatable = 'schema_stub' | 'declarative' | 'python' | 'none'

interface Operation {
  id: number
  name: string
  description: string
  kind: OperationKind
  device_target: string
  params_schema: Record<string, unknown>
  simulatable: Simulatable
  sim_ref: string | null
  sim_package_version: string | null
  suboperations: string[] | null
}

interface Scenario {
  id: number
  scenario_id: string
  version: string
  name: string
  template_type: string | null
  has_meta: boolean
}

interface ExecutableCase {
  id: number
  text_case_id: number
  version: number
  created_at: string
}

interface ExecutableCode extends ExecutableCase {
  code: string
}

interface ExecutionResult {
  task_id: number
  verdict: string
  logs: string
  step_results: unknown[]
  artifacts: { name: string; kind: string; uri: string; checksum: string }[]
  allure_report: Record<string, unknown> | null
  sim_package_version: string | null
  created_at: string
}

interface ExecutionRecord {
  id: number
  executable_case_id: number
  execution_target: string
  status: string
  worker_id: string | null
  env_check_result: string | null
  env_check_detail: Record<string, unknown> | null
  created_at: string
  finished_at: string | null
  result: ExecutionResult | null
}

interface StepDraft {
  id: number
  seq: number
  action_text: string
  aw_operation_id: number | null
  params: Record<string, unknown>
  assertion_text: string
  mapping_status: StepMappingStatus
  opChanged: boolean
  paramsJson: string
}

interface TextCase {
  id: number
  title: string
  precondition: string
  steps_text: string
  expected_text: string
  status: string
  required_topology: Record<string, unknown> | null
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
  mapped: '已映射·待确认',
  confirmed: '已确认',
  generated: '已生成',
  queued: '已入队',
  running: '执行中',
  done: '已完成',
}

const VERDICT_LABELS: Record<string, string> = {
  passed: '通过',
  failed: '失败',
  env_failed: '环境失败',
  inconclusive: '不可判定',
}

const VERDICT_COLORS: Record<string, string> = {
  passed: '#15803d',
  failed: '#b91c1c',
  env_failed: '#b45309',
  inconclusive: '#b45309',
}

const ENV_CHECK_LABELS: Record<string, string> = {
  ready: '环境就绪',
  needs_create: '需新建资源',
  needs_modify: '需调整环境',
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

const KIND_LABELS: Record<OperationKind, string> = {
  mml_family: '高频 MML 命令族',
  mml_generic: '通用 MML',
  long_running: '长时操作',
  instrument_primitive: '仪表原语',
  composite: '组合操作',
}

const SIM_LABELS: Record<Simulatable, string> = {
  schema_stub: '仅桩校验',
  declarative: '声明式仿真',
  python: 'Python 仿真',
  none: '未仿真',
}

const SIM_COLORS: Record<Simulatable, string> = {
  schema_stub: '#b45309',
  declarative: '#15803d',
  python: '#15803d',
  none: '#b91c1c',
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
  const [stepDrafts, setStepDrafts] = useState<StepDraft[]>([])
  const [operations, setOperations] = useState<Operation[]>([])
  const [scenarios, setScenarios] = useState<Scenario[]>([])
  const [savingSteps, setSavingSteps] = useState(false)
  const [confirming, setConfirming] = useState(false)
  const [execCases, setExecCases] = useState<ExecutableCase[]>([])
  const [execCode, setExecCode] = useState<ExecutableCode | null>(null)
  const [generating, setGenerating] = useState(false)
  const [topologyJson, setTopologyJson] = useState('')
  const [executions, setExecutions] = useState<ExecutionRecord[]>([])
  const [executing, setExecuting] = useState(false)
  const [executeConfirm, setExecuteConfirm] = useState<{ uncovered: number[] } | null>(null)
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
    setTopologyJson(data.required_topology ? JSON.stringify(data.required_topology, null, 2) : '')
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

  const loadOperations = useCallback(() => {
    fetch('/api/operations')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: Operation[]) => setOperations(data))
      .catch(() => setOperations([]))
  }, [])

  const loadScenarios = useCallback(() => {
    fetch('/api/scenarios')
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: Scenario[]) => setScenarios(data))
      .catch(() => setScenarios([]))
  }, [])

  // 可执行用例版本历史（新版本在前）；渲染失败/网络失败静默，保留旧列表
  const loadExecCases = useCallback((id: number) => {
    fetch(`/api/text-cases/${id}/executable-cases`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: ExecutableCase[]) => setExecCases(data))
      .catch(() => setExecCases([]))
  }, [])

  function buildDrafts(list: StructuredStep[]): StepDraft[] {
    return list.map((s) => ({
      id: s.id,
      seq: s.seq,
      action_text: s.action_text,
      aw_operation_id: s.aw_operation_id,
      params: s.params,
      assertion_text: s.assertion_text,
      mapping_status: s.mapping_status,
      opChanged: false,
      paramsJson: JSON.stringify(s.params, null, 2),
    }))
  }

  function opById(id: number | null): Operation | undefined {
    if (id === null) return undefined
    return operations.find((o) => o.id === id)
  }

  function updateDraft(stepId: number, patch: Partial<StepDraft>) {
    setStepDrafts((prev) => prev.map((d) => (d.id === stepId ? { ...d, ...patch } : d)))
  }

  function handleStepOpChange(stepId: number, opIdStr: string) {
    const opId = opIdStr === '' ? null : Number(opIdStr)
    const op = opById(opId)
    let params: Record<string, unknown> = {}
    let paramsJson = '{}'
    if (op?.name === 'play_scenario') {
      params = { scenario_id: '', scenario_version: '' }
      paramsJson = JSON.stringify(params, null, 2)
    }
    updateDraft(stepId, {
      aw_operation_id: opId,
      params,
      paramsJson,
      opChanged: true,
    })
  }

  function handleStepParamsJson(stepId: number, json: string) {
    updateDraft(stepId, { paramsJson: json })
    try {
      const parsed = JSON.parse(json)
      if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
        updateDraft(stepId, { params: parsed as Record<string, unknown> })
      }
    } catch {
      /* 非法 JSON 暂不更新 params，保存时后端会校验 */
    }
  }

  function handleScenarioField(stepId: number, field: 'scenario_id' | 'scenario_version', value: string) {
    setStepDrafts((prev) =>
      prev.map((d) => {
        if (d.id !== stepId) return d
        const params = { ...d.params, [field]: value }
        // 仅编辑场景参数不改变操作引用，不触发 manual 状态迁移
        return { ...d, params, paramsJson: JSON.stringify(params, null, 2) }
      }),
    )
  }

  async function saveStepEdits() {
    if (editingId === null) return
    setSavingSteps(true)
    setError(null)
    const patches = stepDrafts
      .filter((d) => d.opChanged || JSON.stringify(d.params) !== JSON.stringify(steps.find((s) => s.id === d.id)?.params ?? {}))
      .map((d) => {
        const patch: { id: number; aw_operation_id?: number | null; params?: Record<string, unknown> } = { id: d.id }
        if (d.opChanged) patch.aw_operation_id = d.aw_operation_id
        patch.params = d.params
        return patch
      })
    try {
      const r = await fetch(`/api/text-cases/${editingId}/steps`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ steps: patches }),
      })
      if (!r.ok) {
        const detail = (await r.json().catch(() => ({}))) as { detail?: unknown }
        throw new Error(typeof detail.detail === 'string' ? detail.detail : `HTTP ${r.status}`)
      }
      const updated = (await r.json()) as StructuredStep[]
      setSteps(updated)
      setStepDrafts(buildDrafts(updated))
    } catch (e) {
      setError(`保存步骤失败：${(e as Error).message}`)
    } finally {
      setSavingSteps(false)
    }
  }

  async function confirmCase() {
    if (editingId === null) return
    setConfirming(true)
    setError(null)
    try {
      const r = await fetch(`/api/text-cases/${editingId}/confirm`, { method: 'POST' })
      if (!r.ok) {
        const detail = (await r.json().catch(() => ({}))) as { detail?: string }
        throw new Error(detail.detail ?? `HTTP ${r.status}`)
      }
      refreshSelected(editingId)
      refreshList()
    } catch (e) {
      setError(`确认失败：${(e as Error).message}`)
    } finally {
      setConfirming(false)
    }
  }

  async function generateCase() {
    if (editingId === null) return
    setGenerating(true)
    setError(null)
    try {
      const r = await fetch(`/api/text-cases/${editingId}/generate`, { method: 'POST' })
      if (!r.ok) {
        const detail = (await r.json().catch(() => ({}))) as { detail?: unknown }
        const msg =
          typeof detail.detail === 'object' && detail.detail !== null
            ? `渲染失败：${(detail.detail as { detail?: string }).detail ?? ''}`
            : typeof detail.detail === 'string'
              ? detail.detail
              : `HTTP ${r.status}`
        throw new Error(msg)
      }
      loadExecCases(editingId)
      refreshSelected(editingId)
      refreshList()
    } catch (e) {
      setError(`生成失败：${(e as Error).message}`)
    } finally {
      setGenerating(false)
    }
  }

  async function viewCode(execId: number) {
    setError(null)
    try {
      const r = await fetch(`/api/executable-cases/${execId}/code`)
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setExecCode((await r.json()) as ExecutableCode)
    } catch (e) {
      setError(`读取代码失败：${(e as Error).message}`)
    }
  }

  // 真实执行历史（T12 结果页数据源）：只含 real 任务，新的在前
  const loadExecutions = useCallback((execId: number) => {
    fetch(`/api/executable-cases/${execId}/executions`)
      .then((r) => (r.ok ? r.json() : Promise.reject(new Error(`HTTP ${r.status}`))))
      .then((data: ExecutionRecord[]) => setExecutions(data))
      .catch(() => {
        /* 单次拉取失败保留旧列表，轮询/刷新时重试 */
      })
  }, [])

  async function executeReal(confirmInconclusive: boolean) {
    const execId = execCases[0]?.id
    if (execId === undefined) return
    setExecuting(true)
    setError(null)
    try {
      const r = await fetch(`/api/executable-cases/${execId}/execute`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ confirm_inconclusive: confirmInconclusive }),
      })
      if (r.status === 409) {
        const detail = (await r.json().catch(() => ({}))) as {
          detail?: { code?: string; detail?: string; uncovered_steps?: number[] }
        }
        const code = detail.detail?.code
        if (code === 'confirmation_required') {
          // 沙盒判决不可判定：呈现未覆盖步骤清单，由工程师二次确认（故事 44）
          setExecuteConfirm({ uncovered: detail.detail?.uncovered_steps ?? [] })
        } else {
          setError(detail.detail?.detail ?? `当前状态不允许提交真实执行（${code ?? '409'}）`)
        }
        return
      }
      if (r.status === 503) {
        const detail = (await r.json().catch(() => ({}))) as { detail?: string }
        setError(`环境中台不可达：${detail.detail ?? 'HTTP 503'}（校验未发生，未放行）`)
        return
      }
      if (!r.ok) throw new Error(`HTTP ${r.status}`)
      setExecuteConfirm(null)
      if (editingId !== null) refreshSelected(editingId)
      refreshList()
      loadExecutions(execId)
    } catch (e) {
      setError(`提交真实执行失败：${(e as Error).message}`)
    } finally {
      setExecuting(false)
    }
  }

  async function recheckEnv() {
    const execId = execCases[0]?.id
    if (execId === undefined) return
    setExecuting(true)
    setError(null)
    try {
      const r = await fetch(`/api/executable-cases/${execId}/recheck`, { method: 'POST' })
      if (!r.ok) {
        const detail = (await r.json().catch(() => ({}))) as {
          detail?: { detail?: string } | string
        }
        const msg =
          typeof detail.detail === 'object' && detail.detail !== null
            ? detail.detail.detail
            : typeof detail.detail === 'string'
              ? detail.detail
              : `HTTP ${r.status}`
        throw new Error(msg)
      }
      if (editingId !== null) refreshSelected(editingId)
      refreshList()
      loadExecutions(execId)
    } catch (e) {
      setError(`环境复检失败：${(e as Error).message}`)
    } finally {
      setExecuting(false)
    }
  }

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
      setStepDrafts([])
      return
    }
    if (mapping?.state === 'succeeded') {
      loadSteps(editingId)
    } else if (mapping === null) {
      setSteps([])
      setStepDrafts([])
    }
  }, [editingId, mapping?.state, loadSteps])

  // 步骤变化时重建草稿；进入 mapped/confirmed 态时加载操作目录与场景索引
  useEffect(() => {
    if (steps.length > 0) {
      setStepDrafts(buildDrafts(steps))
    }
  }, [steps])

  useEffect(() => {
    if (selected && (selected.status === 'mapped' || selected.status === 'confirmed')) {
      loadOperations()
      loadScenarios()
    }
  }, [selected?.status, loadOperations, loadScenarios])

  // 进入 confirmed 及之后的状态装载版本历史（执行面板在 queued/running/done
  // 也依赖 execCases）；离开时清空代码视图
  useEffect(() => {
    if (editingId === null) {
      setExecCases([])
      setExecCode(null)
      return
    }
    if (
      selected &&
      ['confirmed', 'generated', 'queued', 'running', 'done'].includes(selected.status)
    ) {
      loadExecCases(editingId)
    } else {
      setExecCases([])
      setExecCode(null)
    }
  }, [editingId, selected?.status, loadExecCases])

  // 执行域（T11/T12）：进入执行相关状态时装载执行历史；执行中每 2s 轮询
  const latestExecId = execCases[0]?.id ?? null
  useEffect(() => {
    if (latestExecId === null || !selected) {
      setExecutions([])
      return
    }
    if (!['generated', 'queued', 'running', 'done'].includes(selected.status)) {
      setExecutions([])
      return
    }
    loadExecutions(latestExecId)
    if (selected.status !== 'queued' && selected.status !== 'running') return
    const timer = setInterval(() => {
      loadExecutions(latestExecId)
      refreshSelected(selected.id)
    }, 2000)
    return () => clearInterval(timer)
  }, [latestExecId, selected?.status, loadExecutions, refreshSelected])

  function startNew() {
    setSelected(null)
    setForm(EMPTY_FORM)
    setTopologyJson('')
    setExecutions([])
    setExecuteConfirm(null)
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
    let requiredTopology: Record<string, unknown> | null = null
    const topoText = topologyJson.trim()
    if (topoText !== '') {
      try {
        const parsed: unknown = JSON.parse(topoText)
        if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) {
          throw new Error('需为 JSON 对象')
        }
        requiredTopology = parsed as Record<string, unknown>
      } catch (e) {
        setError(`所需拓扑不是合法 JSON：${(e as Error).message}`)
        setSaving(false)
        return
      }
    }
    const payload = { ...form, required_topology: requiredTopology }
    const request =
      editingId === null
        ? fetch('/api/text-cases', {
            method: 'POST',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
          })
        : fetch(`/api/text-cases/${editingId}`, {
            method: 'PATCH',
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify(payload),
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

  function renderConfirmationPanel() {
    if (!selected) return null
    if (selected.status !== 'mapped') return null

    const allMapped = stepDrafts.every(
      (d) => d.mapping_status !== 'unmapped' && d.aw_operation_id !== null,
    )
    const mappedCount = stepDrafts.filter((d) => d.mapping_status === 'mapped').length
    const manualCount = stepDrafts.filter((d) => d.mapping_status === 'manual').length
    const unmappedCount = stepDrafts.filter((d) => d.mapping_status === 'unmapped').length

    // 按 kind 分组操作目录供下拉
    const groupedOps: Record<string, Operation[]> = {}
    for (const op of operations) {
      const key = `${op.kind}|${op.device_target}`
      ;(groupedOps[key] ||= []).push(op)
    }
    const groupKeys = Object.keys(groupedOps).sort()

    return (
      <div style={{ ...PANEL_STYLE, borderLeftColor: '#7c3aed' }}>
        <strong>确认态：人工审核结构化步骤</strong>
        <p style={{ margin: '0.5rem 0', color: '#444' }}>
          核对每步的 AW 操作与参数：未映射步骤（红底）从操作目录下拉手选，可编辑任意参数；
          场景类操作选择场景 ID+版本；全部步骤映射后才可确认。
        </p>
        <p style={{ margin: '0.25rem 0', fontSize: '0.9rem' }}>
          共 {stepDrafts.length} 步：
          <span style={{ color: '#15803d' }}> 已映射 {mappedCount}</span>
          <span style={{ color: '#7c3aed' }}> · 手选 {manualCount}</span>
          <span style={{ color: unmappedCount > 0 ? '#b91c1c' : '#15803d' }}>
            {' '}
            · 未映射 {unmappedCount}
          </span>
        </p>

        <ol style={{ margin: 0, paddingLeft: 0, listStyle: 'none' }}>
          {stepDrafts.map((d) => {
            const op = opById(d.aw_operation_id)
            const isUnmapped = d.mapping_status === 'unmapped'
            const isScenario = op?.name === 'play_scenario'
            const sim = op?.simulatable
            // 场景类操作的仿真可用性还取决于场景 has_meta
            const scenarioMeta = isScenario
              ? scenarios.find(
                  (s) =>
                    s.scenario_id === (d.params.scenario_id as string) &&
                    s.version === (d.params.scenario_version as string),
                )?.has_meta
              : undefined
            const realEnvOnly = isScenario && sim === 'declarative' && scenarioMeta === false

            return (
              <li
                key={d.id}
                style={{
                  border: '1px solid',
                  borderColor: isUnmapped ? '#fca5a5' : '#cbd5e1',
                  borderLeft: '4px solid',
                  borderLeftColor: isUnmapped ? '#dc2626' : '#7c3aed',
                  background: isUnmapped ? '#fef2f2' : '#fff',
                  borderRadius: '4px',
                  padding: '0.5rem 0.75rem',
                  marginBottom: '0.5rem',
                }}
              >
                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
                  <strong>#{d.seq}</strong>
                  <span
                    style={{
                      fontSize: '0.75rem',
                      borderRadius: '4px',
                      padding: '0 0.4rem',
                      color: isUnmapped ? '#b91c1c' : d.mapping_status === 'manual' ? '#7c3aed' : '#15803d',
                      background: isUnmapped ? '#fee2e2' : d.mapping_status === 'manual' ? '#ede9fe' : '#dcfce7',
                      fontWeight: 600,
                    }}
                  >
                    {isUnmapped ? '未映射' : d.mapping_status === 'manual' ? '手选' : '已映射'}
                  </span>
                  {sim && (
                    <span
                      style={{
                        fontSize: '0.7rem',
                        borderRadius: '4px',
                        padding: '0 0.4rem',
                        color: SIM_COLORS[sim],
                        background: sim === 'none' ? '#fee2e2' : '#f1f5f9',
                        border: '1px solid #e2e8f0',
                      }}
                      title={op ? `操作 ${op.name} 的仿真供给声明` : ''}
                    >
                      仿真：{SIM_LABELS[sim]}
                    </span>
                  )}
                  {realEnvOnly && (
                    <span
                      style={{
                        fontSize: '0.7rem',
                        borderRadius: '4px',
                        padding: '0 0.4rem',
                        color: '#b91c1c',
                        background: '#fee2e2',
                        fontWeight: 600,
                      }}
                    >
                      真实环境专用
                    </span>
                  )}
                </div>
                <div style={{ marginTop: '0.25rem' }}>{d.action_text}</div>

                <div style={{ marginTop: '0.4rem', display: 'flex', gap: '0.5rem', alignItems: 'flex-start' }}>
                  <label style={{ fontSize: '0.8rem', color: '#555', flexShrink: 0 }}>
                    操作：
                  </label>
                  <select
                    value={d.aw_operation_id ?? ''}
                    onChange={(e) => handleStepOpChange(d.id, e.target.value)}
                    style={{ fontSize: '0.85rem', padding: '0.2rem' }}
                  >
                    <option value="">（未选择）</option>
                    {groupKeys.map((key) => {
                      const [kind, target] = key.split('|')
                      return (
                        <optgroup key={key} label={`${KIND_LABELS[kind as OperationKind]} · ${target}`}>
                          {groupedOps[key].map((o) => (
                            <option key={o.id} value={o.id}>
                              {o.name}
                            </option>
                          ))}
                        </optgroup>
                      )
                    })}
                  </select>
                  {op && (
                    <span style={{ fontSize: '0.75rem', color: '#888' }}>
                      {KIND_LABELS[op.kind]} · {op.device_target}
                    </span>
                  )}
                </div>

                <div style={{ marginTop: '0.35rem' }}>
                  {isScenario ? (
                    <div style={{ display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
                      <label style={{ fontSize: '0.8rem', color: '#555' }}>
                        场景 ID：
                        <input
                          value={(d.params.scenario_id as string) ?? ''}
                          onChange={(e) => handleScenarioField(d.id, 'scenario_id', e.target.value)}
                          style={{ fontSize: '0.85rem', padding: '0.2rem', marginLeft: '0.25rem' }}
                        />
                      </label>
                      <label style={{ fontSize: '0.8rem', color: '#555' }}>
                        版本：
                        <input
                          value={(d.params.scenario_version as string) ?? ''}
                          onChange={(e) => handleScenarioField(d.id, 'scenario_version', e.target.value)}
                          style={{ fontSize: '0.85rem', padding: '0.2rem', marginLeft: '0.25rem' }}
                        />
                      </label>
                      {scenarios.length > 0 && (
                        <select
                          value=""
                          onChange={(e) => {
                            const sc = scenarios.find((s) => s.id === Number(e.target.value))
                            if (sc) {
                              handleScenarioField(d.id, 'scenario_id', sc.scenario_id)
                              handleScenarioField(d.id, 'scenario_version', sc.version)
                            }
                          }}
                          style={{ fontSize: '0.8rem' }}
                        >
                          <option value="">从场景索引选择…</option>
                          {scenarios.map((s) => (
                            <option key={s.id} value={s.id}>
                              {s.name}（{s.scenario_id}@{s.version}）
                            </option>
                          ))}
                        </select>
                      )}
                      {scenarios.length === 0 && (
                        <span style={{ fontSize: '0.75rem', color: '#b45309' }}>
                          场景索引为空，可手工录入 ID+版本
                        </span>
                      )}
                    </div>
                  ) : (
                    <div>
                      <span style={{ fontSize: '0.8rem', color: '#555' }}>参数（JSON）：</span>
                      <textarea
                        value={d.paramsJson}
                        onChange={(e) => handleStepParamsJson(d.id, e.target.value)}
                        style={{
                          width: '100%',
                          minHeight: '3rem',
                          fontFamily: 'monospace',
                          fontSize: '0.8rem',
                          padding: '0.3rem',
                          marginTop: '0.2rem',
                          boxSizing: 'border-box',
                        }}
                      />
                    </div>
                  )}
                </div>

                {d.assertion_text && (
                  <div style={{ fontSize: '0.8rem', color: '#444', marginTop: '0.25rem' }}>
                    断言：{d.assertion_text}
                  </div>
                )}
              </li>
            )
          })}
        </ol>

        <div style={{ display: 'flex', gap: '0.75rem', marginTop: '0.5rem' }}>
          <button onClick={saveStepEdits} disabled={savingSteps}>
            {savingSteps ? '保存中…' : '保存步骤修改'}
          </button>
          <button
            onClick={confirmCase}
            disabled={confirming || !allMapped}
            title={!allMapped ? '存在未映射步骤，需先为所有步骤选择操作' : ''}
          >
            {confirming ? '确认中…' : '确认全部步骤并进入下一阶段'}
          </button>
        </div>
        {!allMapped && (
          <p style={{ color: '#b91c1c', fontSize: '0.8rem', margin: '0.4rem 0 0' }}>
            存在未映射步骤，请先为所有步骤选择操作后再确认。
          </p>
        )}
      </div>
    )
  }

  function renderGenerationPanel() {
    if (!selected) return null
    if (selected.status !== 'confirmed' && selected.status !== 'generated') return null

    return (
      <div style={{ ...PANEL_STYLE, borderLeftColor: '#0e7490' }}>
        <strong>可执行用例：模板渲染（只读）</strong>
        {selected.status === 'confirmed' && (
          <p style={{ margin: '0.5rem 0', color: '#444' }}>
            确认后的结构化步骤将由模板渲染为带 Allure 步骤标记的 pytest 代码（ADR-0001：
            LLM 不直接产出代码）；每次生成追加新版本，历史版本保留可回溯。
          </p>
        )}
        {selected.status === 'generated' && (
          <p style={{ margin: '0.5rem 0', color: '#444' }}>
            代码 100% 由模板渲染产出，全文只读——如需修改请回上游改文本/映射后重新生成新版本。
          </p>
        )}

        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
          <button onClick={generateCase} disabled={generating}>
            {generating ? '生成中…' : execCases.length > 0 ? '重新生成新版本' : '生成可执行用例'}
          </button>
          {execCases.length > 0 && (
            <span style={{ fontSize: '0.85rem', color: '#666' }}>
              共 {execCases.length} 个版本
            </span>
          )}
        </div>

        {execCases.length > 0 && (
          <ul style={{ listStyle: 'none', padding: 0, margin: '0.75rem 0 0' }}>
            {execCases.map((ec) => (
              <li
                key={ec.id}
                style={{
                  display: 'flex',
                  gap: '0.75rem',
                  alignItems: 'center',
                  padding: '0.35rem 0.5rem',
                  borderBottom: '1px solid #e2e8f0',
                  background: execCode?.id === ec.id ? '#ecfeff' : 'transparent',
                }}
              >
                <strong style={{ flexShrink: 0 }}>版本 {ec.version}</strong>
                <span style={{ fontSize: '0.8rem', color: '#666', flex: 1 }}>
                  {new Date(ec.created_at).toLocaleString()}
                </span>
                <button onClick={() => viewCode(ec.id)} style={{ fontSize: '0.8rem' }}>
                  {execCode?.id === ec.id ? '查看中' : '查看代码'}
                </button>
              </li>
            ))}
          </ul>
        )}

        {execCode && (
          <div style={{ marginTop: '0.75rem' }}>
            <div style={{ fontSize: '0.8rem', color: '#0e7490', marginBottom: '0.25rem' }}>
              版本 {execCode.version} 代码全文（只读，系统不提供在线编辑入口）
            </div>
            <pre
              style={{
                background: '#0f172a',
                color: '#e2e8f0',
                padding: '0.75rem',
                borderRadius: '6px',
                fontSize: '0.78rem',
                lineHeight: 1.5,
                overflow: 'auto',
                maxHeight: '32rem',
                userSelect: 'text',
              }}
            >
              <code>{execCode.code}</code>
            </pre>
          </div>
        )}
      </div>
    )
  }

  function renderExecutionPanel() {
    if (!selected) return null
    if (!['generated', 'queued', 'running', 'done'].includes(selected.status)) return null
    if (execCases.length === 0) return null

    const latest = executions[0] ?? null
    const envBlocked =
      selected.status === 'done' &&
      latest !== null &&
      (latest.env_check_result === 'needs_create' || latest.env_check_result === 'needs_modify')

    return (
      <div style={{ ...PANEL_STYLE, borderLeftColor: '#7c3aed' }}>
        <strong>真实执行</strong>

        {selected.status === 'generated' && (
          <div style={{ marginTop: '0.5rem' }}>
            <p style={{ margin: '0 0 0.5rem', color: '#444' }}>
              提交前依次过两道闸门：沙盒判决（ADR-0009）→ 环境校验（LASS
              三值：就绪入队 / 需新建 / 需调整则阻断）。
            </p>
            <button onClick={() => executeReal(false)} disabled={executing}>
              {executing ? '提交中…' : '提交真实执行'}
            </button>
          </div>
        )}

        {executeConfirm && (
          <div
            style={{
              marginTop: '0.75rem',
              padding: '0.5rem 0.75rem',
              background: '#fef3c7',
              border: '1px solid #f59e0b',
              borderRadius: '4px',
            }}
          >
            <strong>沙盒判决不可判定</strong>
            <p style={{ margin: '0.25rem 0', color: '#78350f' }}>
              未被仿真覆盖的步骤：
              {executeConfirm.uncovered.length > 0
                ? executeConfirm.uncovered.map((s) => `步骤${s}`).join('、')
                : '（全部步骤尚无调试结论）'}
              。提交真实执行需二次确认。
            </p>
            <div style={{ display: 'flex', gap: '0.5rem' }}>
              <button onClick={() => executeReal(true)} disabled={executing}>
                已知悉风险，确认提交
              </button>
              <button onClick={() => setExecuteConfirm(null)} disabled={executing}>
                取消
              </button>
            </div>
          </div>
        )}

        {(selected.status === 'queued' || selected.status === 'running') && (
          <p style={{ margin: '0.5rem 0 0', color: '#444' }}>
            {selected.status === 'queued' ? '已入队，等待 real Worker 领取…' : 'real Worker 执行中…'}
          </p>
        )}

        {envBlocked && latest && (
          <div
            style={{
              marginTop: '0.75rem',
              padding: '0.5rem 0.75rem',
              background: '#fff7ed',
              border: '1px solid #ea580c',
              borderRadius: '4px',
            }}
          >
            <strong>环境校验未通过：{ENV_CHECK_LABELS[latest.env_check_result ?? ''] ?? latest.env_check_result}</strong>
            <p style={{ margin: '0.25rem 0', color: '#7c2d12' }}>
              {latest.env_check_result === 'needs_create'
                ? '环境中台需新建以下资源：'
                : '环境与所需拓扑存在以下差异：'}
            </p>
            <pre
              style={{
                background: '#fff',
                padding: '0.5rem',
                borderRadius: '4px',
                fontSize: '0.78rem',
                overflow: 'auto',
                margin: '0 0 0.5rem',
              }}
            >
              {JSON.stringify(latest.env_check_detail, null, 2)}
            </pre>
            <p style={{ margin: '0 0 0.5rem', color: '#7c2d12' }}>
              下一步：请环境中台完成新建/调整后，点击「环境复检」重新校验入队。
            </p>
            <button onClick={recheckEnv} disabled={executing}>
              {executing ? '复检中…' : '环境复检'}
            </button>
          </div>
        )}

        {executions.length > 0 && (
          <div style={{ marginTop: '0.75rem' }}>
            <strong style={{ fontSize: '0.9rem' }}>执行历史（最新在前）</strong>
            <ul style={{ listStyle: 'none', padding: 0, margin: '0.5rem 0 0' }}>
              {executions.map((rec) => (
                <li
                  key={rec.id}
                  style={{
                    padding: '0.5rem',
                    borderBottom: '1px solid #e2e8f0',
                    background: '#fff',
                  }}
                >
                  <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center' }}>
                    <span style={{ fontSize: '0.8rem', color: '#666' }}>
                      {new Date(rec.created_at).toLocaleString()}
                    </span>
                    <span style={{ fontSize: '0.8rem', color: '#666' }}>
                      {rec.worker_id ? `Worker: ${rec.worker_id}` : rec.status === 'queued' ? '待领取' : ''}
                    </span>
                    {rec.result && (
                      <span
                        style={{
                          fontWeight: 600,
                          color: VERDICT_COLORS[rec.result.verdict] ?? '#334155',
                        }}
                      >
                        {VERDICT_LABELS[rec.result.verdict] ?? rec.result.verdict}
                      </span>
                    )}
                    {!rec.result && rec.env_check_result && (
                      <span style={{ color: '#b45309', fontWeight: 600 }}>
                        {ENV_CHECK_LABELS[rec.env_check_result] ?? rec.env_check_result}
                      </span>
                    )}
                  </div>
                  {rec.result && rec.result.artifacts.length > 0 && (
                    <table
                      style={{
                        marginTop: '0.4rem',
                        borderCollapse: 'collapse',
                        fontSize: '0.78rem',
                        width: '100%',
                      }}
                    >
                      <thead>
                        <tr style={{ textAlign: 'left', color: '#666' }}>
                          <th style={{ padding: '0.15rem 0.4rem' }}>制品</th>
                          <th style={{ padding: '0.15rem 0.4rem' }}>类型</th>
                          <th style={{ padding: '0.15rem 0.4rem' }}>路径（testbed 侧）</th>
                          <th style={{ padding: '0.15rem 0.4rem' }}>校验和</th>
                        </tr>
                      </thead>
                      <tbody>
                        {rec.result.artifacts.map((a) => (
                          <tr key={`${a.name}-${a.checksum}`}>
                            <td style={{ padding: '0.15rem 0.4rem' }}>{a.name}</td>
                            <td style={{ padding: '0.15rem 0.4rem' }}>{a.kind}</td>
                            <td style={{ padding: '0.15rem 0.4rem', fontFamily: 'monospace' }}>
                              {a.uri}
                            </td>
                            <td style={{ padding: '0.15rem 0.4rem', fontFamily: 'monospace' }}>
                              {a.checksum}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  )}
                  {rec.result && rec.result.logs && (
                    <details style={{ marginTop: '0.3rem' }}>
                      <summary style={{ fontSize: '0.78rem', color: '#666', cursor: 'pointer' }}>
                        执行日志
                      </summary>
                      <pre
                        style={{
                          background: '#0f172a',
                          color: '#e2e8f0',
                          padding: '0.5rem',
                          borderRadius: '4px',
                          fontSize: '0.75rem',
                          overflow: 'auto',
                          maxHeight: '16rem',
                        }}
                      >
                        {rec.result.logs}
                      </pre>
                    </details>
                  )}
                </li>
              ))}
            </ul>
          </div>
        )}
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
          <label style={{ display: 'block', marginTop: '1rem' }}>
            所需拓扑（JSON，可选；提交真实执行时由环境中台校验，留空表示无环境要求）
            <textarea
              value={topologyJson}
              onChange={(e) => setTopologyJson(e.target.value)}
              placeholder='{"bbu": 1, "ue": 2, "instrument": ["rf-power-meter"]}'
              style={{ ...FIELD_STYLE, minHeight: '3.5rem', fontFamily: 'monospace' }}
            />
          </label>
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
          {selected && <div style={{ marginTop: '1rem' }}>{renderConfirmationPanel()}</div>}
          {selected && <div style={{ marginTop: '1rem' }}>{renderGenerationPanel()}</div>}
          {selected && <div style={{ marginTop: '1rem' }}>{renderExecutionPanel()}</div>}
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
