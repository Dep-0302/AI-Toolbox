import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Activity,
  AlertTriangle,
  ArrowRight,
  Blocks,
  BookOpen,
  Bot,
  Boxes,
  Check,
  ChevronRight,
  CircleAlert,
  CircleHelp,
  Code2,
  Command,
  Copy,
  Database,
  FileText,
  FolderOpen,
  Grid2X2,
  HeartPulse,
  Info,
  Library,
  ListFilter,
  LockKeyhole,
  Menu,
  Package,
  PanelLeftClose,
  Plug,
  RefreshCw,
  Search,
  ShieldCheck,
  Sparkles,
  Star,
  TerminalSquare,
  Wrench,
  X,
} from 'lucide-react'
import {
  FINDING_LABELS,
  HOST_LABELS,
  HOST_ORDER,
  TYPE_LABELS,
  candidateDescriptionState,
  candidateDisplayItems,
  candidateHealthFindings,
  categoriesFor,
  coverageNotice,
  filterAssets,
  formatTimestamp,
  hostStats,
  issueGroups,
  splitAttention,
  countFindings,
  findingNote,
  itemFindings,
  itemLabel,
  observationFacts,
  itemSummary,
  rankedAssets,
  reconcileSelectedItem,
  snapshotAge,
  subcategoriesFor,
} from './lib/toolbox'
import {
  SCENARIO_RELATION_LABELS,
  groupAssetsByScenario,
  scenarioContextFor,
  scenarioGuideFor,
} from './lib/scenarios'
import collectionTaxonomy from '../registry/collection_taxonomy.json'
import ProjectSkillsView from './ProjectSkillsView'

const FAVORITES_KEY = 'ai-toolbox-workbench:favorites:v1'

const NAV_ITEMS = [
  { id: 'overview', label: '总览', icon: Grid2X2 },
  { id: 'skills', label: 'Skill 库', icon: Library },
  { id: 'project-skills', label: '项目 Skill', icon: FolderOpen },
  { id: 'tools', label: 'Plugin 与工具', icon: Plug },
  { id: 'candidates', label: '备选 skill 库', icon: Database },
  { id: 'collections', label: '全部收藏', icon: BookOpen },
  { id: 'health', label: '系统体检', icon: HeartPulse },
]

const PAGE_TITLES = {
  overview: '能力总览',
  skills: 'Skill 库',
  'project-skills': '项目 Skill',
  collections: '全部收藏',
  candidates: '备选 skill 库',
  tools: 'Plugin 与工具',
  health: '系统体检',
}

const TYPE_ICONS = {
  skill: Sparkles,
  plugin: Blocks,
  mcp: Bot,
  cli: TerminalSquare,
  sdk: Code2,
}

const METRICS = [
  { key: 'skill', label: 'Skills', icon: Grid2X2, tone: 'blue' },
  { key: 'tools', label: 'Plugins 与工具', icon: Blocks, tone: 'violet' },
  { key: 'bindings', label: '宿主绑定', icon: Boxes, tone: 'blue' },
  { key: 'findings', label: '体检项', icon: Activity, tone: 'mint' },
]

function loadFavorites() {
  try {
    const parsed = JSON.parse(localStorage.getItem(FAVORITES_KEY) || '[]')
    return new Set(Array.isArray(parsed) ? parsed.filter((value) => typeof value === 'string') : [])
  } catch {
    return new Set()
  }
}

async function parseResponse(response) {
  const payload = await response.json().catch(() => ({}))
  if (!response.ok) {
    const error = new Error(payload.message || payload.error || `HTTP ${response.status}`)
    error.payload = payload
    error.status = response.status
    throw error
  }
  return payload
}

function collectionScanAttemptFromError(error) {
  const attempt = error?.payload?.attempt
  if (!attempt || !Array.isArray(attempt.scan_errors) || !attempt.scan_errors.length) return null
  return {
    ...attempt,
    message: error.payload?.message || '',
  }
}

function useFavorites() {
  const [favorites, setFavorites] = useState(loadFavorites)
  const toggle = useCallback((assetId) => {
    setFavorites((current) => {
      const next = new Set(current)
      if (next.has(assetId)) next.delete(assetId)
      else next.add(assetId)
      localStorage.setItem(FAVORITES_KEY, JSON.stringify([...next]))
      return next
    })
  }, [])
  return [favorites, toggle]
}

export default function App() {
  const [snapshot, setSnapshot] = useState(null)
  const [health, setHealth] = useState(null)
  const [activeView, setActiveView] = useState('overview')
  const [query, setQuery] = useState('')
  const [hostFilter, setHostFilter] = useState('')
  const [categoryFilter, setCategoryFilter] = useState('')
  const [skillSubcategoryFilter, setSkillSubcategoryFilter] = useState('')
  const [localizationFilter, setLocalizationFilter] = useState('')
  const [favoriteOnly, setFavoriteOnly] = useState(false)
  const [multiHost, setMultiHost] = useState(false)
  const [toolType, setToolType] = useState('plugin')
  const [toolSubcategoryFilter, setToolSubcategoryFilter] = useState('')
  const [selectedItem, setSelectedItem] = useState(null)
  const [selectedCollectionEntry, setSelectedCollectionEntry] = useState(null)
  const [showBoundary, setShowBoundary] = useState(false)
  const [mobileMenu, setMobileMenu] = useState(false)
  const [loading, setLoading] = useState(true)
  const [refreshing, setRefreshing] = useState(false)
  const [collectionSnapshot, setCollectionSnapshot] = useState(null)
  const [collectionScanAttempt, setCollectionScanAttempt] = useState(null)
  const [candidateCatalog, setCandidateCatalog] = useState(null)
  const [candidateFocusUid, setCandidateFocusUid] = useState(null)
  const [collectionLoading, setCollectionLoading] = useState(false)
  const [collectionChecking, setCollectionChecking] = useState(false)
  const [collectionRefreshing, setCollectionRefreshing] = useState(false)
  const [projectSkillView, setProjectSkillView] = useState(null)
  const [projectSkillLoading, setProjectSkillLoading] = useState(false)
  const [projectSkillLoaded, setProjectSkillLoaded] = useState(false)
  const [projectSkillRefreshing, setProjectSkillRefreshing] = useState(false)
  const [projectSkillError, setProjectSkillError] = useState('')
  const [temporaryProjectSkill, setTemporaryProjectSkill] = useState(null)
  const [folderDialog, setFolderDialog] = useState(null)
  const [sourceSwitching, setSourceSwitching] = useState(false)
  const [notice, setNotice] = useState(null)
  const [favorites, toggleFavorite] = useFavorites()
  const collectionCheckRequestRef = useRef(0)
  const collectionCheckInFlightRef = useRef(false)
  const projectSkillRequestRef = useRef(0)
  const collectionStartupCheckStartedRef = useRef(false)
  const folderDialogTriggerRef = useRef(null)

  const loadWorkbench = useCallback(async () => {
    setLoading(true)
    setNotice(null)
    try {
      const nextHealth = await parseResponse(await fetch('/api/health', { cache: 'no-store' }))
      setHealth(nextHealth)
      try {
        const nextSnapshot = await parseResponse(
          await fetch('/api/snapshot', { cache: 'no-store' }),
        )
        setSnapshot(nextSnapshot)
      } catch (error) {
        if (error.status === 404) setSnapshot(null)
        else throw error
      }
    } catch (error) {
      setNotice({ tone: 'error', message: `工作台未能读取本地服务：${error.message}` })
    } finally {
      setLoading(false)
    }
  }, [])

  useEffect(() => {
    loadWorkbench()
  }, [loadWorkbench])

  const loadProjectSkills = useCallback(async () => {
    const requestId = projectSkillRequestRef.current + 1
    projectSkillRequestRef.current = requestId
    setProjectSkillLoading(true)
    setProjectSkillError('')
    try {
      const nextView = await parseResponse(
        await fetch('/api/project-skills', { cache: 'no-store' }),
      )
      if (requestId !== projectSkillRequestRef.current) return false
      setProjectSkillView(nextView)
      return nextView
    } catch (error) {
      if (requestId !== projectSkillRequestRef.current) return false
      if (error.status === 404) {
        setProjectSkillView(null)
      } else {
        setProjectSkillError(error.payload?.message || `项目 Skill 观察数据读取失败：${error.message}`)
      }
      return false
    } finally {
      if (requestId === projectSkillRequestRef.current) {
        setProjectSkillLoaded(true)
        setProjectSkillLoading(false)
      }
    }
  }, [])

  useEffect(() => {
    if (activeView !== 'project-skills' || projectSkillLoaded || projectSkillLoading) return
    loadProjectSkills()
  }, [activeView, loadProjectSkills, projectSkillLoaded, projectSkillLoading])

  const fetchCandidateCatalog = useCallback(async () => {
    try {
      return await parseResponse(
        await fetch('/candidates/data.json', { cache: 'no-store' }),
      )
    } catch {
      return null
    }
  }, [])

  const checkCollectionsForUpdates = useCallback(async () => {
    const csrfToken = health?.csrf_token
    if (!csrfToken || collectionCheckInFlightRef.current) return

    const requestId = collectionCheckRequestRef.current + 1
    collectionCheckRequestRef.current = requestId
    collectionCheckInFlightRef.current = true
    setCollectionChecking(true)
    if (!collectionSnapshot) setCollectionLoading(true)

    try {
      const [result, candidates] = await Promise.all([
        fetch('/api/collections/check', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-AI-Toolbox-CSRF': csrfToken,
            },
            body: '{}',
          }).then(parseResponse),
        fetchCandidateCatalog(),
      ])
      if (requestId !== collectionCheckRequestRef.current) return
      setCollectionSnapshot(result.snapshot)
      setCollectionScanAttempt(null)
      setCandidateCatalog(candidates)
      if (result.status === 'refreshed') {
        setNotice({
          tone: 'success',
          message: '检测到收藏目录变化，已自动更新“全部收藏”索引。',
        })
      }
    } catch (error) {
      if (requestId !== collectionCheckRequestRef.current) return
      const failedAttempt = collectionScanAttemptFromError(error)
      if (failedAttempt) setCollectionScanAttempt(failedAttempt)
      try {
        const [fallback, candidates] = await Promise.all([
          fetch('/api/collections', { cache: 'no-store' }).then(parseResponse),
          fetchCandidateCatalog(),
        ])
        if (requestId !== collectionCheckRequestRef.current) return
        setCollectionSnapshot(fallback)
        setCandidateCatalog(candidates)
        setNotice({
          tone: 'info',
          message: error.status === 409
            ? '另一次刷新正在进行，已继续显示上一份有效索引。'
            : '自动检查暂未完成，已继续显示上一份有效索引。',
        })
      } catch (fallbackError) {
        if (requestId !== collectionCheckRequestRef.current) return
        setNotice({
          tone: 'error',
          message: error.payload?.message || `全部收藏自动检查失败：${error.message || fallbackError.message}`,
        })
      }
    } finally {
      if (requestId === collectionCheckRequestRef.current) {
        collectionCheckInFlightRef.current = false
        setCollectionChecking(false)
        setCollectionLoading(false)
      }
    }
  }, [collectionSnapshot, fetchCandidateCatalog, health?.csrf_token])

  useEffect(() => {
    if (!health?.csrf_token || collectionStartupCheckStartedRef.current) return
    collectionStartupCheckStartedRef.current = true
    checkCollectionsForUpdates()
  }, [checkCollectionsForUpdates, health?.csrf_token])

  useEffect(() => {
    if (!notice || notice.tone === 'error') return undefined
    const timer = window.setTimeout(() => setNotice(null), 4200)
    return () => window.clearTimeout(timer)
  }, [notice])

  const refresh = useCallback(async () => {
    if (!health?.csrf_token || refreshing) return
    setRefreshing(true)
    setNotice({ tone: 'info', message: '正在读取白名单配置、入口与缓存元数据并合并重复资产…' })
    let nextSnapshot
    try {
      nextSnapshot = await parseResponse(
        await fetch('/api/refresh', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-AI-Toolbox-CSRF': health.csrf_token,
          },
          body: '{}',
        }),
      )
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error.payload?.message || `本次观察未完成：${error.message}`,
      })
      setRefreshing(false)
      return
    }

    setSnapshot(nextSnapshot)
    setSelectedItem((current) => reconcileSelectedItem(current, nextSnapshot.items))
    setNotice({ tone: 'success', message: '只读观察已完成，新快照已保存到独立目录。' })
    try {
      const nextHealth = await parseResponse(await fetch('/api/health', { cache: 'no-store' }))
      setHealth(nextHealth)
    } catch {
      setNotice({ tone: 'info', message: '只读观察已完成并保存；服务状态回执暂未更新。' })
    }
    setRefreshing(false)
  }, [health?.csrf_token, refreshing])

  const refreshProjectSkills = useCallback(async () => {
    if (!health?.csrf_token || projectSkillRefreshing) return
    setProjectSkillRefreshing(true)
    setProjectSkillError('')
    try {
      const nextSnapshot = await parseResponse(
        await fetch('/api/project-skills/refresh', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-AI-Toolbox-CSRF': health.csrf_token,
          },
          body: '{}',
        }),
      )
      setProjectSkillView((current) => ({
        ...(current || {}),
        snapshot: nextSnapshot,
        last_attempt: null,
        report: { available: false, generation_id: null, content: null },
        integrity: {
          degraded: true,
          errors: ['last_attempt_missing', 'report_missing'],
        },
      }))
      const reloaded = await loadProjectSkills()
      setNotice(reloaded
        ? { tone: 'success', message: '项目 Skill 只读观察已更新；被观察项目未被写入。' }
        : { tone: 'info', message: '新快照已提交，但完整回执暂未重读；当前显示本次机器快照。' })
    } catch (error) {
      const reloaded = await loadProjectSkills()
      const hasCompleteSnapshot = Boolean(reloaded?.snapshot || projectSkillView?.snapshot)
      const hasIncompleteReceipt = Boolean(
        reloaded?.last_attempt && reloaded.last_attempt.scan_status !== 'complete',
      )
      const message = error.status === 409
        ? hasCompleteSnapshot
          ? '另一次项目 Skill 观察正在进行，已继续显示最后完整快照。'
          : '另一次项目 Skill 观察正在进行；当前尚无完整快照。'
        : error.status === 422
          ? hasCompleteSnapshot
            ? hasIncompleteReceipt
              ? '本次观察未完整，已保留上一份完整快照并显示失败回执。'
              : '本次观察未完整，已保留上一份完整快照；失败回执暂未重读。'
            : hasIncompleteReceipt
              ? '本次观察未完整，尚未建立可保留的完整快照；失败回执仍可查看。'
              : '本次观察未完整，尚无完整快照；失败回执暂未重读。'
          : error.payload?.message || `项目 Skill 观察失败：${error.message}`
      setProjectSkillError(message)
    } finally {
      setProjectSkillRefreshing(false)
    }
  }, [health?.csrf_token, loadProjectSkills, projectSkillRefreshing, projectSkillView?.snapshot])

  const refreshCollections = useCallback(async () => {
    if (!health?.csrf_token || collectionRefreshing || collectionChecking || sourceSwitching) return
    setCollectionRefreshing(true)
    setNotice({ tone: 'info', message: '正在只读盘点手工收藏、整库与内部能力…' })
    try {
      const [payload, candidates] = await Promise.all([
        fetch('/api/collections/refresh', {
            method: 'POST',
            headers: {
              'Content-Type': 'application/json',
              'X-AI-Toolbox-CSRF': health.csrf_token,
            },
            body: '{}',
          }).then(parseResponse),
        fetchCandidateCatalog(),
      ])
      setCollectionSnapshot(payload)
      setCollectionScanAttempt(null)
      setCandidateCatalog(candidates)
      setNotice({ tone: 'success', message: '全部收藏索引已更新；原文件与旧备选库均未改动。' })
    } catch (error) {
      const failedAttempt = collectionScanAttemptFromError(error)
      if (failedAttempt) setCollectionScanAttempt(failedAttempt)
      setNotice({
        tone: 'error',
        message: error.payload?.message || `全部收藏索引未完成：${error.message}`,
      })
    } finally {
      setCollectionRefreshing(false)
    }
  }, [collectionChecking, collectionRefreshing, fetchCandidateCatalog, health?.csrf_token, sourceSwitching])

  const sourceSession = health?.source_session || {
    mode: 'default',
    display_path: '',
    source_key: 'default',
  }
  const folderPicker = health?.folder_picker || {
    available: false,
    provider: 'unavailable',
    reason: '本地服务尚未报告系统文件夹选择器状态。',
  }

  const closeFolderDialog = useCallback(() => {
    if (sourceSwitching) return
    setFolderDialog(null)
    window.setTimeout(() => folderDialogTriggerRef.current?.focus?.(), 0)
  }, [sourceSwitching])

  const openFolderDialog = useCallback((context, trigger) => {
    folderDialogTriggerRef.current = trigger || document.activeElement
    setFolderDialog({ context, step: 'explain', error: '' })
  }, [])

  const chooseFolder = useCallback(async () => {
    if (!health?.csrf_token || sourceSwitching || !folderPicker.available) return
    setSourceSwitching(true)
    setFolderDialog((current) => current ? { ...current, step: 'picking', error: '' } : current)
    try {
      const payload = await parseResponse(
        await fetch('/api/folder-picker', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-AI-Toolbox-CSRF': health.csrf_token,
          },
          body: JSON.stringify({
            target: folderDialog?.context === 'project-skills'
              ? 'project-skill-source'
              : 'collection-source',
          }),
        }),
      )
      if (!payload.selected) {
        setFolderDialog(null)
        setNotice({
          tone: 'info',
          message: folderDialog?.context === 'project-skills'
            ? '已取消选择，当前项目与项目 Skill 观察均未改变。'
            : '已取消选择，当前收藏来源和索引均未改变。',
        })
        window.setTimeout(() => folderDialogTriggerRef.current?.focus?.(), 0)
        return
      }
      setFolderDialog((current) => ({
        ...(current || { context: 'collections' }),
        step: 'confirm',
        selectionToken: payload.selection_token,
        displayPath: payload.display_path,
        expiresIn: payload.expires_in,
        error: '',
      }))
    } catch (error) {
      setFolderDialog((current) => current ? {
        ...current,
        error: error.payload?.message || `无法打开系统文件夹选择器：${error.message}`,
      } : current)
    } finally {
      setSourceSwitching(false)
    }
  }, [folderDialog?.context, folderPicker.available, health?.csrf_token, sourceSwitching])

  const confirmFolder = useCallback(async () => {
    if (!health?.csrf_token || sourceSwitching || !folderDialog?.selectionToken) return
    setSourceSwitching(true)
    setFolderDialog((current) => current ? { ...current, error: '' } : current)
    try {
      const projectSkillContext = folderDialog.context === 'project-skills'
      const payload = await parseResponse(
        await fetch(projectSkillContext
          ? '/api/project-skills/folder-selection/confirm'
          : '/api/folder-selection/confirm', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-AI-Toolbox-CSRF': health.csrf_token,
          },
          body: JSON.stringify({ selection_token: folderDialog.selectionToken }),
        }),
      )
      if (projectSkillContext) {
        setTemporaryProjectSkill(payload)
      } else {
        collectionCheckRequestRef.current += 1
        collectionCheckInFlightRef.current = false
        setCollectionSnapshot(payload.collection_snapshot || null)
        setCollectionScanAttempt(null)
        setCandidateCatalog(payload.candidate_catalog || null)
        setSelectedCollectionEntry(null)
        setHealth((current) => ({ ...(current || {}), source_session: payload.source_session }))
      }
      setFolderDialog(null)
      setNotice({
        tone: 'success',
        message: projectSkillContext
          ? '已读取所选文件夹中的项目 Skill；仅在当前页面临时展示，未加入登记或写回项目。'
          : '已为本次本地服务会话切换收藏来源；“全部收藏”与“备选 Skill 库”已使用同一份只读索引。',
      })
      window.setTimeout(() => folderDialogTriggerRef.current?.focus?.(), 0)
    } catch (error) {
      setFolderDialog((current) => current ? {
        ...current,
        error: error.payload?.message || `只读索引未建立：${error.message}`,
      } : current)
    } finally {
      setSourceSwitching(false)
    }
  }, [folderDialog?.context, folderDialog?.selectionToken, health?.csrf_token, sourceSwitching])

  const restoreDefaultSource = useCallback(async () => {
    if (!health?.csrf_token || sourceSwitching || sourceSession.mode === 'default') return
    setSourceSwitching(true)
    try {
      const payload = await parseResponse(
        await fetch('/api/folder-source/restore', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-AI-Toolbox-CSRF': health.csrf_token,
          },
          body: '{}',
        }),
      )
      collectionCheckRequestRef.current += 1
      collectionCheckInFlightRef.current = false
      setCollectionSnapshot(payload.collection_snapshot || null)
      setCollectionScanAttempt(null)
      setCandidateCatalog(payload.candidate_catalog || null)
      setSelectedCollectionEntry(null)
      setHealth((current) => ({ ...(current || {}), source_session: payload.source_session }))
      setNotice({ tone: 'success', message: '已恢复默认收藏来源；临时自选索引已从本次会话中移除。' })
    } catch (error) {
      setNotice({
        tone: 'error',
        message: error.payload?.message || `恢复默认收藏来源失败：${error.message}`,
      })
    } finally {
      setSourceSwitching(false)
    }
  }, [health?.csrf_token, sourceSession.mode, sourceSwitching])

  const items = snapshot?.items || []
  const findings = snapshot?.health_findings || []
  const skillItems = useMemo(() => items.filter((item) => item.type === 'skill'), [items])
  const categories = useMemo(() => categoriesFor(skillItems), [skillItems])
  const skillSubcategories = useMemo(
    () => subcategoriesFor(skillItems, categoryFilter),
    [skillItems, categoryFilter],
  )
  const currentToolItems = useMemo(
    () => items.filter((item) => item.type === toolType),
    [items, toolType],
  )
  const toolSubcategories = useMemo(
    () => subcategoriesFor(currentToolItems),
    [currentToolItems],
  )
  const hosts = useMemo(() => hostStats(snapshot), [snapshot])
  const groups = useMemo(() => issueGroups(findings), [findings])
  const candidateScope = useMemo(() => {
    const candidateSource = typeof candidateCatalog?.source_dir === 'string'
      ? candidateCatalog.source_dir
      : ''
    const collectionSource = typeof collectionSnapshot?.source_root === 'string'
      ? collectionSnapshot.source_root
      : ''
    if (!candidateCatalog || !Array.isArray(candidateCatalog.items) || !candidateSource || !collectionSource) {
      return { status: 'unavailable', candidateSource, collectionSource }
    }
    if (candidateSource !== collectionSource) {
      return { status: 'mismatch', candidateSource, collectionSource }
    }
    return { status: 'ready', candidateSource, collectionSource }
  }, [candidateCatalog, collectionSnapshot?.source_root])
  const candidateFindings = useMemo(
    () => candidateScope.status === 'ready' ? candidateHealthFindings(candidateCatalog) : [],
    [candidateCatalog, candidateScope.status],
  )
  const healthGroups = useMemo(
    () => issueGroups([...findings, ...candidateFindings]),
    [candidateFindings, findings],
  )
  const filteredSkills = useMemo(
    () =>
      filterAssets(items, {
        types: ['skill'],
        query,
        host: hostFilter,
        category: categoryFilter,
        subcategory: skillSubcategoryFilter,
        localization: localizationFilter,
        favoriteOnly,
        multiHost,
        favorites,
      }),
    [
      items,
      query,
      hostFilter,
      categoryFilter,
      skillSubcategoryFilter,
      localizationFilter,
      favoriteOnly,
      multiHost,
      favorites,
    ],
  )
  const filteredTools = useMemo(
    () =>
      filterAssets(items, {
        types: [toolType],
        query,
        host: hostFilter,
        subcategory: toolSubcategoryFilter,
        favoriteOnly,
        multiHost,
        favorites,
      }),
    [items, toolType, query, hostFilter, toolSubcategoryFilter, favoriteOnly, multiHost, favorites],
  )
  const overviewItems = useMemo(
    () =>
      rankedAssets(
        filterAssets(items, { query, host: hostFilter, favorites }),
        favorites,
      ).slice(0, query ? 8 : 6),
    [items, query, hostFilter, favorites],
  )

  const navigate = useCallback((view) => {
    setActiveView(view)
    setMobileMenu(false)
    window.scrollTo({ top: 0, behavior: 'smooth' })
  }, [])

  const selectCandidateFinding = useCallback((candidateUid) => {
    setCandidateFocusUid(candidateUid)
    navigate('candidates')
  }, [navigate])

  const clearCandidateFocus = useCallback(() => setCandidateFocusUid(null), [])

  const selectHost = useCallback(
    (hostId) => {
      setHostFilter((current) => (current === hostId ? '' : hostId))
      navigate('skills')
    },
    [navigate],
  )

  const changeSkillCategory = useCallback((category) => {
    setCategoryFilter(category)
    setSkillSubcategoryFilter('')
  }, [])

  const changeToolType = useCallback((type) => {
    setToolType(type)
    setToolSubcategoryFilter('')
  }, [])

  const clearFilters = () => {
    setQuery('')
    setHostFilter('')
    setCategoryFilter('')
    setSkillSubcategoryFilter('')
    setToolSubcategoryFilter('')
    setLocalizationFilter('')
    setFavoriteOnly(false)
    setMultiHost(false)
  }

  const shellClass = `app-shell${selectedItem || showBoundary || selectedCollectionEntry ? ' drawer-open' : ''}`

  return (
    <div className={shellClass}>
      <Sidebar
        activeView={activeView}
        mobileMenu={mobileMenu}
        onNavigate={navigate}
        onClose={() => setMobileMenu(false)}
      />
      <main className="workspace">
        <Topbar
          title={PAGE_TITLES[activeView]}
          query={query}
          onQuery={setQuery}
          onMenu={() => setMobileMenu(true)}
          onRefresh={refresh}
          refreshing={refreshing}
          snapshot={activeView === 'project-skills' ? projectSkillView?.snapshot : snapshot}
          onBoundary={() => setShowBoundary(true)}
          candidateMode={activeView === 'candidates'}
          collectionMode={activeView === 'collections'}
          projectSkillMode={activeView === 'project-skills'}
        />

        <div className="workspace-content">
          {notice && <Notice tone={notice.tone}>{notice.message}</Notice>}
          {activeView === 'project-skills' ? (
            <ProjectSkillsView
              snapshot={projectSkillView?.snapshot || null}
              attempt={projectSkillView?.last_attempt || null}
              integrity={projectSkillView?.integrity || null}
              report={projectSkillView?.report || null}
              loading={projectSkillLoading}
              refreshing={projectSkillRefreshing}
              error={projectSkillError}
              onRefresh={refreshProjectSkills}
              onReload={loadProjectSkills}
              temporaryProjectSkill={temporaryProjectSkill}
              onChooseOtherFolder={(trigger) => openFolderDialog('project-skills', trigger)}
            />
          ) : activeView === 'collections' ? (
            <CollectionsView
              snapshot={collectionSnapshot}
              scanAttempt={collectionScanAttempt}
              candidateCatalog={candidateCatalog}
              loading={collectionLoading}
              checking={collectionChecking}
              refreshing={collectionRefreshing}
              query={query}
              onRefresh={refreshCollections}
              sourceSession={sourceSession}
              sourceBusy={sourceSwitching}
              onChooseSource={openFolderDialog}
              onRestoreSource={restoreDefaultSource}
              selectedEntry={selectedCollectionEntry}
              onSelectEntry={setSelectedCollectionEntry}
            />
          ) : loading && !snapshot ? (
            <LoadingState />
          ) : !snapshot ? (
            <FirstObservation
              serviceReady={Boolean(health?.ok)}
              refreshing={refreshing}
              onRefresh={refresh}
              onRetry={loadWorkbench}
            />
          ) : (
            <>
              {activeView !== 'candidates' && activeView !== 'collections' && (
                <StatusStrip snapshot={snapshot} onBoundary={() => setShowBoundary(true)} />
              )}
              {activeView === 'overview' && (
                <Overview
                  snapshot={snapshot}
                  hosts={hosts}
                  groups={groups}
                  items={overviewItems}
                  favorites={favorites}
                  query={query}
                  hostFilter={hostFilter}
                  onSelectItem={setSelectedItem}
                  onToggleFavorite={toggleFavorite}
                  onSelectHost={selectHost}
                  onNavigate={navigate}
                />
              )}
              {activeView === 'skills' && (
                <LibraryView
                  items={filteredSkills}
                  allItems={items}
                  total={skillItems.length}
                  hosts={hosts}
                  categories={categories}
                  subcategories={skillSubcategories}
                  hostFilter={hostFilter}
                  categoryFilter={categoryFilter}
                  subcategoryFilter={skillSubcategoryFilter}
                  localizationFilter={localizationFilter}
                  favoriteOnly={favoriteOnly}
                  multiHost={multiHost}
                  favorites={favorites}
                  findings={findings}
                  onHostFilter={setHostFilter}
                  onCategoryFilter={changeSkillCategory}
                  onSubcategoryFilter={setSkillSubcategoryFilter}
                  onLocalizationFilter={setLocalizationFilter}
                  onFavoriteOnly={setFavoriteOnly}
                  onMultiHost={setMultiHost}
                  onSelectItem={setSelectedItem}
                  onToggleFavorite={toggleFavorite}
                  onClear={clearFilters}
                />
              )}
              {activeView === 'candidates' && (
                <CandidatesView
                  csrfToken={health?.csrf_token}
                  initialCatalog={candidateCatalog}
                  onCatalog={setCandidateCatalog}
                  collectionSourceRoot={collectionSnapshot?.source_root}
                  sourceSession={sourceSession}
                  sourceBusy={sourceSwitching}
                  onChooseSource={openFolderDialog}
                  onRestoreSource={restoreDefaultSource}
                  focusUid={candidateFocusUid}
                  onFocusHandled={clearCandidateFocus}
                />
              )}
              {activeView === 'tools' && (
                <ToolsView
                  snapshot={snapshot}
                  items={filteredTools}
                  allItems={items}
                  totalItems={items}
                  hosts={hosts}
                  toolType={toolType}
                  subcategories={toolSubcategories}
                  subcategoryFilter={toolSubcategoryFilter}
                  hostFilter={hostFilter}
                  favoriteOnly={favoriteOnly}
                  multiHost={multiHost}
                  favorites={favorites}
                  findings={findings}
                  onType={changeToolType}
                  onSubcategoryFilter={setToolSubcategoryFilter}
                  onHostFilter={setHostFilter}
                  onFavoriteOnly={setFavoriteOnly}
                  onMultiHost={setMultiHost}
                  onSelectItem={setSelectedItem}
                  onToggleFavorite={toggleFavorite}
                  onClear={clearFilters}
                />
              )}
              {activeView === 'health' && (
                <HealthView
                  groups={healthGroups}
                  snapshot={snapshot}
                  items={items}
                  candidateCatalog={candidateCatalog}
                  candidateFindings={candidateFindings}
                  candidateScope={candidateScope}
                  onSelectItem={setSelectedItem}
                  onSelectCandidate={selectCandidateFinding}
                  onBoundary={() => setShowBoundary(true)}
                />
              )}
            </>
          )}
        </div>
      </main>

      <BottomNav activeView={activeView} onNavigate={navigate} />
      {selectedItem && (
        <AssetDrawer
          item={selectedItem}
          findings={itemFindings(selectedItem, findings)}
          favorite={favorites.has(selectedItem.asset_id)}
          onFavorite={() => toggleFavorite(selectedItem.asset_id)}
          onClose={() => setSelectedItem(null)}
        />
      )}
      {showBoundary && (
        <BoundaryDrawer snapshot={snapshot} health={health} onClose={() => setShowBoundary(false)} />
      )}
      {folderDialog && (
        <FolderSourceDialog
          state={folderDialog}
          picker={folderPicker}
          busy={sourceSwitching}
          onChoose={chooseFolder}
          onConfirm={confirmFolder}
          onClose={closeFolderDialog}
        />
      )}
    </div>
  )
}

function Sidebar({ activeView, mobileMenu, onNavigate, onClose }) {
  return (
    <>
      {mobileMenu && <button className="menu-scrim" aria-label="关闭导航" onClick={onClose} />}
      <aside className={`sidebar${mobileMenu ? ' is-open' : ''}`}>
        <div className="brand">
          <div className="brand-mark" aria-hidden="true">
            <Boxes size={21} strokeWidth={2.2} />
          </div>
          <div>
            <strong>AI-Toolbox</strong>
            <span>本地能力工作台</span>
          </div>
          <button className="icon-button sidebar-close" onClick={onClose} aria-label="关闭导航">
            <PanelLeftClose size={19} />
          </button>
        </div>
        <nav className="sidebar-nav" aria-label="主要导航">
          {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
            <button
              key={id}
              className={activeView === id ? 'active' : ''}
              onClick={() => onNavigate(id)}
            >
              <Icon size={19} />
              <span>{label}</span>
            </button>
          ))}
        </nav>
        <div className="boundary-card">
          <ShieldCheck size={22} />
          <div>
            <strong>只读观察</strong>
            <span>不会改动任何宿主</span>
          </div>
        </div>
      </aside>
    </>
  )
}

function BottomNav({ activeView, onNavigate }) {
  return (
    <nav className="bottom-nav" aria-label="移动端导航">
      {NAV_ITEMS.map(({ id, label, icon: Icon }) => (
        <button key={id} className={activeView === id ? 'active' : ''} onClick={() => onNavigate(id)}>
          <Icon size={19} />
          <span>
            {label
              .replace('Plugin 与工具', '工具')
              .replace('备选 skill 库', '备选')
              .replace('全部收藏', '收藏')}
          </span>
        </button>
      ))}
    </nav>
  )
}

function Topbar({
  title,
  query,
  onQuery,
  onMenu,
  onRefresh,
  refreshing,
  snapshot,
  onBoundary,
  candidateMode,
  collectionMode,
  projectSkillMode,
}) {
  return (
    <header className={candidateMode ? 'topbar candidate-topbar' : 'topbar'}>
      <div className="title-cluster">
        <button className="icon-button menu-button" onClick={onMenu} aria-label="打开导航">
          <Menu size={21} />
        </button>
        <div>
          <h1>{title}</h1>
          {!candidateMode && (
            <span className="mobile-scan-age">{snapshotAge(snapshot?.generated_at)}</span>
          )}
        </div>
      </div>
      {candidateMode || projectSkillMode ? (
        <div className="candidate-topbar-context">
          {projectSkillMode ? <FolderOpen size={17} aria-hidden="true" /> : <Database size={17} aria-hidden="true" />}
          <span>{projectSkillMode ? '项目内只读证据' : '收藏夹决策区'}</span>
          <i aria-hidden="true" />
          <small>{projectSkillMode ? '不代表已加载或可调用' : '留 / 砍 / 并'}</small>
        </div>
      ) : (
        <label className="global-search">
          <Search size={19} />
          <input
            value={query}
            onChange={(event) => onQuery(event.target.value)}
            placeholder="描述你想做的事，或搜索能力名称"
            aria-label="搜索能力"
          />
          {query && (
            <button type="button" onClick={() => onQuery('')} aria-label="清空搜索">
              <X size={16} />
            </button>
          )}
        </label>
      )}
      <div className="topbar-actions">
        {!projectSkillMode && (
          <button className="secondary-button info-button" onClick={onBoundary}>
            <Info size={17} />
            <span>{candidateMode || collectionMode ? '边界说明' : '观察说明'}</span>
          </button>
        )}
        {!candidateMode && !collectionMode && !projectSkillMode && (
          <>
            <button className="refresh-button" onClick={onRefresh} disabled={refreshing}>
              <RefreshCw size={17} className={refreshing ? 'spin' : ''} />
              <span>{refreshing ? '观察中…' : '刷新观察'}</span>
            </button>
            <div className="scan-time">
              <span>上次观察</span>
              <strong>{snapshot ? formatTimestamp(snapshot.generated_at) : '尚未观察'}</strong>
            </div>
          </>
        )}
      </div>
    </header>
  )
}

function Notice({ tone, children }) {
  const Icon = tone === 'error' ? CircleAlert : tone === 'success' ? Check : Info
  return (
    <div className={`notice notice-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <Icon size={18} />
      <span>{children}</span>
    </div>
  )
}

function FolderSourceDialog({ state, picker, busy, onChoose, onConfirm, onClose }) {
  const dialogRef = useRef(null)
  const closeRef = useRef(null)
  const candidateContext = state.context === 'candidates'
  const projectSkillContext = state.context === 'project-skills'
  const title = projectSkillContext
    ? '选择其他项目文件夹'
    : candidateContext
      ? '为备选 Skill 库选择收藏文件夹'
      : '为全部收藏选择文件夹'

  useEffect(() => {
    closeRef.current?.focus()
    const handleKeyDown = (event) => {
      if (event.key === 'Escape' && !busy) {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab' || !dialogRef.current) return
      const focusable = [...dialogRef.current.querySelectorAll(
        'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
      )]
      if (!focusable.length) return
      const first = focusable[0]
      const last = focusable[focusable.length - 1]
      if (event.shiftKey && document.activeElement === first) {
        event.preventDefault()
        last.focus()
      } else if (!event.shiftKey && document.activeElement === last) {
        event.preventDefault()
        first.focus()
      }
    }
    document.addEventListener('keydown', handleKeyDown)
    return () => document.removeEventListener('keydown', handleKeyDown)
  }, [busy, onClose])

  const description = projectSkillContext
    ? '选择一个位于“文稿”观察根内的具体项目文件夹。本页只读取该项目批准的 Skill 入口，并临时展示结果。'
    : candidateContext
      ? '此页只识别所选目录中可评估的 Skill 包，用于留／砍／并；确认后同一目录也会成为本次会话的“全部收藏”来源。'
      : '默认只读盘点当前收藏目录。只有在收藏位于其他目录，或想临时盘点另一批资料时，才需要切换。'

  return (
    <div
      className="folder-dialog-backdrop"
      onMouseDown={(event) => { if (event.target === event.currentTarget && !busy) onClose() }}
    >
      <section
        className="folder-dialog"
        role="dialog"
        aria-modal="true"
        aria-busy={busy}
        aria-labelledby="folder-dialog-title"
        aria-describedby="folder-dialog-description"
        ref={dialogRef}
      >
        <header>
          <span className="folder-dialog-icon"><FolderOpen size={21} /></span>
          <div>
            <p className="section-kicker">选择前说明</p>
            <h2 id="folder-dialog-title">{title}</h2>
          </div>
          <button ref={closeRef} className="icon-button" onClick={onClose} disabled={busy} aria-label="关闭文件夹选择说明">
            <X size={18} />
          </button>
        </header>

        {state.step === 'confirm' ? (
          <div className="folder-dialog-confirm" id="folder-dialog-description">
            <p>系统选择器已返回以下目录。请核对后再{projectSkillContext ? '读取项目 Skill' : '建立只读索引'}：</p>
            <code>{state.displayPath}</code>
            <div className="folder-dialog-safety">
              <ShieldCheck size={18} />
              <span>{projectSkillContext
                ? '结果只保留在当前页面内存中，不加入项目登记、不覆盖完整快照，也不写回所选文件夹。'
                : '只有“全部收藏”和“备选 Skill 库”两份索引都成功且来源一致时才会切换。默认快照不会被覆盖。'}
              </span>
            </div>
          </div>
        ) : state.step === 'picking' ? (
          <div className="folder-dialog-waiting" id="folder-dialog-description" role="status">
            <FolderOpen size={22} />
            <div>
              <strong>系统文件夹窗口正在等待操作</strong>
              <p>请在系统窗口中选择文件夹，或按 Esc 取消。窗口关闭后，本页会自动继续；无需重复点击。</p>
              <small>如果暂时没看到，请查看浏览器前方、其他显示器或 Dock 中的“osascript”窗口。</small>
            </div>
          </div>
        ) : (
          <div className="folder-dialog-copy" id="folder-dialog-description">
            <p>{description}</p>
            <dl>
              <div>
                <dt>会读取什么</dt>
                <dd>{projectSkillContext
                  ? '固定 Skill 入口、SKILL.md 的受限 frontmatter，以及同项目内安全链接关系；不读取正文。'
                  : '文件名、目录结构、有限元数据、可识别清单，以及受大小和数量上限保护的包内清单。'}
                </dd>
              </div>
              <div>
                <dt>不会做什么</dt>
                <dd>不移动、重命名、删除、解压到磁盘、执行、安装或联网；不修改任何宿主。根／祖先软链接和宽泛敏感目录会被拒绝，目录内软链接只记录并跳过。</dd>
              </div>
              <div>
                <dt>影响范围</dt>
                <dd>{projectSkillContext
                  ? '只在当前“项目 Skill”页面临时增加一个可选项目；不加入项目登记，刷新页面后回到登记项目。'
                  : '只影响当前本地服务会话中的两个页面；重启服务或点击“恢复默认”后回到默认来源。'}
                </dd>
              </div>
            </dl>
            <p className="folder-dialog-caution">{projectSkillContext
              ? '请选择具体项目文件夹；整个个人目录、桌面、文稿根目录、系统目录和敏感目录都会被拒绝。'
              : '请选择尽可能小、专门存放收藏或备选 Skill 的目录；不要选择整个个人目录、桌面、文稿或系统目录。'}
            </p>
            {!picker.available && (
              <Notice tone="error">{picker.reason || '当前本地环境不支持系统文件夹选择器。'}</Notice>
            )}
          </div>
        )}

        {state.error && <Notice tone="error">{state.error}</Notice>}
        <footer>
          <button className="secondary-button" onClick={onClose} disabled={busy}>取消</button>
          {state.step === 'confirm' ? (
            <button className="refresh-button" onClick={onConfirm} disabled={busy || !state.selectionToken}>
              <ShieldCheck size={17} />
              {busy
                ? projectSkillContext ? '正在读取项目 Skill…' : '正在建立两份索引…'
                : projectSkillContext ? '确认并读取项目 Skill' : '确认并建立只读索引'}
            </button>
          ) : (
            <button className="refresh-button" onClick={onChoose} disabled={busy || !picker.available}>
              <FolderOpen size={17} />
              {busy ? '等待系统窗口选择…' : '了解并选择'}
            </button>
          )}
        </footer>
      </section>
    </div>
  )
}

function LoadingState() {
  return (
    <div className="loading-layout" aria-label="正在载入工作台">
      <div className="skeleton skeleton-strip" />
      <div className="metrics-grid">
        {Array.from({ length: 4 }, (_, index) => (
          <div className="skeleton skeleton-metric" key={index} />
        ))}
      </div>
      <div className="skeleton skeleton-panel" />
    </div>
  )
}

function FirstObservation({ serviceReady, refreshing, onRefresh, onRetry }) {
  return (
    <section className="first-observation">
      <div className="first-icon">
        <Search size={28} />
      </div>
      <p className="section-kicker">独立工作台已就绪</p>
      <h2>先建立一份只读能力快照</h2>
      <p>
        工作台只会读取明确列出的 Skill、Plugin 与 Codex MCP、CLI、SDK 安全观察源。
        不会执行命令、连接 MCP、导入 SDK，也不读取 auth/config 等独立宿主凭据文件、联网或写入宿主。
        缓存 manifest 的操作字段与检测到的敏感配置不会写入快照。
      </p>
      {serviceReady ? (
        <button className="primary-button" onClick={onRefresh} disabled={refreshing}>
          <RefreshCw size={18} className={refreshing ? 'spin' : ''} />
          {refreshing ? '正在观察…' : '开始只读观察'}
        </button>
      ) : (
        <button className="secondary-button" onClick={onRetry}>
          <RefreshCw size={18} />
          重新连接本地服务
        </button>
      )}
    </section>
  )
}

function StatusStrip({ snapshot, onBoundary }) {
  const errors = snapshot.summary?.scan_error_count || 0
  return (
    <button className={`status-strip${errors ? ' has-errors' : ''}`} onClick={onBoundary}>
      {errors ? <AlertTriangle size={18} /> : <ShieldCheck size={18} />}
      <span>
        <strong>观察模式</strong>
        <i aria-hidden="true" />
        本地扫描
        <i aria-hidden="true" />
        {errors} 个扫描错误
      </span>
      <span className="status-age">快照 {snapshotAge(snapshot.generated_at)}</span>
      <ChevronRight size={17} />
    </button>
  )
}

function Overview({
  snapshot,
  hosts,
  groups,
  items,
  favorites,
  query,
  hostFilter,
  onSelectItem,
  onToggleFavorite,
  onSelectHost,
  onNavigate,
}) {
  const counts = snapshot.summary?.counts_by_type || {}
  const metricValues = {
    skill: counts.skill || 0,
    tools: (counts.plugin || 0) + (counts.mcp || 0) + (counts.cli || 0) + (counts.sdk || 0),
    bindings: snapshot.summary?.host_binding_count || 0,
    findings: snapshot.summary?.health_finding_count || 0,
  }
  const { attention, designNotes } = splitAttention(groups)
  const attentionCount = countFindings(attention)
  const designNoteCount = countFindings(designNotes)
  return (
    <>
      <section className="metrics-grid" aria-label="能力摘要">
        {METRICS.map(({ key, label, icon: Icon, tone }) => (
          <article className="metric-card" key={key}>
            <span className={`metric-icon tone-${tone}`}>
              <Icon size={21} />
            </span>
            <div>
              <strong>{metricValues[key]}</strong>
              <span>{label}</span>
            </div>
          </article>
        ))}
      </section>

      <div className="overview-grid">
        <section className="panel capability-panel">
          <PanelHeading
            title={query ? '搜索结果' : favorites.size ? '常用与收藏' : '能力入口'}
            meta={hostFilter ? `已筛选 ${HOST_LABELS[hostFilter]}` : `显示 ${items.length} 项`}
            action="查看全部能力"
            onAction={() => onNavigate('skills')}
          />
          {items.length ? (
            <div className="capability-grid compact-grid">
              {items.map((item) => (
                <AssetCard
                  key={item.asset_id}
                  item={item}
                  favorite={favorites.has(item.asset_id)}
                  findingCount={0}
                  onSelect={() => onSelectItem(item)}
                  onFavorite={() => onToggleFavorite(item.asset_id)}
                />
              ))}
            </div>
          ) : (
            <InlineEmpty title="没有匹配的能力" detail="换一个用途描述，或清除宿主筛选。" />
          )}
        </section>

        <div className="overview-side">
          <section className="panel host-panel">
            <PanelHeading title="宿主状态" meta="发现不等于启用" />
            <div className="host-list">
              {hosts.filter((host) => host.id !== 'antigravity').map((host) => (
                <button key={host.id} className="host-row" onClick={() => onSelectHost(host.id)}>
                  <HostMark hostId={host.id} />
                  <span className="host-copy">
                    <strong>{host.label}</strong>
                    <small>发现 {host.assetCount} 项</small>
                  </span>
                  <span className={`host-status status-${host.status}`}>
                    <i /> {host.status === 'observed' ? '已观察' : host.status === 'missing' ? '未发现' : '需关注'}
                  </span>
                  <ChevronRight size={16} />
                </button>
              ))}
            </div>
          </section>

          <section className="panel attention-panel">
            <PanelHeading
              title="需要关注"
              meta={`${attentionCount} 条事实`}
              action="查看全部"
              onAction={() => onNavigate('health')}
            />
            <div className="attention-list">
              {attention.slice(0, 3).map((group) => (
                <button key={group.code} onClick={() => onNavigate('health')}>
                  <span className={`finding-icon severity-${group.severity}`}>
                    <AlertTriangle size={17} />
                  </span>
                  <span>
                    <strong>{group.title}</strong>
                    <small>{group.findings.length} 个观察项</small>
                    {findingNote(group.code) && (
                      <small className="finding-note">{findingNote(group.code)}</small>
                    )}
                  </span>
                  <b>{group.findings.length}</b>
                  <ChevronRight size={16} />
                </button>
              ))}
              {!attention.length && (
                <InlineEmpty
                  title="当前范围内未发现需处理项"
                  detail="这不代表已验证全部能力可用。"
                />
              )}
            </div>
            {designNoteCount > 0 && (
              <p className="attention-design-note">
                另有 {designNoteCount} 项因只读模式跳过完整指纹（设计内，无需处理）。
              </p>
            )}
          </section>
        </div>
      </div>
    </>
  )
}

function PanelHeading({ title, meta, action, onAction }) {
  return (
    <header className="panel-heading">
      <div>
        <h2>{title}</h2>
        {meta && <span>{meta}</span>}
      </div>
      {action && (
        <button onClick={onAction}>
          {action} <ArrowRight size={16} />
        </button>
      )}
    </header>
  )
}

function FilterBar({
  hosts,
  hostFilter,
  onHostFilter,
  categoryFilter,
  categories,
  onCategoryFilter,
  subcategoryFilter,
  subcategories,
  onSubcategoryFilter,
  localizationFilter,
  onLocalizationFilter,
  favoriteOnly,
  onFavoriteOnly,
  multiHost,
  onMultiHost,
}) {
  return (
    <div className="filter-bar">
      <span className="filter-label">
        <ListFilter size={16} /> 筛选
      </span>
      <select value={hostFilter} onChange={(event) => onHostFilter(event.target.value)} aria-label="宿主筛选">
        <option value="">全部宿主</option>
        {hosts.map((host) => (
          <option key={host.id} value={host.id}>
            {host.label} · {host.assetCount}
          </option>
        ))}
      </select>
      {categories && (
        <select
          value={categoryFilter}
          onChange={(event) => onCategoryFilter(event.target.value)}
          aria-label="分类筛选"
        >
          <option value="">全部分类</option>
          {categories.map((category) => (
            <option key={category} value={category}>
              {category}
            </option>
          ))}
        </select>
      )}
      {subcategories && onSubcategoryFilter && (
        <select
          className="subcategory-select"
          value={subcategoryFilter}
          onChange={(event) => onSubcategoryFilter(event.target.value)}
          aria-label="子类目筛选"
          title={subcategoryFilter || '全部子类目'}
        >
          <option value="">全部子类目</option>
          {subcategories.map((subcategory) => (
            <option key={subcategory} value={subcategory}>
              {subcategory}
            </option>
          ))}
        </select>
      )}
      {onLocalizationFilter && (
        <select
          value={localizationFilter}
          onChange={(event) => onLocalizationFilter(event.target.value)}
          aria-label="中文说明状态筛选"
        >
          <option value="">全部说明状态</option>
          <option value="reviewed">已复核</option>
          <option value="stale">待复核</option>
          <option value="missing">缺失</option>
        </select>
      )}
      <button className={favoriteOnly ? 'filter-chip active' : 'filter-chip'} onClick={() => onFavoriteOnly(!favoriteOnly)}>
        <Star size={15} fill={favoriteOnly ? 'currentColor' : 'none'} /> 收藏
      </button>
      <button className={multiHost ? 'filter-chip active' : 'filter-chip'} onClick={() => onMultiHost(!multiHost)}>
        <Boxes size={15} /> 多宿主
      </button>
    </div>
  )
}

function LibraryView(props) {
  const activeFilterCount = [
    props.hostFilter,
    props.categoryFilter,
    props.subcategoryFilter,
    props.localizationFilter,
    props.favoriteOnly,
    props.multiHost,
  ].filter(Boolean).length
  return (
    <section className="library-view">
      <div className="view-intro">
        <div>
          <p className="section-kicker">按用途检索</p>
          <h2 aria-live="polite">{props.items.length} / {props.total} 个 Skill</h2>
          <p>优先看它能做什么，再到详情中核对来源、路径、宿主绑定与可信状态。</p>
        </div>
        {activeFilterCount > 0 && (
          <button className="text-button" onClick={props.onClear}>
            <X size={15} /> 清除 {activeFilterCount} 个筛选
          </button>
        )}
      </div>
      <FilterBar {...props} />
      {props.items.length ? (
        <GroupedAssetSections
          items={props.items}
          allItems={props.allItems}
          favorites={props.favorites}
          findings={props.findings}
          onSelectItem={props.onSelectItem}
          onToggleFavorite={props.onToggleFavorite}
        />
      ) : (
        <EmptyState
          title="没有符合条件的 Skill"
          detail="当前数据仍然保留，只是被搜索或筛选条件隐藏。"
          action="清除筛选"
          onAction={props.onClear}
        />
      )}
    </section>
  )
}

const COLLECTION_KIND_LABELS = {
  repository: 'Git 整库',
  project_directory: '项目目录',
  collection_directory: '收藏项目目录',
  skill_directory: '已解包 Skill',
  skill_archive: 'Skill 压缩包',
  archive: '压缩资料包',
  document: '文档资料',
  script: '独立脚本',
  resource_file: '资源文件',
}

const COLLECTION_TYPE_LABELS = {
  project: '项目 / 整库',
  composite_asset: '复合资产',
  skill: '标准 Skill',
  skill_bundle: 'Skill 包 / Skill 组',
  plugin: 'Plugin 包',
  workflow: '工作流包',
  agent_collection: 'Agent 集合',
  prompt_pack: 'Prompt 配方包',
  knowledge_base: '知识库 / 参考资料包',
  case_archive: '案例 / 对话归档',
  document: '文档资料',
  tool: '脚本 / 工具',
  resource: '资源文件',
  unknown: '待辨认对象',
}

const COLLECTION_TYPE_FILTERS = [
  ['skill', 'Skill'],
  ['project', '项目'],
  ['composite_skill_bundle', '复合 Skill 包'],
  ['plugin', 'Plugin'],
  ['workflow', '工作流'],
  ['other_material', '其他资料'],
]

const COLLECTION_FUNCTIONAL_TYPE_ORDER = [
  'skill', 'project', 'composite_skill_bundle', 'plugin', 'workflow', 'other_material',
]

const COLLECTION_FUNCTIONAL_TYPE_LABELS = {
  skill: 'Skill',
  project: '项目',
  composite_skill_bundle: '复合 Skill 包',
  plugin: 'Plugin',
  workflow: '工作流',
  other_material: '其他资料',
}

const COLLECTION_FUNCTIONAL_TYPE_DESCRIPTIONS = {
  skill: '单个标准 Skill 或待适配的单一 Skill 源。',
  project: '整库、应用源码或由多个组件组成的项目。',
  composite_skill_bundle: '包含多个可区分 Skill 的能力套件。',
  plugin: '带 Plugin manifest 的插件包。',
  workflow: '工作流、Agent、Prompt、CLI 或脚本型执行配方。',
  other_material: '案例、文档、知识库、图片和普通参考资料；不再细分使用场景。',
}

const COLLECTION_SCENARIO_GROUPS = (collectionTaxonomy.scenario?.groups || []).map(
  (group, order) => ({ ...group, order }),
)
const COLLECTION_SCENARIO_BY_NAME = new Map(
  COLLECTION_SCENARIO_GROUPS.map((group) => [group.name, group]),
)

const COLLECTION_COMPONENT_LABELS = {
  skill: 'Skill',
  skill_candidate: 'Skill 源文件',
  plugin: 'Plugin',
  workflow: '工作流',
  agent_collection: 'Agent 集合',
  agent_profile: 'Agent 配置',
  prompt: 'Prompt',
  prompt_pack: 'Prompt 包',
  knowledge_base: '知识库',
  case_study: '案例',
  cli: 'CLI',
  mcp: 'MCP',
  script: '脚本',
  document: '文档',
  resource: '资源',
}

const CLASSIFICATION_STATUS_LABELS = { reviewed: '人工复核', classified: '结构识别', needs_review: '待复核' }
const CLASSIFICATION_CONFIDENCE_LABELS = { high: '高置信度', medium: '中置信度', low: '低置信度' }
const READINESS_LABELS = {
  needs_adaptation: '需要适配',
  reference_only: '资料参考',
  standard_source: '标准入口已识别',
  not_assessed: '可用性未评估',
}
const ENTRY_STATUS_LABELS = { standard: '标准入口', candidate: '候选源文件', supporting: '配套组件' }
const COLLECTION_SCAN_ERROR_META = {
  symlink_skipped: {
    label: '符号链接未跟随',
    description: '为避免循环扫描或越出收藏范围，扫描器只记录这个链接，不读取它的目标。',
    advice: '如果目标目录已在同一收藏中正常收录，通常无需处理；不要仅为消除提示删除链接。',
    safe: true,
  },
  archive_compression_ratio: {
    label: '压缩比异常',
    description: '压缩包解压比例超出安全上限，已停止深入读取。',
    advice: '请先核对压缩包来源与用途；确认可信且需要收录时，在隔离位置检查后重新打包为正常压缩比，否则移出收藏范围。不要仅为消除提示调高安全上限。',
  },
  archive_duplicate_path: {
    label: '压缩包含重复路径',
    description: '压缩包中存在重复路径，无法稳定判断最终内容。',
    advice: '请重新打包并移除规范化后重复的成员路径；未处理前不要把该压缩包视为已完整索引。',
  },
  archive_encrypted_entry: {
    label: '压缩包含加密条目',
    description: '扫描器不尝试解密收藏内容，因此该条目未完整读取。',
    advice: 'Toolbox 不接收解密密码。若需索引，请自行确认来源后生成未加密副本；否则移出收藏范围，再手动刷新。',
  },
  archive_entry_limit: {
    label: '压缩包条目过多',
    description: '压缩包条目数超过只读扫描上限。',
    advice: '请确认是否需要整包收录；需要时拆分或精简压缩包，不需要则移出收藏范围。不要仅为消除提示调高条目上限。',
  },
  archive_member_too_large: {
    label: '压缩包成员过大',
    description: '压缩包内单个文件超过扫描上限。',
    advice: '请确认超大成员是否需要索引；需要时拆分或移除大文件后重新打包，否则移出收藏范围，再手动刷新。',
  },
  archive_path_invalid: {
    label: '压缩包路径不安全',
    description: '压缩包包含无效或可能越界的路径，已停止深入读取。',
    advice: '不要直接解压。请核对来源并用不含绝对路径、上级跳转或控制字符的路径重新打包；来源不可信时移出收藏范围。',
  },
  archive_total_size_limit: {
    label: '压缩包展开体积超限',
    description: '压缩包预计展开体积超过扫描上限。',
    advice: '请核对预计展开体积；确认可信且需要收录时拆分或精简后重新打包，否则移出收藏范围。不要调高展开体积上限。',
  },
  archive_unreadable: {
    label: '压缩包无法读取',
    description: '压缩包格式损坏、不受支持或当前不可读。',
    advice: '请核对文件是否完整、确为受支持的 ZIP 且当前可读；可信时重新下载或重新打包，无法确认时移出收藏范围，再手动刷新。',
  },
  entry_limit: { label: '收藏条目超限', description: '收藏目录的条目数超过本次只读扫描上限。' },
  manifest_read_limit: { label: '入口清单读取超限', description: '入口清单的读取量超过安全预算。' },
  manifest_too_large: { label: '入口清单过大', description: '入口清单文件超过扫描上限。' },
  manifest_unreadable: { label: '入口清单无法读取', description: '入口清单当前不可读或内容不完整。' },
  source_root_missing: { label: '收藏根目录不存在', description: '收藏根目录已移动、删除或当前不可达。' },
  source_root_unsafe: { label: '收藏根目录未通过安全校验', description: '收藏根目录不符合只读扫描边界，扫描已停止。' },
}
const DEFAULT_COLLECTION_SCAN_ERROR_META = {
  label: '对象未完整读取',
  description: '扫描器未能完整读取该路径，已保留其他可确认的索引信息。',
  safe: false,
}
const RELATION_LABELS = {
  packaged_copy_of: '压缩副本对应',
  packaged_as: '另有压缩分发版',
  portable_variant: '另有便携版本',
  supersedes: '升级自',
  superseded_by: '已被新版替代',
  duplicate_of: '重复来源',
  contains: '包含',
}
const VERSION_RELATION_TYPES = new Set([
  'packaged_copy_of',
  'packaged_as',
  'portable_variant',
  'supersedes',
  'superseded_by',
  'duplicate_of',
])
const COLLECTION_ORIGIN_META = {
  github: { label: 'GitHub', tone: 'violet' },
  website: { label: '网站', tone: 'blue' },
  chat: { label: '微信或聊天', tone: 'teal' },
  local: { label: '本地创建', tone: 'amber' },
  unknown: { label: '未知', tone: 'slate' },
}
const COLLECTION_ORIGIN_BASIS = {
  manual: '来自人工确认的来源记录。',
  git_remote: '根据 Git 仓库来源识别。',
  download_metadata: '根据文件保留的下载来源识别。',
  collection_path: '根据收藏目录中的来源标记识别。',
  unavailable: '当前文件没有可核实的来源记录。',
}
const SKILL_ORIGIN_META = {
  github: { label: 'GitHub', tone: 'violet' },
  website: { label: '网站', tone: 'blue' },
  chat: { label: '聊天', tone: 'teal' },
  local: { label: '本地创建', tone: 'amber' },
  system: { label: '系统自带', tone: 'green' },
  unknown: { label: '未知', tone: 'slate' },
}
const SKILL_ORIGIN_BASIS = {
  download_metadata: '根据文件保留的下载来源识别。',
  frontmatter: '根据 Skill 自身声明的本地创建标记识别。',
  hermes_bundled_manifest: '根据 Hermes 随附 Skill 清单识别。',
  hermes_skill_registry: '根据 Hermes 的安全来源投影识别。',
  workbuddy_skill_metadata: '根据 WorkBuddy Skill 元数据识别。',
  conflicting_evidence: '观察到的多个副本来源记录不一致，因此不替你猜测。',
  unavailable: '当前 Skill 没有可核实的来源记录。',
}

function collectionFunctionalType(item) {
  if (COLLECTION_FUNCTIONAL_TYPE_ORDER.includes(item.classification.functional_type)) {
    return item.classification.functional_type
  }
  const primaryType = item.classification.primary_type
  const componentTypes = new Set(item.classification.component_types)
  const skillNames = new Set(
    item.capabilities
      .filter((capability) => capability.type === 'skill' || capability.type === 'skill_candidate')
      .map((capability) => capability.name.trim().toLocaleLowerCase('en-US'))
      .filter(Boolean),
  )
  if (primaryType === 'project') return 'project'
  if (primaryType === 'composite_asset') {
    return componentTypes.has('skill_bundle') ? 'composite_skill_bundle' : 'project'
  }
  if (primaryType === 'skill' || primaryType === 'skill_bundle') {
    return skillNames.size > 1 ? 'composite_skill_bundle' : 'skill'
  }
  if (primaryType === 'plugin') return 'plugin'
  if (['workflow', 'agent_collection', 'prompt_pack', 'tool'].includes(primaryType)) return 'workflow'
  return 'other_material'
}

function collectionMatchesType(item, typeFilter) {
  return !typeFilter || collectionFunctionalType(item) === typeFilter
}

function collectionTypeIcon(functionalType) {
  if (functionalType === 'skill') return Sparkles
  if (functionalType === 'project') return Boxes
  if (functionalType === 'composite_skill_bundle') return Package
  if (functionalType === 'plugin') return Blocks
  if (functionalType === 'workflow') return Bot
  return FileText
}

function collectionScenarioOrder(scenario) {
  return COLLECTION_SCENARIO_BY_NAME.get(scenario)?.order ?? Number.MAX_SAFE_INTEGER
}

function collectionUsageScenario(item) {
  if (collectionFunctionalType(item) === 'other_material') return ''
  if (item.classification.primary_scenario) return item.classification.primary_scenario
  const counts = new Map()
  item.capabilities.forEach((capability) => {
    const scenario = capability.scenario
    if (!scenario || scenario === '待识别') return
    counts.set(scenario, (counts.get(scenario) || 0) + 1)
  })
  if (!counts.size) {
    item.scenarios.forEach((scenario) => {
      if (scenario && scenario !== '待识别') counts.set(scenario, 1)
    })
  }
  return [...counts.entries()]
    .sort(([left, leftCount], [right, rightCount]) =>
      rightCount - leftCount ||
      collectionScenarioOrder(left) - collectionScenarioOrder(right) ||
      left.localeCompare(right, 'zh-CN'),
    )[0]?.[0] || '待识别'
}

function collectionScenarioGuide(scenario, count) {
  const configured = COLLECTION_SCENARIO_BY_NAME.get(scenario)
  const relation = scenarioGuideFor(scenario, count)
  if (configured) {
    return {
      description: configured.desc,
      badge: `${relation.relationLabel} ${count} 个`,
      tone: relation.relationKind,
    }
  }
  if (scenario === '未归类' || scenario === '待识别') {
    return {
      description: '功能类型已经确认，但主要使用场景还需要补充。',
      badge: `待归场景 ${count} 个`,
      tone: 'unclassified',
    }
  }
  return {
    description: '按主要使用场景归入这一组。',
    badge: `${relation.relationLabel} ${count} 个`,
    tone: relation.relationKind,
  }
}

function normalizeCollectionPath(value) {
  return String(value || '').replaceAll('\\', '/').replace(/^\.\//, '')
}

function collectionScanErrorMeta(code) {
  const meta = COLLECTION_SCAN_ERROR_META[code] || DEFAULT_COLLECTION_SCAN_ERROR_META
  return {
    ...meta,
    advice: meta.advice || '核对该路径的文件状态与访问权限后，使用“手动刷新收藏索引”重新检查。',
    safe: Boolean(meta.safe),
  }
}

function collectionScanErrorFullPath(sourceRoot, path) {
  const rawPath = String(path || '').trim()
  if (!rawPath) return sourceRoot || '路径未提供'
  if (rawPath.startsWith('/')) return rawPath
  const root = String(sourceRoot || '').replace(/[\\/]+$/, '')
  return root ? `${root}/${rawPath.replace(/^[\\/]+/, '')}` : rawPath
}

function collectionEntryForScanError(error, entries) {
  const errorPath = normalizeCollectionPath(error?.path)
  if (!errorPath) return null
  let best = null
  let bestLength = -1
  entries.forEach((entry) => {
    const sources = [entry.source, ...(entry.sources || [])].filter(Boolean)
    sources.forEach((source) => {
      const sourcePath = normalizeCollectionPath(source.relative_path)
      if (!sourcePath) return
      if (errorPath !== sourcePath && !errorPath.startsWith(`${sourcePath}/`)) return
      if (sourcePath.length > bestLength) {
        best = entry
        bestLength = sourcePath.length
      }
    })
  })
  return best
}

function collectionOriginMeta(source) {
  const kind = COLLECTION_ORIGIN_META[source?.origin_kind] ? source.origin_kind : 'unknown'
  const meta = COLLECTION_ORIGIN_META[kind]
  const detail = COLLECTION_ORIGIN_BASIS[source?.origin_basis] || COLLECTION_ORIGIN_BASIS.unavailable
  return { ...meta, detail: `来源：${meta.label}。${detail}` }
}

function skillOriginMeta(item) {
  const source = item?.source || {}
  const kind = SKILL_ORIGIN_META[source.origin_kind] ? source.origin_kind : 'unknown'
  const meta = SKILL_ORIGIN_META[kind]
  const basis = SKILL_ORIGIN_BASIS[source.origin_basis] || SKILL_ORIGIN_BASIS.unavailable
  return {
    ...meta,
    detail: `来源：${meta.label}。${basis}只说明来源渠道，不代表当前会话已经调用或验证可用。`,
  }
}

function collectionAvailabilityMeta(source, installedHosts = []) {
  if (source?.installation_status === 'linked' || installedHosts.length > 0) {
    return {
      label: '宿主已引用',
      tone: 'green',
      detail: '已观察到宿主入口引用或安装记录；这不等于当前会话已经验证可调用。',
    }
  }
  return {
    label: '仅收藏',
    tone: 'amber',
    detail: '只在外部收藏目录中观察到；未观察到宿主安装，当前会话可调用性未验证。',
  }
}

function collectionVersionFor(entry, skill = null) {
  return String(skill?.version || entry.capability?.version || entry.source?.version || '').trim()
}

function collectionVersionRelations(entry) {
  return (entry.version_relations || []).filter((relation) =>
    VERSION_RELATION_TYPES.has(relation.type),
  )
}

function collectionVersionStateMeta(entry, version, copyPaths = []) {
  const relations = collectionVersionRelations(entry)
  if (relations.some((relation) => relation.type === 'superseded_by')) {
    return {
      label: '旧版',
      tone: 'red',
      detail: '已有明确关系指出它被另一个较新版本替代；这里只标记，不执行归档。',
    }
  }
  if (relations.some((relation) => relation.type === 'supersedes')) {
    return {
      label: '较新版本',
      tone: 'violet',
      detail: '已有明确关系指出它升级自另一个版本；尚未自动删除或归档旧版。',
    }
  }
  if (
    relations.some((relation) => relation.type === 'duplicate_of') ||
    copyPaths.length > 1 ||
    Number(entry.source?.duplicate_capability_count || 0) > 0
  ) {
    return {
      label: '发现重复',
      tone: 'rose',
      detail: '观察到同内容副本或重复能力；尚未自动判断哪一份应归档。',
    }
  }
  if (version) {
    return {
      label: '版本已记录',
      tone: 'blue',
      detail: '已读取版本号，但没有足够证据自动判定它是新版还是旧版。',
    }
  }
  return {
    label: '版本未声明',
    tone: 'slate',
    detail: '当前文件或元数据没有明确版本号，因此不猜测新旧关系。',
  }
}

function collectionRelationTone(type) {
  if (type === 'superseded_by') return 'red'
  if (type === 'supersedes') return 'violet'
  if (type === 'duplicate_of') return 'rose'
  return 'blue'
}

function collectionEntriesFor(items, candidateCatalog, sourceRoot) {
  const sourceByPath = new Map(
    items.map((item) => [normalizeCollectionPath(item.relative_path), item]),
  )
  const incomingRelations = new Map()
  items.forEach((item) => {
    ;(item.relations || []).forEach((relation) => {
      if (relation.type !== 'supersedes') return
      const target = normalizeCollectionPath(relation.target_relative_path)
      if (!target) return
      if (!incomingRelations.has(target)) incomingRelations.set(target, [])
      incomingRelations.get(target).push({
        type: 'superseded_by',
        target_relative_path: item.relative_path,
        note: relation.note || '',
      })
    })
  })
  const relationsFor = (source) => source ? [
    ...(source.relations || []),
    ...(incomingRelations.get(normalizeCollectionPath(source.relative_path)) || []),
  ] : []
  const candidateSourcesMatch = !candidateCatalog || (
    typeof candidateCatalog.source_dir === 'string' &&
    candidateCatalog.source_dir === sourceRoot
  )
  const candidateSkills = candidateSourcesMatch && Array.isArray(candidateCatalog?.items)
    ? candidateCatalog.items
    : []
  const candidateSourcePaths = new Set()
  const skillEntries = candidateSkills.flatMap((skill) => {
    const paths = [skill.src, ...(Array.isArray(skill.copy_srcs) ? skill.copy_srcs : [])]
      .map(normalizeCollectionPath)
      .filter(Boolean)
    paths.forEach((path) => candidateSourcePaths.add(path))
    const source = paths.map((path) => sourceByPath.get(path)).find(Boolean) || null
    // The legacy candidate catalog is refreshed independently and can lag behind
    // the physical collection snapshot. Never keep an orphaned catalog row in
    // "All Collections" after its source has been removed from disk.
    if (!source) return []
    const reviewedSingleScenario = source &&
      collectionFunctionalType(source) === 'skill' &&
      source.classification.status === 'reviewed'
      ? source.classification.primary_scenario
      : ''
    const scenario = reviewedSingleScenario || skill.group || '未归类'
    return [{
      entry_kind: 'candidate_skill',
      entry_id: `candidate-skill:${skill.uid}`,
      functional_type: 'skill',
      scenario,
      scenario_overridden: Boolean(reviewedSingleScenario && reviewedSingleScenario !== skill.group),
      skill,
      source,
      version_relations: relationsFor(source),
    }]
  })
  const sourceEntries = items
    .filter((item) => collectionFunctionalType(item) !== 'skill')
    .map((source) => ({
      entry_kind: 'source',
      entry_id: `source:${source.source_id}`,
      functional_type: collectionFunctionalType(source),
      scenario: collectionUsageScenario(source),
      source,
      version_relations: relationsFor(source),
    }))
  const discoveredByCapability = new Map()
  items
    .filter((item) =>
      collectionFunctionalType(item) === 'skill' &&
      (!candidateSkills.length || !candidateSourcePaths.has(normalizeCollectionPath(item.relative_path))),
    )
    .forEach((source) => {
      const capability = source.capabilities.find((row) =>
        row.type === 'skill' || row.type === 'skill_candidate',
      )
      const identity = capability?.capability_id || `source:${source.source_id}`
      if (!discoveredByCapability.has(identity)) {
        discoveredByCapability.set(identity, {
          entry_kind: 'discovered_skill',
          entry_id: `discovered-skill:${identity}`,
          functional_type: 'skill',
          scenario: source.classification.primary_scenario || '未归类',
          capability,
          source,
          sources: [],
          version_relations: [],
        })
      }
      const entry = discoveredByCapability.get(identity)
      entry.sources.push(source)
      entry.version_relations.push(...relationsFor(source))
    })
  return [...skillEntries, ...discoveredByCapability.values(), ...sourceEntries]
}

function collectionEntryName(entry) {
  if (entry.entry_kind === 'candidate_skill') return entry.skill.zh_name || entry.skill.name
  if (entry.entry_kind === 'discovered_skill') return entry.capability?.name || entry.source.name
  return entry.source.name
}

function collectionEntryMatches(entry, query) {
  if (entry.entry_kind === 'source') return collectionMatches(entry.source, query)
  if (entry.entry_kind === 'discovered_skill') {
    const needle = query.trim().toLowerCase()
    if (!needle) return true
    return [
      entry.capability?.name,
      entry.capability?.description,
      entry.capability?.relative_path,
      entry.scenario,
      ...entry.sources.map((source) => `${source.name} ${source.relative_path}`),
    ].join(' ').toLowerCase().includes(needle)
  }
  const skill = entry.skill
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  return [
    skill.zh_name,
    skill.name,
    skill.zh_sum,
    skill.desc,
    skill.desc_full,
    skill.group,
    skill.src,
    ...(skill.tags || []),
    ...(skill.platform || []),
    ...(skill.inputs || []),
    ...(skill.outputs || []),
  ].join(' ').toLowerCase().includes(needle)
}

function collectionEntryMatchesState(entry, stateFilter) {
  if (!stateFilter) return true
  const source = entry.source
  if (stateFilter === 'anchor') return source?.move_safety === 'keep_anchor'
  if (stateFilter === 'review') return source?.move_safety !== 'keep_anchor'
  if (stateFilter === 'unclassified') {
    return ['未归类', '待识别'].includes(entry.scenario) ||
      source?.classification.status === 'needs_review'
  }
  return true
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes) || bytes <= 0) return '大小未记录'
  if (bytes >= 1024 ** 3) return `${(bytes / 1024 ** 3).toFixed(1)} GB`
  if (bytes >= 1024 ** 2) return `${(bytes / 1024 ** 2).toFixed(1)} MB`
  if (bytes >= 1024) return `${(bytes / 1024).toFixed(1)} KB`
  return `${bytes} B`
}

function collectionMatches(item, query) {
  const needle = query.trim().toLowerCase()
  if (!needle) return true
  const capabilityText = item.capabilities
    .map((capability) => `${capability.name} ${capability.description} ${capability.scenario} ${capability.type}`)
    .join(' ')
  const classificationText = `${item.classification.display_label} ${item.classification.summary} ${item.classification.component_types.join(' ')} ${item.classification.basis.join(' ')}`
  return `${item.name} ${item.relative_path} ${item.scenarios.join(' ')} ${classificationText} ${capabilityText}`
    .toLowerCase()
    .includes(needle)
}

function CollectionsView({
  snapshot,
  scanAttempt,
  candidateCatalog,
  loading,
  checking,
  refreshing,
  query,
  onRefresh,
  sourceSession,
  sourceBusy,
  onChooseSource,
  onRestoreSource,
  selectedEntry,
  onSelectEntry,
}) {
  const [typeFilter, setTypeFilter] = useState('')
  const [scenarioFilter, setScenarioFilter] = useState('')
  const [stateFilter, setStateFilter] = useState('')
  const items = snapshot?.items || []
  const entries = useMemo(
    () => collectionEntriesFor(items, candidateCatalog, snapshot?.source_root),
    [candidateCatalog, items, snapshot?.source_root],
  )
  const candidateSourceMismatch = Boolean(
    snapshot && candidateCatalog && candidateCatalog.source_dir !== snapshot.source_root,
  )
  const sourceDisplayPath = sourceSession?.display_path || '来源路径未提供'
  const scenarios = useMemo(
    () => [...new Set(
      entries
        .filter((entry) => entry.functional_type !== 'other_material')
        .map((entry) => entry.scenario)
        .filter(Boolean),
    )].sort((left, right) =>
      collectionScenarioOrder(left) - collectionScenarioOrder(right) ||
      left.localeCompare(right, 'zh-CN'),
    ),
    [entries],
  )
  useEffect(() => {
    if (typeFilter === 'other_material' && scenarioFilter) setScenarioFilter('')
  }, [scenarioFilter, typeFilter])
  const filtered = useMemo(
    () =>
      entries.filter((entry) => {
        if (!collectionEntryMatches(entry, query)) return false
        if (typeFilter && entry.functional_type !== typeFilter) return false
        if (scenarioFilter && entry.scenario !== scenarioFilter) return false
        if (!collectionEntryMatchesState(entry, stateFilter)) return false
        return true
      }),
    [entries, query, scenarioFilter, stateFilter, typeFilter],
  )
  const grouped = useMemo(() => {
    return COLLECTION_FUNCTIONAL_TYPE_ORDER.map((functionalType) => {
      const typeItems = filtered
        .filter((entry) => entry.functional_type === functionalType)
        .sort((left, right) => collectionEntryName(left).localeCompare(collectionEntryName(right), 'zh-CN'))
      if (!typeItems.length) return null
      const scenarioGroups = functionalType === 'other_material' ? [] : scenarios
        .map((scenario) => ({
          scenario,
          items: typeItems.filter((entry) => entry.scenario === scenario),
        }))
        .filter((group) => group.items.length > 0)
      return { functionalType, items: typeItems, scenarioGroups }
    }).filter(Boolean)
  }, [filtered, scenarios])

  if (loading && !snapshot) {
    return (
      <section className="collection-loading" aria-label="正在读取全部收藏">
        <div className="skeleton skeleton-strip" />
        <div className="metrics-grid">
          {Array.from({ length: 4 }, (_, index) => (
            <div className="skeleton skeleton-metric" key={index} />
          ))}
        </div>
        <div className="skeleton skeleton-panel" />
      </section>
    )
  }

  if (!snapshot) {
    return (
      <section className="collection-first-state">
        <div className="first-icon">
          <BookOpen size={28} />
        </div>
        <h2>建立第一份全部收藏索引</h2>
        <p>只读取手工收藏目录并写入 Toolbox 自己的生成快照，不移动、解压、安装或执行任何收藏内容。</p>
        <CollectionScanNotice
          context="attempt"
          errors={scanAttempt?.scan_errors}
          entries={[]}
          sourceRoot={sourceSession?.display_path}
          onSelectEntry={onSelectEntry}
        />
        <div className="collection-first-actions">
          <button
            className="secondary-button"
            onClick={(event) => onChooseSource('collections', event.currentTarget)}
            disabled={sourceBusy}
          >
            <FolderOpen size={17} /> 选择其他收藏文件夹
          </button>
          <button className="refresh-button" onClick={onRefresh} disabled={checking || refreshing || sourceBusy}>
            <RefreshCw size={17} className={checking || refreshing ? 'spin' : ''} />
            <span>{checking ? '检查更新…' : refreshing ? '索引中…' : '建立默认只读索引'}</span>
          </button>
        </div>
      </section>
    )
  }

  const summary = snapshot.summary
  const skillCardCount = entries.filter((entry) => entry.functional_type === 'skill').length
  const retainedCandidateCount = entries.filter((entry) => entry.entry_kind === 'candidate_skill').length
  const unassignedScenarioCount = entries.filter(
    (entry) => entry.functional_type !== 'other_material' &&
      ['未归类', '待识别'].includes(entry.scenario),
  ).length
  const metrics = [
    { label: '全部收藏卡片', value: entries.length, icon: BookOpen, note: '固定六种功能类型' },
    { label: 'Skill 卡片', value: skillCardCount, icon: Sparkles, note: retainedCandidateCount ? `原备选库 ${retainedCandidateCount} 张已保留` : '来自收藏扫描结果' },
    { label: '物理收藏源', value: summary.source_count, icon: Boxes, note: '父项目与原路径仍可回查' },
    { label: '待归场景', value: unassignedScenarioCount, icon: CircleHelp, note: '其他资料不计使用场景' },
  ]

  return (
    <section className="collections-view">
      <div className="collections-hero">
        <div>
          <p className="section-kicker">手工收藏真源</p>
          <h2>所有收藏，一处看全</h2>
          <p>按功能类型与使用场景整理，快速找到需要的能力与资源。</p>
        </div>
        <div className="collections-hero-actions">
          <button
            className="secondary-button collection-source-button"
            onClick={(event) => onChooseSource('collections', event.currentTarget)}
            disabled={sourceBusy || checking || refreshing}
          >
            <FolderOpen size={17} />
            <span>选择文件夹</span>
          </button>
          <button className="secondary-button collection-refresh" onClick={onRefresh} disabled={checking || refreshing || sourceBusy}>
            <RefreshCw size={17} className={checking || refreshing ? 'spin' : ''} />
            <span>{checking ? '检查更新…' : refreshing ? '索引中…' : '手动刷新收藏索引'}</span>
          </button>
        </div>
      </div>

      <div className="collection-source-line">
        <ShieldCheck size={17} />
        <span>只读来源 · {sourceSession?.mode === 'temporary' ? '临时自选' : '默认目录'}</span>
        <code>{sourceDisplayPath}</code>
        <small>启动时已检查 · {formatTimestamp(snapshot.generated_at)}</small>
        {sourceSession?.mode === 'temporary' && (
          <button className="source-restore-button" onClick={onRestoreSource} disabled={sourceBusy}>
            恢复默认
          </button>
        )}
      </div>

      {candidateSourceMismatch && (
        <Notice tone="error">备选 Skill 数据与当前收藏来源不一致，已停止合并备选卡片；请重新刷新或恢复默认来源。</Notice>
      )}

      <div className="collection-metrics">
        {metrics.map(({ label, value, icon: Icon, note }) => (
          <div className="collection-metric" key={label}>
            <Icon size={19} />
            <div>
              <strong>{value}</strong>
              <span>{label}</span>
              <small>{note}</small>
            </div>
          </div>
        ))}
      </div>

      {summary.anchored_source_count > 0 && (
        <div className="collection-anchor-warning">
          <LockKeyhole size={18} />
          <div>
            <strong>{summary.anchored_source_count} 个收藏源正在被宿主入口引用，整理时必须保留原位</strong>
            <span>当前只生成整理建议，不会移动这些活动锚点。</span>
          </div>
        </div>
      )}

      <div className="collection-filter-bar">
        <span className="filter-label">
          <ListFilter size={16} />
          筛选
        </span>
        <select aria-label="对象类型筛选" value={typeFilter} onChange={(event) => setTypeFilter(event.target.value)}>
          <option value="">全部功能类型</option>
          {COLLECTION_TYPE_FILTERS.map(([value, label]) => (
            <option value={value} key={value}>{label}</option>
          ))}
        </select>
        <select
          aria-label="收藏场景筛选"
          value={scenarioFilter}
          onChange={(event) => setScenarioFilter(event.target.value)}
          disabled={typeFilter === 'other_material'}
        >
          <option value="">{typeFilter === 'other_material' ? '其他资料不分场景' : '全部场景'}</option>
          {scenarios.map((scenario) => (
            <option value={scenario} key={scenario}>{scenario}</option>
          ))}
        </select>
        <select aria-label="整理状态筛选" value={stateFilter} onChange={(event) => setStateFilter(event.target.value)}>
          <option value="">全部整理状态</option>
          <option value="anchor">活动锚点 · 原位保留</option>
          <option value="review">可进入移动预览</option>
          <option value="unclassified">待归类 / 待归场景</option>
        </select>
        <span className="collection-result-count">显示 {filtered.length} / {entries.length}</span>
      </div>

      <CollectionScanNotice
        context="attempt"
        errors={scanAttempt?.scan_errors}
        entries={entries}
        sourceRoot={sourceDisplayPath}
        onSelectEntry={onSelectEntry}
      />

      <CollectionScanNotice
        errors={snapshot.scan_errors}
        entries={entries}
        sourceRoot={sourceDisplayPath}
        onSelectEntry={onSelectEntry}
      />

      {grouped.length ? (
        <div className="collection-groups">
          {grouped.map((group) => (
            <CollectionTypeSection {...group} key={group.functionalType} onSelectEntry={onSelectEntry} />
          ))}
        </div>
      ) : (
        <EmptyState
          icon={Search}
          title="没有符合当前条件的收藏"
          detail="清空搜索词或调整功能类型、场景和整理状态筛选。"
        />
      )}
      {selectedEntry && (
        <CollectionEntryDrawer entry={selectedEntry} onClose={() => onSelectEntry(null)} />
      )}
    </section>
  )
}

function CollectionScanNotice({ errors = [], entries, sourceRoot, onSelectEntry, context = 'snapshot' }) {
  if (!errors.length) return null
  const safeCount = errors.filter((error) => collectionScanErrorMeta(error.code).safe).length
  const reviewCount = errors.length - safeCount
  const failedAttempt = context === 'attempt'
  const title = reviewCount > 0
    ? `${failedAttempt ? '本次检查有 ' : ''}${reviewCount} 个对象未完整读取${safeCount ? `，另记录 ${safeCount} 个安全跳过项` : ''}`
    : `发现 ${safeCount} 个安全跳过项`
  const detail = reviewCount > 0
    ? '本次结果未覆盖上一份有效索引；展开查看具体路径与处理建议。'
    : '当前有效索引仍可正常浏览；展开查看记录，通常无需处理。'
  const SummaryIcon = reviewCount > 0 ? CircleAlert : ShieldCheck

  return (
    <details className={`collection-scan-notice ${reviewCount > 0 ? 'needs-review' : 'safe-only'}`}>
      <summary>
        <span className="collection-scan-summary-icon"><SummaryIcon size={18} aria-hidden="true" /></span>
        <span className="collection-scan-summary-copy" role={reviewCount > 0 ? 'alert' : 'status'}>
          <strong>{title}</strong>
          <small>{detail}</small>
        </span>
        <span className="collection-scan-summary-count">{errors.length} 项</span>
        <ChevronRight className="collection-scan-summary-chevron" size={17} aria-hidden="true" />
      </summary>
      <div className="collection-scan-rows">
        {errors.map((error, index) => {
          const meta = collectionScanErrorMeta(error.code)
          const relatedEntry = collectionEntryForScanError(error, entries)
          return (
            <article className={`collection-scan-row ${meta.safe ? 'is-safe' : 'needs-review'}`} key={`${error.code}:${error.path}:${index}`}>
              <header>
                <span>{meta.safe ? '安全跳过' : '需要复核'}</span>
                <div>
                  <strong>{meta.label}</strong>
                  <code>{error.code || 'unknown_scan_error'}</code>
                </div>
              </header>
              <p>{meta.description}</p>
              {(error.detail || error.message) && (
                <p className="collection-scan-technical">技术补充：{error.detail || error.message}</p>
              )}
              <div className="collection-scan-path">
                <span>完整路径</span>
                <PathRow value={collectionScanErrorFullPath(sourceRoot, error.path)} />
              </div>
              <footer>
                <span>{meta.advice}</span>
                {relatedEntry && (
                  <button type="button" onClick={() => onSelectEntry(relatedEntry)}>
                    查看对应收藏 <ArrowRight size={15} aria-hidden="true" />
                  </button>
                )}
              </footer>
            </article>
          )
        })}
      </div>
    </details>
  )
}

function CollectionTypeSection({ functionalType, items, scenarioGroups, onSelectEntry }) {
  const Icon = collectionTypeIcon(functionalType)
  const isOtherMaterial = functionalType === 'other_material'
  return (
    <section className="collection-type-section" data-functional-type={functionalType}>
      <div className="collection-type-heading">
        <span className="collection-type-icon"><Icon size={19} /></span>
        <div>
          <span>功能类型</span>
          <h3>{COLLECTION_FUNCTIONAL_TYPE_LABELS[functionalType]}</h3>
          <p>{COLLECTION_FUNCTIONAL_TYPE_DESCRIPTIONS[functionalType]}</p>
        </div>
        <strong>{items.length} 项</strong>
      </div>
      {isOtherMaterial ? (
        <div className="collection-card-grid collection-other-grid">
          {items.map((entry) => (
            <CollectionEntryCard entry={entry} key={entry.entry_id} onSelect={onSelectEntry} />
          ))}
        </div>
      ) : (
        <div className="collection-scenario-groups">
          {scenarioGroups.map((group) => (
            <CollectionScenarioSection {...group} key={group.scenario} onSelectEntry={onSelectEntry} />
          ))}
        </div>
      )}
    </section>
  )
}

function CollectionScenarioSection({ scenario, items, onSelectEntry }) {
  const guide = collectionScenarioGuide(scenario, items.length)
  return (
    <section className="collection-scenario-section" data-scenario={scenario}>
      <div className="collection-scenario-heading">
        <div>
          <h4>{scenario}</h4>
          <span className={`collection-scenario-badge relation-${guide.tone}`}>{guide.badge}</span>
        </div>
        <p>{guide.description}</p>
      </div>
      <div className="collection-card-grid">
        {items.map((entry) => (
          <CollectionEntryCard entry={entry} key={entry.entry_id} onSelect={onSelectEntry} />
        ))}
      </div>
    </section>
  )
}

function CollectionEntryCard({ entry, onSelect }) {
  if (entry.entry_kind === 'candidate_skill') {
    return (
      <CollectionSkillCard
        skill={entry.skill}
        source={entry.source}
        scenario={entry.scenario}
        scenarioOverridden={entry.scenario_overridden}
        onSelect={() => onSelect(entry)}
      />
    )
  }
  if (entry.entry_kind === 'discovered_skill') {
    const source = entry.source
    const capability = entry.capability
    const installedHosts = [...new Set(
      entry.sources.flatMap((item) => item.host_links.map((link) => HOST_LABELS[link.host_id] || link.host_id)),
    )]
    const skill = {
      name: capability?.name || source.name,
      zh_name: capability?.name || source.name,
      version: capability?.version || source.version || '',
      zh_sum: capability?.description || source.classification.summary,
      desc_full: capability?.description || source.classification.summary,
      group: entry.scenario,
      src: source.relative_path,
      copy_srcs: entry.sources.map((item) => item.relative_path),
      tags: [source.classification.display_label, READINESS_LABELS[source.classification.readiness]],
      platform: [],
      inputs: [],
      outputs: [],
      shape: { 文件: source.shape.files, total: source.shape.files },
      copies: entry.sources.length,
      chars: 0,
      installed: { hosts: installedHosts, as_name: '' },
    }
    return (
      <CollectionSkillCard
        skill={skill}
        source={source}
        legacy={false}
        scenario={entry.scenario}
        onSelect={() => onSelect(entry)}
      />
    )
  }
  return <CollectionSourceCard item={entry.source} onSelect={() => onSelect(entry)} />
}

function CollectionSkillCard({ skill, source, legacy = true, scenario = skill.group, scenarioOverridden = false, onSelect }) {
  const resourceCount = Number(skill.shape?.total || 0)
  const installedHosts = Array.isArray(skill.installed?.hosts) ? skill.installed.hosts : []
  const origin = collectionOriginMeta(source)
  const displayTags = [
    ...(skill.tags || []),
    ...(skill.platform || []),
  ].filter(Boolean).slice(0, 6)
  return (
    <button
      type="button"
      className={`collection-source-card collection-skill-card${source?.move_safety === 'keep_anchor' ? ' is-anchor' : ''}`}
      onClick={onSelect}
    >
      <span className="collection-source-icon"><Sparkles size={18} /></span>
      <span className="collection-card-main">
        <strong>{skill.zh_name || skill.name}</strong>
        <small>{skill.name}</small>
        <span className="collection-card-description">{skill.zh_sum || skill.desc}</span>
        {(installedHosts.length > 0 || displayTags.length > 0) && (
          <span className="collection-card-tags">
            {installedHosts.length > 0 && <span className="installed-tag">已观察到安装</span>}
            {displayTags.map((tag) => <span key={tag}>{tag}</span>)}
          </span>
        )}
      </span>
      <span className="collection-card-facts">
        <span>{resourceCount} 项随附资源</span>
        <span>{skill.copies || 1} 份来源</span>
      </span>
      <span className="collection-card-footer">
        <span className="collection-card-disclosure">
          查看详情
          <ChevronRight size={15} />
        </span>
        <span className="collection-card-identity" aria-label="卡片身份信息">
          <CollectionMetaBadge {...origin} />
          <span>Skill</span>
        </span>
      </span>
    </button>
  )
}

function CollectionSourceCard({ item, onSelect }) {
  const functionalType = collectionFunctionalType(item)
  const Icon = collectionTypeIcon(functionalType)
  const origin = collectionOriginMeta(item)
  return (
    <button
      type="button"
      className={`collection-source-card${item.move_safety === 'keep_anchor' ? ' is-anchor' : ''}`}
      onClick={onSelect}
    >
      <span className="collection-source-icon"><Icon size={18} /></span>
      <span className="collection-card-main">
        <strong>{item.name}</strong>
        <span className="collection-card-description">{item.classification.summary}</span>
        <span className="collection-card-tags">
          <CollectionMetaBadge {...origin} />
          <span>{item.classification.display_label}</span>
          <span>{READINESS_LABELS[item.classification.readiness] || item.classification.readiness}</span>
          {item.move_safety === 'keep_anchor' && <span className="anchor-tag">活动锚点</span>}
        </span>
      </span>
      <span className="collection-card-facts">
        {item.capabilities.length > 0 ? `${item.capabilities.length} 项内部能力` : `${item.shape.documents} 份文档`}
        <span>{item.shape.files} 个文件</span>
      </span>
      <span className="collection-card-disclosure">
        查看详情
        <ChevronRight size={15} />
      </span>
    </button>
  )
}

function CollectionEntryDrawer({ entry, onClose }) {
  if (entry.entry_kind === 'candidate_skill' || entry.entry_kind === 'discovered_skill') {
    return <CollectionSkillDrawer entry={entry} onClose={onClose} />
  }
  return <CollectionSourceDrawer entry={entry} onClose={onClose} />
}

function CollectionSkillDrawer({ entry, onClose }) {
  const isCandidate = entry.entry_kind === 'candidate_skill'
  const source = entry.source
  const capability = entry.capability
  const installedHosts = isCandidate
    ? (Array.isArray(entry.skill?.installed?.hosts) ? entry.skill.installed.hosts : [])
    : [...new Set(entry.sources.flatMap((item) => item.host_links.map((link) => HOST_LABELS[link.host_id] || link.host_id)))]
  const skill = isCandidate ? entry.skill : {
    name: capability?.name || source.name,
    zh_name: capability?.name || source.name,
    version: capability?.version || source.version || '',
    zh_sum: capability?.description || source.classification.summary,
    desc_full: capability?.description || source.classification.summary,
    group: entry.scenario,
    src: source.relative_path,
    copy_srcs: entry.sources.map((item) => item.relative_path),
    tags: [source.classification.display_label, READINESS_LABELS[source.classification.readiness]],
    platform: [],
    inputs: [],
    outputs: [],
    shape: { 文件: source.shape.files, total: source.shape.files },
    copies: entry.sources.length,
    chars: 0,
    installed: { hosts: installedHosts, as_name: '' },
  }
  const resourceCount = Number(skill.shape?.total || 0)
  const supportingResources = Object.entries(skill.shape || {}).filter(([name, count]) => name !== 'total' && Number(count) > 0)
  const origin = collectionOriginMeta(source)
  const availability = collectionAvailabilityMeta(source, installedHosts)
  const version = collectionVersionFor(entry, skill)
  const copyPaths = [...new Set((skill.copy_srcs || []).map(normalizeCollectionPath).filter(Boolean))]
  const versionState = collectionVersionStateMeta(entry, version, copyPaths)
  const versionRelations = collectionVersionRelations(entry)
  return (
    <>
      <button className="drawer-scrim" aria-label="关闭详情" onClick={onClose} />
      <aside className="detail-drawer" aria-label={`${skill.zh_name || skill.name}详情`}>
        <header className="drawer-header">
          <button className="icon-button" onClick={onClose} aria-label="关闭详情"><X size={20} /></button>
        </header>
        <div className="drawer-content">
          <div className="drawer-title">
            <span className="asset-icon asset-skill"><Sparkles size={22} /></span>
            <div>
              <h2>{skill.zh_name || skill.name}</h2>
              <span>{skill.name}{skill.version ? ` · v${skill.version}` : ''}</span>
            </div>
          </div>
          <div className="locked-banner"><LockKeyhole size={16} /> 锁定 · 只读</div>
          <DrawerSection title="能力简介">
            <p>{skill.desc_full || skill.desc || skill.zh_sum}</p>
          </DrawerSection>
          <DrawerSection title="详情">
            <dl className="identity-grid">
              <div><dt>来源</dt><dd><CollectionMetaBadge {...origin} focusable /></dd></div>
              <div><dt>版本</dt><dd>{version ? `v${version}` : '未声明'}</dd></div>
              <div><dt>状态</dt><dd><CollectionMetaBadge {...availability} focusable /></dd></div>
              <div><dt>版本状态</dt><dd><CollectionMetaBadge {...versionState} focusable /></dd></div>
              <div><dt>场景</dt><dd>{entry.scenario || skill.group || '未归类'}</dd></div>
              <div><dt>识别方式</dt><dd>{entry.scenario_overridden ? '人工复核场景' : isCandidate ? '原备选 Skill 库分类' : '新收藏识别'}</dd></div>
              <div><dt>随附资源</dt><dd>{resourceCount} 项</dd></div>
              <div><dt>来源份数</dt><dd>{skill.copies || 1} 份</dd></div>
              {skill.chars > 0 && <div><dt>正文</dt><dd>{skill.chars.toLocaleString('zh-CN')} 字符</dd></div>}
              {source && <div><dt>物理源</dt><dd>{source.relative_path}</dd></div>}
            </dl>
            {skill.src && <div className="collection-path-field"><span>路径</span><PathRow value={skill.src} /></div>}
          </DrawerSection>
          {(skill.inputs?.length > 0 || skill.outputs?.length > 0) && (
            <DrawerSection title="输入与产出">
              {skill.inputs?.length > 0 && <ListSection title="输入" items={skill.inputs} />}
              {skill.outputs?.length > 0 && <ListSection title="产出" items={skill.outputs} />}
            </DrawerSection>
          )}
          {supportingResources.length > 0 && (
            <DrawerSection title="随附资源构成">
              <div className="collection-capability-summary">
                {supportingResources.map(([name, count]) => <span key={name}>{name} {count}</span>)}
              </div>
            </DrawerSection>
          )}
          {installedHosts.length > 0 && (
            <DrawerSection title="宿主观察（不代表可用）">
              <div className="binding-list">
                {installedHosts.map((host) => (
                  <article key={host}>
                    <div><strong>{host}</strong></div>
                  </article>
                ))}
              </div>
            </DrawerSection>
          )}
          <CollectionVersionRelations
            relations={versionRelations}
            copyPaths={copyPaths}
            duplicateCount={entry.source?.duplicate_capability_count || 0}
          />
        </div>
        <footer className="drawer-footer">
          <ShieldCheck size={16} /> 这里只呈现观察结果，不执行、安装或修改能力。
        </footer>
      </aside>
    </>
  )
}

function CollectionSourceDrawer({ entry, onClose }) {
  const item = entry.source
  const functionalType = collectionFunctionalType(item)
  const Icon = collectionTypeIcon(functionalType)
  const hostLabels = [...new Set(item.host_links.map((link) => HOST_LABELS[link.host_id] || link.host_id))]
  const capabilityCounts = item.capabilities.reduce((counts, capability) => {
    counts[capability.type] = (counts[capability.type] || 0) + 1
    return counts
  }, {})
  const origin = collectionOriginMeta(item)
  const availability = collectionAvailabilityMeta(item)
  const version = collectionVersionFor(entry)
  const versionState = collectionVersionStateMeta(entry, version)
  const versionRelations = collectionVersionRelations(entry)
  return (
    <>
      <button className="drawer-scrim" aria-label="关闭详情" onClick={onClose} />
      <aside className="detail-drawer" aria-label={`${item.name}详情`}>
        <header className="drawer-header">
          <button className="icon-button" onClick={onClose} aria-label="关闭详情"><X size={20} /></button>
        </header>
        <div className="drawer-content">
          <div className="drawer-title">
            <span className="asset-icon asset-skill"><Icon size={22} /></span>
            <div>
              <h2>{item.name}</h2>
              <span>{COLLECTION_KIND_LABELS[item.kind] || item.kind}</span>
            </div>
          </div>
          <div className="locked-banner"><LockKeyhole size={16} /> 锁定 · 只读</div>
          <DrawerSection title="分类摘要">
            <p>{item.classification.summary}</p>
          </DrawerSection>
          <DrawerSection title="详情">
            <dl className="identity-grid">
              <div><dt>来源</dt><dd><CollectionMetaBadge {...origin} focusable /></dd></div>
              <div><dt>版本</dt><dd>{version ? `v${version}` : '未声明'}</dd></div>
              <div><dt>状态</dt><dd><CollectionMetaBadge {...availability} focusable /></dd></div>
              <div><dt>版本状态</dt><dd><CollectionMetaBadge {...versionState} focusable /></dd></div>
              <div><dt>功能类型</dt><dd>{COLLECTION_FUNCTIONAL_TYPE_LABELS[functionalType] || functionalType}</dd></div>
              <div><dt>识别状态</dt><dd>{CLASSIFICATION_STATUS_LABELS[item.classification.status] || item.classification.status}</dd></div>
              <div><dt>置信度</dt><dd>{CLASSIFICATION_CONFIDENCE_LABELS[item.classification.confidence] || item.classification.confidence}</dd></div>
              <div><dt>就绪度</dt><dd>{READINESS_LABELS[item.classification.readiness] || item.classification.readiness}</dd></div>
              <div><dt>大小</dt><dd>{formatFileSize(item.size_bytes)}</dd></div>
              <div><dt>文件数</dt><dd>{item.shape.files} 个</dd></div>
              <div><dt>使用状态</dt><dd>{item.usage_status === 'unrecorded' ? '使用尚未记录' : item.usage_status}</dd></div>
              {item.classification.primary_scenario && <div><dt>主场景</dt><dd>{item.classification.primary_scenario}</dd></div>}
            </dl>
            <div className="collection-path-field"><span>路径</span><PathRow value={item.relative_path} /></div>
          </DrawerSection>
          <DrawerSection title="整理建议">
            <div className="collection-tag-line" style={{ marginTop: 0 }}>
              {item.move_safety === 'keep_anchor' ? (
                <span className="anchor-tag">原位保留 · {hostLabels.join(' / ')} 正在引用</span>
              ) : (
                <span>建议归入 {item.proposed_bucket.replaceAll('_', ' ')}</span>
              )}
              {item.classification.component_types.map((type) => (
                <span key={type}>{COLLECTION_TYPE_LABELS[type] || COLLECTION_COMPONENT_LABELS[type] || type}</span>
              ))}
            </div>
            {item.classification.basis?.length > 0 && (
              <p style={{ marginTop: 12 }}>{item.classification.basis.join('；')}</p>
            )}
          </DrawerSection>
          <CollectionVersionRelations
            relations={versionRelations}
            duplicateCount={item.duplicate_capability_count}
          />
          {item.capabilities.length > 0 && (
            <DrawerSection title={`内部能力 · ${item.capabilities.length} 项`}>
              <div className="collection-capability-summary">
                {Object.entries(capabilityCounts).map(([type, count]) => (
                  <span key={type}>{COLLECTION_COMPONENT_LABELS[type] || TYPE_LABELS[type] || type} {count}</span>
                ))}
              </div>
              <div className="collection-capability-list">
                {item.capabilities.map((capability) => (
                  <div key={capability.capability_id}>
                    <strong>{capability.name}</strong>
                    <span>{COLLECTION_COMPONENT_LABELS[capability.type] || TYPE_LABELS[capability.type] || capability.type}</span>
                    <small>
                      {ENTRY_STATUS_LABELS[capability.entry_status] || capability.entry_status}
                      {' · '}
                      {functionalType !== 'other_material' && capability.scenario ? `${capability.scenario} · ` : ''}
                      {capability.relative_path}
                    </small>
                  </div>
                ))}
              </div>
            </DrawerSection>
          )}
        </div>
        <footer className="drawer-footer">
          <ShieldCheck size={16} /> 这里只呈现观察结果，不执行、安装或修改能力。
        </footer>
      </aside>
    </>
  )
}

// ═══ 备选 skill 库（原 collection-workbench iframe，已迁入宿主 React）═══
// 数据源：/candidates/data.json（app.py 直接映射 collection-workbench/data.json）
// 决策与中文审校：只存浏览器 localStorage，不改动收藏夹；导入导出 decisions.json
const CANDIDATES_LS_KEY = 'skill-workbench:decisions:v1'
const CANDIDATE_BUCKETS = ['训练与测试', '知识库', '范例库', '模板', '参考资料', '素材', '脚本']
const CANDIDATE_ZH_LABEL = { missing: '待补中文', stale: '中文说明待复核', ai_draft: '中文草稿', reviewed: '中文已确认' }
const CANDIDATE_V_ORDER = { pending: 0, keep: 1, merge: 2, drop: 3 }
const CANDIDATE_FSEC = [['platform', '平台'], ['inputs', '输入'], ['outputs', '产出'], ['zh_state', '中文状态'], ['has', '特征']]
const CANDIDATE_DANGEROUS_KEYS = new Set(['__proto__', 'constructor', 'prototype'])

function candidateOwnMap(entries = []) {
  const output = Object.create(null)
  entries.forEach(([key, value]) => {
    if (typeof key === 'string' && !CANDIDATE_DANGEROUS_KEYS.has(key)) output[key] = value
  })
  return output
}

function sanitizeCandidateDecisions(raw, validUids = null) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return candidateOwnMap()
  const allowed = new Set(['pending', 'keep', 'drop', 'merge'])
  return candidateOwnMap(Object.entries(raw).flatMap(([uid, value]) => {
    if (
      CANDIDATE_DANGEROUS_KEYS.has(uid) ||
      (validUids && !validUids.has(uid)) ||
      !value || typeof value !== 'object' || Array.isArray(value) ||
      !allowed.has(value.verdict)
    ) return []
    const mergeInto = value.merge_into == null ? null : value.merge_into
    if (mergeInto !== null && (
      typeof mergeInto !== 'string' ||
      CANDIDATE_DANGEROUS_KEYS.has(mergeInto) ||
      (validUids && !validUids.has(mergeInto))
    )) return []
    const note = typeof value.note === 'string' ? value.note.slice(0, 2000) : ''
    const decidedAt = typeof value.decided_at === 'string' ? value.decided_at.slice(0, 80) : ''
    const anchor = value.anchor && typeof value.anchor === 'object' && !Array.isArray(value.anchor)
      ? {
          name: typeof value.anchor.name === 'string' ? value.anchor.name.slice(0, 240) : '',
          src: typeof value.anchor.src === 'string' ? value.anchor.src.slice(0, 1000) : '',
        }
      : undefined
    return [[uid, {
      verdict: value.verdict,
      merge_into: mergeInto,
      note,
      decided_at: decidedAt,
      ...(anchor ? { anchor } : {}),
    }]]
  }))
}

function sanitizeCandidateOverrides(raw, validUids = null) {
  if (!raw || typeof raw !== 'object' || Array.isArray(raw)) return candidateOwnMap()
  return candidateOwnMap(Object.entries(raw).flatMap(([uid, value]) => {
    if (
      CANDIDATE_DANGEROUS_KEYS.has(uid) ||
      (validUids && !validUids.has(uid)) ||
      !value || typeof value !== 'object' || Array.isArray(value)
    ) return []
    const clean = Object.create(null)
    if (typeof value.zh_name === 'string') clean.zh_name = value.zh_name.slice(0, 240)
    if (typeof value.zh_sum === 'string') clean.zh_sum = value.zh_sum.slice(0, 2000)
    for (const field of ['platform', 'inputs', 'outputs']) {
      if (Array.isArray(value[field])) {
        clean[field] = value[field]
          .filter((entry) => typeof entry === 'string')
          .slice(0, 50)
          .map((entry) => entry.slice(0, 160))
      }
    }
    if (typeof value.zh_reviewed === 'boolean') clean.zh_reviewed = value.zh_reviewed
    return [[uid, clean]]
  }))
}

function candidatesStorageKey(sourceKey = 'default') {
  return !sourceKey || sourceKey === 'default'
    ? CANDIDATES_LS_KEY
    : `${CANDIDATES_LS_KEY}:source:${sourceKey}`
}

function candidatesLoadLS(sourceKey = 'default') {
  try {
    const raw = localStorage.getItem(candidatesStorageKey(sourceKey))
    if (!raw) return { decisions: {}, overrides: {} }
    const j = JSON.parse(raw)
    return {
      decisions: sanitizeCandidateDecisions(j.decisions),
      overrides: sanitizeCandidateOverrides(j.overrides),
    }
  } catch { return { decisions: {}, overrides: {} } }
}
function candidatesSaveLS(decisions, overrides, sourceKey = 'default') {
  try {
    localStorage.setItem(candidatesStorageKey(sourceKey), JSON.stringify({
      schema_version: 1, updated_at: new Date().toISOString(), decisions, overrides,
    }))
  } catch { /* localStorage 写失败不阻塞 */ }
}

function CandidatesView({
  csrfToken,
  initialCatalog,
  onCatalog,
  collectionSourceRoot,
  sourceSession,
  sourceBusy,
  onChooseSource,
  onRestoreSource,
  focusUid,
  onFocusHandled,
}) {
  const sourceKey = sourceSession?.source_key || 'default'
  const [catalog, setCatalog] = useState(initialCatalog || null)
  const [loadError, setLoadError] = useState(null)
  const [refreshingCatalog, setRefreshingCatalog] = useState(false)
  const [decisions, setDecisions] = useState(() => candidatesLoadLS(sourceKey).decisions)
  const [overrides, setOverrides] = useState(() => candidatesLoadLS(sourceKey).overrides)
  const [selectedUid, setSelectedUid] = useState(null)
  const [query, setQuery] = useState('')
  const [sort, setSort] = useState('chars')
  const [onlyPending, setOnlyPending] = useState(false)
  const [filters, setFilters] = useState({ platform: new Set(), inputs: new Set(), outputs: new Set(), zh_state: new Set(), has: new Set() })
  const [showFilters, setShowFilters] = useState(false)
  const [compareSel, setCompareSel] = useState(new Set())
  const [showCompare, setShowCompare] = useState(false)
  const [notice, setNotice] = useState(null)
  const [importedSnapshot, setImportedSnapshot] = useState(null)
  const importRef = useRef(null)

  useEffect(() => {
    if (!initialCatalog) return
    setCatalog(initialCatalog)
    setLoadError(null)
  }, [initialCatalog])

  useEffect(() => {
    const stored = candidatesLoadLS(sourceKey)
    setDecisions(stored.decisions)
    setOverrides(stored.overrides)
    setSelectedUid(null)
    setCompareSel(new Set())
  }, [sourceKey])

  useEffect(() => {
    Promise.resolve(fetch('/candidates/data.json', { cache: 'no-store' }))
      .then((r) => { if (!r.ok) throw new Error(`HTTP ${r.status}`); return r.json() })
      .then((payload) => { setCatalog(payload); onCatalog?.(payload); setLoadError(null) })
      .catch((e) => setLoadError(e?.message || 'fetch unavailable'))
  }, [onCatalog, sourceKey])

  const refreshCatalog = useCallback(async () => {
    if (!csrfToken || refreshingCatalog || sourceBusy) return
    setRefreshingCatalog(true)
    setNotice({ tone: 'info', text: '正在只读重新扫描候选 Skill…' })
    try {
      const payload = await parseResponse(
        await fetch('/api/candidates/refresh', {
          method: 'POST',
          headers: {
            'Content-Type': 'application/json',
            'X-AI-Toolbox-CSRF': csrfToken,
          },
          body: '{}',
        }),
      )
      setCatalog(payload)
      onCatalog?.(payload)
      setLoadError(null)
      setNotice({ tone: 'success', text: `候选数据已刷新，共 ${payload.items?.length || 0} 个 Skill；原收藏文件未改动。` })
    } catch (error) {
      setNotice({
        tone: 'error',
        text: error.payload?.message || `候选数据刷新失败：${error.message}`,
      })
    } finally {
      setRefreshingCatalog(false)
    }
  }, [csrfToken, onCatalog, refreshingCatalog, sourceBusy])

  const persist = useCallback((nextDecisions, nextOverrides) => {
    setDecisions(nextDecisions)
    setOverrides(nextOverrides)
    candidatesSaveLS(nextDecisions, nextOverrides, sourceKey)
  }, [sourceKey])

  const catalogInvalid = Boolean(catalog && !Array.isArray(catalog.items))
  const items = useMemo(() => candidateDisplayItems(catalog), [catalog])
  const candidateSourceDir = typeof catalog?.source_dir === 'string' ? catalog.source_dir : ''
  const currentCollectionSource = typeof collectionSourceRoot === 'string' ? collectionSourceRoot : ''
  const sourceDisplayPath = sourceSession?.display_path || '来源路径未提供'
  const sourceMismatch = Boolean(
    catalog && currentCollectionSource && candidateSourceDir !== currentCollectionSource,
  )
  const byUid = useMemo(
    () => candidateOwnMap(items.map((item) => [item.uid, item])),
    [items],
  )

  useEffect(() => {
    if (!focusUid || !catalog) return
    if (!sourceMismatch && Object.hasOwn(byUid, focusUid)) setSelectedUid(focusUid)
    onFocusHandled?.()
  }, [byUid, catalog, focusUid, onFocusHandled, sourceMismatch])

  const groups = useMemo(() => {
    if (!Array.isArray(catalog?.groups)) return []
    const seenNames = new Set()
    return catalog.groups.flatMap((group) => {
      if (!group || typeof group !== 'object' || Array.isArray(group)) return []
      const name = typeof group.name === 'string' ? group.name.trim().slice(0, 240) : ''
      if (!name || seenNames.has(name) || CANDIDATE_DANGEROUS_KEYS.has(name)) return []
      seenNames.add(name)
      return [{
        ...group,
        name,
        desc: typeof group.desc === 'string' ? group.desc.slice(0, 2000) : '',
        competing: Boolean(group.competing),
      }]
    })
  }, [catalog])
  const facets = useMemo(() => {
    const sourceFacets = catalog?.facets && typeof catalog.facets === 'object'
      ? catalog.facets
      : {}
    const safeFacet = (dimension) => {
      if (!Array.isArray(sourceFacets[dimension])) return []
      const seenValues = new Set()
      return sourceFacets[dimension].flatMap((entry) => {
        if (!entry || typeof entry !== 'object' || Array.isArray(entry)) return []
        const value = typeof entry.value === 'string' ? entry.value.trim().slice(0, 240) : ''
        if (!value || seenValues.has(value) || CANDIDATE_DANGEROUS_KEYS.has(value)) return []
        seenValues.add(value)
        return [{ value, count: Number.isFinite(entry.count) && entry.count >= 0 ? entry.count : 0 }]
      })
    }
    const existingHas = Array.isArray(sourceFacets.has)
      ? safeFacet('has').filter((entry) => !['简介有问题', '简介解析异常', '简介已截短'].includes(entry.value))
      : []
    const parsingErrorCount = items.filter((item) => candidateDescriptionState(item).parsingError).length
    const truncatedCount = items.filter((item) => candidateDescriptionState(item).truncated).length
    return {
      platform: safeFacet('platform'),
      inputs: safeFacet('inputs'),
      outputs: safeFacet('outputs'),
      zh_state: safeFacet('zh_state'),
      has: [
        ...existingHas,
        ...(parsingErrorCount ? [{ value: '简介解析异常', count: parsingErrorCount }] : []),
        ...(truncatedCount ? [{ value: '简介已截短', count: truncatedCount }] : []),
      ],
    }
  }, [catalog, items])
  const maxc = items.length ? Math.max(...items.map((x) => x.chars)) : 1

  const ov = useCallback(
    (uid) => Object.hasOwn(overrides, uid) ? overrides[uid] : {},
    [overrides],
  )
  const verdictOf = useCallback(
    (uid) => Object.hasOwn(decisions, uid) ? decisions[uid]?.verdict || 'pending' : 'pending',
    [decisions],
  )
  const titleOf = useCallback((x) => ov(x.uid).zh_name || x.zh_name || x.name, [ov])
  const sumOf = useCallback((x) => { const o = ov(x.uid); return o.zh_sum != null ? o.zh_sum : x.zh_sum }, [ov])
  const platformOf = useCallback((x) => { const o = ov(x.uid); return o.platform != null ? o.platform : x.platform }, [ov])
  const inputsOf = useCallback((x) => { const o = ov(x.uid); return o.inputs != null ? o.inputs : x.inputs }, [ov])
  const outputsOf = useCallback((x) => { const o = ov(x.uid); return o.outputs != null ? o.outputs : x.outputs }, [ov])
  const zhStateOf = useCallback((x) => ov(x.uid).zh_reviewed ? 'reviewed' : x.zh_state, [ov])

  const setVerdict = useCallback((uid, verdict, mergeInto) => {
    const x = byUid[uid]
    if (!x) return
    const next = { ...decisions }
    const cur = next[uid]
    if (verdict === 'pending') delete next[uid]
    else if (cur && cur.verdict === verdict && verdict !== 'merge' && !mergeInto) delete next[uid]
    else next[uid] = {
      verdict, merge_into: verdict === 'merge' ? (mergeInto || null) : null,
      note: cur?.note || '', decided_at: new Date().toISOString(),
      anchor: { name: x.name, src: x.src },
    }
    persist(next, overrides)
  }, [byUid, decisions, overrides, persist])

  const setNote = useCallback((uid, text) => {
    const x = byUid[uid]
    if (!x) return
    const next = { ...decisions }
    const cur = next[uid]
    next[uid] = {
      verdict: cur?.verdict || 'pending', merge_into: cur?.merge_into || null,
      note: text, decided_at: cur?.decided_at || new Date().toISOString(),
      anchor: { name: x.name, src: x.src },
    }
    if (!text && next[uid].verdict === 'pending') delete next[uid]
    persist(next, overrides)
  }, [byUid, decisions, overrides, persist])

  const setOverride = useCallback((uid, field, value) => {
    const next = { ...overrides }
    const o = { ...(next[uid] || {}) }
    if (value == null || (Array.isArray(value) && !value.length)) delete o[field]
    else o[field] = value
    if (Object.keys(o).length) next[uid] = o; else delete next[uid]
    persist(decisions, next)
  }, [decisions, overrides, persist])

  const validMergeTargets = useCallback((uid) => {
    const g = byUid[uid]?.group
    return items.filter((x) => x.group === g && x.uid !== uid)
  }, [byUid, items])

  const conflicts = useMemo(() => {
    const errs = [], warns = []
    for (const x of items) {
      if (x.installed?.hosts?.length && verdictOf(x.uid) === 'drop')
        errs.push({ uid: x.uid, msg: `「${titleOf(x)}」已装在 ${x.installed.hosts.join('、')}，但你标了淘汰 —— 记得去宿主目录删掉` })
    }
    for (const [uid, dd] of Object.entries(decisions)) {
      if (dd.verdict !== 'merge') continue
      const x = byUid[uid]
      if (!x) continue
      const t = dd.merge_into
      if (t && !byUid[t]) errs.push({ uid, msg: `「${titleOf(x)}」的合并目标已不存在，请重选` })
      else if (t && verdictOf(t) === 'drop') errs.push({ uid, msg: `「${titleOf(x)}」要并入「${titleOf(byUid[t])}」，但后者被标了淘汰` })
      let cur = t, hops = 0, cyc = false
      const seen = new Set([uid])
      while (cur && hops < 60) {
        if (seen.has(cur)) { cyc = true; break }
        seen.add(cur)
        cur = decisions[cur]?.verdict === 'merge' ? decisions[cur].merge_into : null
        hops++
      }
      if (cyc) errs.push({ uid, msg: `「${titleOf(x)}」的合并链成环了（A→B→A），得挑一个当主` })
    }
    for (const g of groups) {
      if (!g.competing) continue
      const mem = items.filter((x) => x.group === g.name)
      if (mem.length >= 2 && mem.every((x) => verdictOf(x.uid) === 'drop'))
        warns.push({ msg: `「${g.name}」这组一个没留` })
      if (mem.length >= 2 && mem.every((x) => verdictOf(x.uid) === 'keep'))
        warns.push({ msg: `「${g.name}」这组全留了，可能还没收敛` })
    }
    const orphans = Object.entries(decisions).filter(([uid]) => !byUid[uid])
    if (orphans.length)
      warns.push({ orphan: true, msg: `有 ${orphans.length} 条决策找不到对应技能了（${orphans.slice(0, 3).map(([, dd]) => dd.anchor?.name || '?').join('、')}${orphans.length > 3 ? ' 等' : ''}）` })
    return { errs, warns }
  }, [items, decisions, byUid, groups, verdictOf, titleOf])

  const clearOrphanDecisions = useCallback(() => {
    const next = Object.fromEntries(Object.entries(decisions).filter(([uid]) => byUid[uid]))
    persist(next, overrides)
  }, [decisions, byUid, overrides, persist])

  const matchQ = useCallback((x) => {
    if (!query) return true
    const n = query.toLowerCase()
    return [titleOf(x), x.name, sumOf(x), x.desc, x.desc_full, x.src].some((v) => v && v.toLowerCase().includes(n))
  }, [query, titleOf, sumOf])

  const matchF = useCallback((x) => {
    const f = filters
    if (f.platform.size && !platformOf(x).some((v) => f.platform.has(v))) return false
    if (f.inputs.size && !inputsOf(x).some((v) => f.inputs.has(v))) return false
    if (f.outputs.size && !outputsOf(x).some((v) => f.outputs.has(v))) return false
    if (f.zh_state.size && !f.zh_state.has(zhStateOf(x))) return false
    if (f.has.size) {
      const descriptionState = candidateDescriptionState(x)
      const matchesFeature = [...f.has].some((value) => {
        if (value === '简介解析异常') return descriptionState.parsingError
        if (value === '简介已截短') return descriptionState.truncated
        return x.tags?.includes(value)
      })
      if (!matchesFeature) return false
    }
    if (onlyPending && verdictOf(x.uid) !== 'pending') return false
    return true
  }, [filters, onlyPending, platformOf, inputsOf, outputsOf, zhStateOf, verdictOf])

  const visible = useCallback((x) => matchQ(x) && matchF(x), [matchQ, matchF])

  const sortMem = useCallback((mem) => {
    const a = [...mem]
    if (sort === 'files') a.sort((p, q) => q.shape.total - p.shape.total)
    else if (sort === 'zh') a.sort((p, q) => titleOf(p).localeCompare(titleOf(q), 'zh-Hans-CN'))
    else if (sort === 'copies') a.sort((p, q) => q.copies - p.copies)
    else if (sort === 'dec') a.sort((p, q) => CANDIDATE_V_ORDER[verdictOf(p.uid)] - CANDIDATE_V_ORDER[verdictOf(q.uid)] || q.chars - p.chars)
    else a.sort((p, q) => q.chars - p.chars)
    return a
  }, [sort, titleOf, verdictOf])

  const decidedCount = items.filter((x) => verdictOf(x.uid) !== 'pending').length
  const unsavedCount = useMemo(() => {
    const ref = importedSnapshot?.decisions || {}
    return Object.entries(decisions).filter(([uid, dd]) => {
      const rd = ref[uid]
      return !rd || dd.verdict !== rd.verdict || dd.merge_into !== rd.merge_into || (dd.note || '') !== (rd.note || '')
    }).length
  }, [decisions, importedSnapshot])

  const download = useCallback((name, text, type = 'application/json') => {
    const b = new Blob([text], { type })
    const a = document.createElement('a')
    a.href = URL.createObjectURL(b); a.download = name
    document.body.appendChild(a); a.click(); a.remove()
    setTimeout(() => URL.revokeObjectURL(a.href), 4000)
  }, [])

  const doExport = useCallback(() => {
    const payload = { schema_version: 1, updated_at: new Date().toISOString(), decisions, overrides }
    download('decisions.json', JSON.stringify(payload, null, 2))
    const byV = { keep: [], drop: [], merge: [], pending: [] }
    items.forEach((x) => byV[verdictOf(x.uid)].push(x))
    const done = items.length - byV.pending.length
    const dt = new Date(); const pad = (n) => String(n).padStart(2, '0')
    const dateStr = `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}`
    let md = `# 技能清理清单\n生成于 ${dateStr} · 共 ${items.length} 个技能 · 已决策 ${done} 个\n`
    const gs = groups.map((g) => g.name)
    if (items.some((x) => x.group === '未归类')) gs.push('未归类')
    const sec = (title, arr, fn) => {
      if (!arr.length) return
      md += `\n## ${title}（${arr.length} 个）\n`
      gs.forEach((g) => {
        const mem = arr.filter((x) => x.group === g)
        if (!mem.length) return
        md += `\n### ${g}\n`; mem.forEach(fn)
      })
    }
    sec('保留', byV.keep, (x) => {
      md += `- **${titleOf(x)}** \`${x.name}\`${x.version ? ' v' + x.version : ''}\n`
      if (decisions[x.uid]?.note) md += `  - 备注：${decisions[x.uid].note}\n`
      md += `  - 位置：${x.src}\n`
    })
    sec('淘汰', byV.drop, (x) => {
      md += `- ~~${titleOf(x)}~~ \`${x.name}\`\n  - 位置：${x.src}${x.copies > 1 ? `（另有 ${x.copies - 1} 份副本）` : ''}\n`
      if (decisions[x.uid]?.note) md += `  - 备注：${decisions[x.uid].note}\n`
      if (x.installed?.hosts?.length) md += `  - ⚠️ 已装在 ${x.installed.hosts.join('、')}，需去对应宿主的 skills 目录删除\n`
    })
    if (byV.merge.length) {
      md += `\n## 合并（${byV.merge.length} 条）\n`
      byV.merge.forEach((x) => {
        const d = decisions[x.uid]; const t = d?.merge_into && byUid[d.merge_into]
        md += `- **${titleOf(x)}**（${x.src}）→ 并入 → **${t ? titleOf(t) : '（未选目标）'}**${t ? `（${t.src}）` : ''}\n`
      })
    }
    sec('待定', byV.pending, (x) => { md += `- ${titleOf(x)} \`${x.name}\` —— ${x.src}\n` })
    md += `\n---\n本清单由技能能力工作台导出，全程只读；删除文件请照此清单手动执行。\n`
    setTimeout(() => download('技能清理清单.md', md, 'text/markdown'), 300)
    setImportedSnapshot({ decisions: JSON.parse(JSON.stringify(decisions)) })
    setNotice({ tone: 'success', text: '已导出 decisions.json 与技能清理清单.md' })
    setTimeout(() => setNotice(null), 3500)
  }, [decisions, overrides, items, groups, byUid, verdictOf, titleOf, download])

  const doImport = useCallback((file) => {
    if (!file || file.size > 1024 * 1024) {
      setNotice({ tone: 'error', text: '导入失败：决策文件不得超过 1 MB。' })
      return
    }
    const r = new FileReader()
    r.onload = () => {
      try {
        const j = JSON.parse(r.result)
        if (
          !j || typeof j !== 'object' || Array.isArray(j) ||
          !j.decisions || typeof j.decisions !== 'object' || Array.isArray(j.decisions) ||
          Object.keys(j).some((key) => !['schema_version', 'updated_at', 'decisions', 'overrides'].includes(key))
        ) throw new Error('bad')
        const validUids = new Set(items.map((item) => item.uid))
        const importedD = sanitizeCandidateDecisions(j.decisions, validUids)
        const importedO = sanitizeCandidateOverrides(j.overrides, validUids)
        if (Object.keys(importedD).length !== Object.keys(j.decisions).length) throw new Error('bad')
        if (j.overrides && Object.keys(importedO).length !== Object.keys(j.overrides).length) throw new Error('bad')
        const nextD = candidateOwnMap(Object.entries(decisions)); let n = 0
        for (const [uid, fd] of Object.entries(importedD)) {
          const cur = nextD[uid]
          if (!cur || (fd.decided_at || '') > (cur.decided_at || '')) { nextD[uid] = fd; n++ }
        }
        const nextO = candidateOwnMap(Object.entries(overrides))
        for (const [uid, fo] of Object.entries(importedO)) {
          if (!Object.hasOwn(nextO, uid)) nextO[uid] = fo
        }
        persist(nextD, nextO)
        setImportedSnapshot(j)
        setNotice({ tone: 'success', text: n ? `已合并 ${n} 条来自文件的决策` : '文件里的决策都已是最新' })
        setTimeout(() => setNotice(null), 3500)
      } catch {
        setNotice({ tone: 'error', text: '导入失败：这不是合法的 decisions.json' })
        setTimeout(() => setNotice(null), 3500)
      }
    }
    r.readAsText(file)
  }, [decisions, items, overrides, persist])

  const toggleCompare = useCallback((uid) => {
    setCompareSel((cur) => {
      const next = new Set(cur)
      if (next.has(uid)) next.delete(uid)
      else if (next.size < 4) next.add(uid)
      return next
    })
  }, [])

  if (catalogInvalid) {
    return (
      <section className="candidates-view">
        <EmptyState
          icon={CircleAlert}
          title="备选库数据格式不完整"
          detail="当前候选快照没有合法的 items 数组，已停止展示并且未纳入系统体检。"
          action={refreshingCatalog ? '刷新中…' : '刷新候选数据'}
          onAction={refreshCatalog}
        />
      </section>
    )
  }
  if (loadError) {
    return (
      <section className="candidates-view">
        {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}
        <div className="candidate-source-empty-actions">
          <button
            className="secondary-button"
            onClick={(event) => onChooseSource('candidates', event.currentTarget)}
            disabled={sourceBusy}
          >
            <FolderOpen size={17} /> 选择其他收藏文件夹
          </button>
          {sourceSession?.mode === 'temporary' && (
            <button className="secondary-button" onClick={onRestoreSource} disabled={sourceBusy}>恢复默认来源</button>
          )}
        </div>
        <EmptyState
          icon={CircleAlert}
          title="备选库数据没读到"
          detail={`可以直接在这里重新建立候选索引。错误：${loadError}`}
          action={refreshingCatalog ? '刷新中…' : '刷新候选数据'}
          onAction={refreshCatalog}
        />
      </section>
    )
  }
  if (!catalog) {
    return (
      <section className="candidates-view">
        <div className="candidate-source-empty-actions">
          <button
            className="secondary-button"
            onClick={(event) => onChooseSource('candidates', event.currentTarget)}
            disabled={sourceBusy}
          >
            <FolderOpen size={17} /> 选择其他收藏文件夹
          </button>
        </div>
        <div className="collection-loading" aria-label="正在读取备选 skill 库">
          <div className="skeleton skeleton-strip" />
          <div className="skeleton skeleton-panel" />
        </div>
      </section>
    )
  }

  if (sourceMismatch) {
    return (
      <section className="candidates-view">
        <Notice tone="error">候选索引来源与当前会话收藏来源不一致，已停止展示，避免把两个目录的决策混在一起。</Notice>
        <div className="candidate-source-empty-actions">
          <button
            className="secondary-button"
            onClick={(event) => onChooseSource('candidates', event.currentTarget)}
            disabled={sourceBusy}
          >
            <FolderOpen size={17} /> 重新选择文件夹
          </button>
          {sourceSession?.mode === 'temporary' && (
            <button className="secondary-button" onClick={onRestoreSource} disabled={sourceBusy}>恢复默认来源</button>
          )}
        </div>
      </section>
    )
  }

  const activeFilterCount = CANDIDATE_FSEC.reduce((n, [d]) => n + filters[d].size, 0)
  const selected = selectedUid ? byUid[selectedUid] : null

  return (
    <section className="candidates-view candidates-native">
      <div className="view-intro candidates-intro">
        <div>
          <p className="section-kicker">留 / 砍 / 并</p>
          <h2 aria-live="polite">{items.length} 个备选 Skill</h2>
          <p>把同类技能收敛成清晰的保留方案；决策只保存在当前浏览器，可随时导出。</p>
        </div>
      </div>

      <div className="candidate-source-line">
        <ShieldCheck size={17} />
        <span>只读来源 · {sourceSession?.mode === 'temporary' ? '临时自选' : '默认目录'}</span>
        <code>{sourceDisplayPath}</code>
        {sourceSession?.mode === 'temporary' && (
          <button className="source-restore-button" onClick={onRestoreSource} disabled={sourceBusy}>恢复默认</button>
        )}
      </div>

      <div className="candidates-toolbar">
        <div className="candidates-search">
          <Search size={16} />
          <input
            type="text" placeholder="搜索技能名、简介…" value={query}
            onChange={(e) => setQuery(e.target.value)} aria-label="搜索备选技能"
          />
          {query && <button className="icon-button" onClick={() => setQuery('')} aria-label="清除搜索"><X size={15} /></button>}
        </div>
        <button className={`secondary-button${activeFilterCount ? ' has-active' : ''}`} onClick={() => setShowFilters((v) => !v)} aria-expanded={showFilters}>
          <ListFilter size={15} /> 筛选{activeFilterCount ? ` · ${activeFilterCount}` : ''}
        </button>
        <select className="candidates-sort" value={sort} onChange={(e) => setSort(e.target.value)} aria-label="组内排序">
          <option value="chars">篇幅（默认，降序）</option>
          <option value="files">包体文件数</option>
          <option value="zh">中文名 A→Z</option>
          <option value="copies">副本数</option>
          <option value="dec">决策状态</option>
        </select>
        <button
          className={`secondary-button${onlyPending ? ' has-active' : ''}`}
          onClick={() => setOnlyPending((v) => !v)} aria-pressed={onlyPending}
          title="只看还没定的"
        >
          待定 {items.length - decidedCount}/{items.length}
        </button>
        <span className="candidates-toolbar-spacer" />
        <button
          className="secondary-button candidate-source-button"
          onClick={(event) => onChooseSource('candidates', event.currentTarget)}
          disabled={sourceBusy || refreshingCatalog}
          title="先查看选择原因和只读范围，再打开系统文件夹选择器"
        >
          <FolderOpen size={16} />
          <span>选择文件夹</span>
        </button>
        <button
          className="secondary-button candidate-refresh"
          onClick={refreshCatalog}
          disabled={!csrfToken || refreshingCatalog || sourceBusy}
          aria-label="刷新候选数据"
          title="只读重扫收藏夹，不改原文件"
        >
          <RefreshCw size={16} className={refreshingCatalog ? 'spin' : ''} />
          <span>{refreshingCatalog ? '刷新中…' : '刷新候选数据'}</span>
        </button>
        <button className="secondary-button" onClick={() => importRef.current?.click()}>导入</button>
        <button className={`refresh-button${unsavedCount > 0 ? ' has-unsaved' : ''}`} onClick={doExport} title={unsavedCount ? `有 ${unsavedCount} 条决策尚未导出` : '导出 decisions.json + 技能清理清单.md'}>
          导出{unsavedCount > 0 ? ` · ${unsavedCount}` : ''}
        </button>
        <input ref={importRef} type="file" accept="application/json,.json" style={{ display: 'none' }}
          onChange={(e) => { if (e.target.files?.[0]) doImport(e.target.files[0]); e.target.value = '' }} />
      </div>

      {showFilters && (
        <div className="candidates-filter-panel">
          {CANDIDATE_FSEC.map(([dim, label]) => (
            <div className="candidates-filter-sec" key={dim}>
              <div className="candidates-filter-label">{label}</div>
              <div className="candidates-filter-opts">
                {(facets[dim] || []).map(({ value, count }) => {
                  const on = filters[dim].has(value)
                  const lab = dim === 'zh_state' ? (CANDIDATE_ZH_LABEL[value] || value) : value
                  return (
                    <button
                      key={value} disabled={count === 0}
                      className={`candidates-filter-opt${on ? ' on' : ''}`}
                      onClick={() => setFilters((cur) => {
                        const next = { ...cur, [dim]: new Set(cur[dim]) }
                        if (on) next[dim].delete(value); else next[dim].add(value)
                        return next
                      })}
                    >
                      {lab}<span className="candidates-filter-count">{count}</span>
                    </button>
                  )
                })}
              </div>
            </div>
          ))}
          {activeFilterCount > 0 && (
            <button className="text-button" onClick={() => setFilters({ platform: new Set(), inputs: new Set(), outputs: new Set(), zh_state: new Set(), has: new Set() })}>
              <X size={14} /> 清除全部筛选
            </button>
          )}
        </div>
      )}

      {notice && <Notice tone={notice.tone}>{notice.text}</Notice>}

      {(conflicts.errs.length > 0 || conflicts.warns.length > 0) && (
        <div className={`candidates-conflicts${conflicts.errs.length ? ' has-error' : ''}`}>
          <AlertTriangle size={16} />
          <div>
            <strong>{conflicts.errs.length ? `冲突 ${conflicts.errs.length}` : `提醒 ${conflicts.warns.length}`}</strong>
            <ul>
              {conflicts.errs.map((c, i) => (
                <li key={`e${i}`}>{c.uid ? <button className="candidates-conflict-link" onClick={() => setSelectedUid(c.uid)}>{c.msg}</button> : c.msg}</li>
              ))}
              {conflicts.warns.map((c, i) => (
                <li key={`w${i}`}>{c.msg}{c.orphan && <button className="text-button" style={{ marginLeft: 8 }} onClick={clearOrphanDecisions}>清除失效决策</button>}</li>
              ))}
            </ul>
          </div>
        </div>
      )}

      <div className="candidates-groups">
        {groups.map((g) => {
          const mem = items.filter((x) => x.group === g.name && visible(x))
          if (!mem.length) return null
          const done = items.filter((x) => x.group === g.name && verdictOf(x.uid) !== 'pending').length
          const total = items.filter((x) => x.group === g.name).length
          const relation = scenarioGuideFor(g.name, total)
          return (
            <section className="candidates-group" key={g.name}>
              <div className="candidates-group-heading">
                <h3>{g.name}</h3>
                <span className={`candidates-group-badge relation-${relation.relationKind}`}>
                  {relation.relationLabel} {total} 个
                </span>
                {done > 0 && <span className="candidates-group-prog">{done === total ? '✓ 已完成' : `已决策 ${done}/${total}`}</span>}
              </div>
              <p className="candidates-group-desc">{g.desc}</p>
              <div className="candidates-card-grid">
                {sortMem(mem).map((x) => (
                  <CandidateCard
                    key={x.uid} item={x} verdict={verdictOf(x.uid)} decision={decisions[x.uid]}
                    titleOf={titleOf} sumOf={sumOf} platformOf={platformOf} zhStateOf={zhStateOf}
                    isOv={(f) => ov(x.uid)[f] != null} maxc={maxc}
                    selected={compareSel.has(x.uid)}
                    mergeTargets={validMergeTargets(x.uid)}
                    onOpen={() => setSelectedUid(x.uid)}
                    onToggleCompare={() => toggleCompare(x.uid)}
                    onVerdict={(v, m) => setVerdict(x.uid, v, m)}
                  />
                ))}
              </div>
            </section>
          )
        })}
        {items.filter((x) => x.group === '未归类' && visible(x)).length > 0 && (
          <section className="candidates-group">
            <div className="candidates-group-heading">
              <h3>未归类</h3>
              <span className="candidates-group-badge relation-unclassified">待你处理 {items.filter((x) => x.group === '未归类').length} 个</span>
            </div>
            <p className="candidates-group-desc">规则按技能名判断，名字里没线索的会落到这里。</p>
            <div className="candidates-card-grid">
              {sortMem(items.filter((x) => x.group === '未归类' && visible(x))).map((x) => (
                <CandidateCard
                  key={x.uid} item={x} verdict={verdictOf(x.uid)} decision={decisions[x.uid]}
                  titleOf={titleOf} sumOf={sumOf} platformOf={platformOf} zhStateOf={zhStateOf}
                  isOv={(f) => ov(x.uid)[f] != null} maxc={maxc}
                  selected={compareSel.has(x.uid)}
                  mergeTargets={validMergeTargets(x.uid)}
                  onOpen={() => setSelectedUid(x.uid)}
                  onToggleCompare={() => toggleCompare(x.uid)}
                  onVerdict={(v, m) => setVerdict(x.uid, v, m)}
                />
              ))}
            </div>
          </section>
        )}
      </div>

      {compareSel.size > 0 && (
        <div className="candidates-compare-bar">
          <span>已选 {compareSel.size} 个{compareSel.size >= 4 ? '（最多 4 个）' : ''}</span>
          <button className="secondary-button" disabled={compareSel.size < 2} onClick={() => setShowCompare(true)}>并排对比</button>
          <button className="text-button" onClick={() => setCompareSel(new Set())}>清空</button>
        </div>
      )}

      {selected && (
        <CandidateDrawer
          item={selected} decision={decisions[selected.uid]} override={ov(selected.uid)}
          byUid={byUid} facets={facets} validMergeTargets={validMergeTargets(selected.uid)}
          onClose={() => setSelectedUid(null)}
          onVerdict={(v, m) => setVerdict(selected.uid, v, m)}
          onNote={(t) => setNote(selected.uid, t)}
          onOverride={(f, val) => setOverride(selected.uid, f, val)}
          onGoto={(uid) => setSelectedUid(uid)}
        />
      )}
      {showCompare && compareSel.size >= 2 && (
        <CandidateComparePanel
          items={[...compareSel].map((uid) => byUid[uid]).filter(Boolean)}
          titleOf={titleOf} sumOf={sumOf}
          onClose={() => setShowCompare(false)}
          onGoto={(uid) => { setShowCompare(false); setSelectedUid(uid) }}
        />
      )}
    </section>
  )
}

function CandidateCard({ item, verdict, decision, titleOf, sumOf, platformOf, zhStateOf, isOv, maxc, selected, mergeTargets, onOpen, onToggleCompare, onVerdict }) {
  const x = item
  const v = verdict
  const descriptionState = candidateDescriptionState(x)
  const [showMerge, setShowMerge] = useState(false)
  const mergeTarget = decision?.merge_into
  return (
    <article className={`candidate-card v-${v}${selected ? ' is-selected' : ''}`}>
      <button className="candidate-card-main" onClick={onOpen} aria-label={`查看 ${titleOf(x)} 详情`}>
        <span className="candidate-card-title">{titleOf(x)}</span>
        <span className="candidate-card-name">{x.name}{x.version ? ` · v${x.version}` : ''}</span>
        <span className="candidate-card-desc">{sumOf(x) || x.desc}</span>
        <span className="candidate-card-tags">
          {zhStateOf(x) === 'missing' && <span className="cand-tag is-warn">待补中文</span>}
          {zhStateOf(x) === 'stale' && <span className="cand-tag is-warn">中文说明待复核</span>}
          {zhStateOf(x) === 'reviewed' && <span className="cand-tag is-ok">中文已确认</span>}
          {platformOf(x).slice(0, 3).map((p) => <span key={p} className={`cand-tag${isOv('platform') ? ' is-ov' : ''}`}>{p}</span>)}
          {x.tags?.includes('含脚本') && <span className="cand-tag">含脚本</span>}
          {x.installed?.hosts?.length > 0 && <span className="cand-tag is-inst">已装 · {x.installed.hosts.join(' ')}</span>}
          {x.copies > 1 && <span className="cand-tag">{x.copies} 份副本</span>}
          {descriptionState.parsingError && <span className="cand-tag is-warn">简介解析异常</span>}
          {descriptionState.truncated && <span className="cand-tag">简介已截短</span>}
        </span>
        <span className="candidate-card-meter"><i style={{ width: `${Math.round(x.chars / (maxc || 1) * 100)}%` }} /></span>
      </button>
      <footer className="candidate-card-footer">
        <div className="candidate-verdicts">
          <button className={`cand-vb k${v === 'keep' ? ' on' : ''}`} onClick={() => onVerdict('keep')} aria-pressed={v === 'keep'}>留</button>
          <button className={`cand-vb d${v === 'drop' ? ' on' : ''}`} onClick={() => onVerdict('drop')} aria-pressed={v === 'drop'}>砍</button>
          <button
            className={`cand-vb m${v === 'merge' ? ' on' : ''}`} disabled={!mergeTargets.length}
            title={mergeTargets.length ? '并入同组另一个技能' : '这组没有别的技能可并'}
            onClick={() => setShowMerge((s) => !s)} aria-pressed={v === 'merge'}
          >并</button>
        </div>
        <span className={`candidate-vstat is-${v}`}>
          {v === 'keep' && '已留'}
          {v === 'drop' && '已砍'}
          {v === 'merge' && (mergeTarget ? '已标并' : '并入（未选目标）')}
          {v === 'pending' && '待定'}
        </span>
        <button
          className={`candidate-compare-tick${selected ? ' on' : ''}`} onClick={onToggleCompare}
          aria-pressed={selected} title={selected ? '移出对比' : '加入对比'}
        ><Check size={14} /></button>
      </footer>
      {showMerge && mergeTargets.length > 0 && (
        <div className="candidate-merge-pop">
          {mergeTargets.sort((a, b) => b.chars - a.chars).map((t) => (
            <button key={t.uid} onClick={() => { onVerdict('merge', t.uid); setShowMerge(false) }}>
              <strong>{titleOf(t)}</strong>
              <small>{t.name} · {(t.chars / 10000).toFixed(1)} 万字</small>
            </button>
          ))}
        </div>
      )}
    </article>
  )
}

function CandidateDrawer({ item, decision, override, byUid, facets, validMergeTargets, onClose, onVerdict, onNote, onOverride, onGoto }) {
  const x = item
  const v = decision?.verdict || 'pending'
  const descriptionState = candidateDescriptionState(x)
  const [showMerge, setShowMerge] = useState(false)
  const zhState = override.zh_reviewed ? 'reviewed' : x.zh_state
  const guessRow = (field, label, current) => {
    const all = (facets[field] || []).map((o) => o.value)
    const covered = override[field] != null
    return (
      <div className="candidate-guess-row" key={field}>
        <b>{label}</b>
        <span className="candidate-guess-tags">
          {all.map((val) => {
            const on = current.includes(val)
            return (
              <button key={val} className={`cand-gt${on ? ' on' : ''}`}
                onClick={() => {
                  const next = on ? current.filter((c) => c !== val) : [...current, val]
                  onOverride(field, next.length ? next : null)
                }}>{val}</button>
            )
          })}
          {covered && <button className="cand-greset" onClick={() => onOverride(field, null)}>恢复自动判断</button>}
        </span>
      </div>
    )
  }
  return (
    <>
      <button className="drawer-scrim" aria-label="关闭详情" onClick={onClose} />
      <aside className="detail-drawer" aria-label={`${override.zh_name || x.zh_name || x.name}详情`}>
        <header className="drawer-header">
          <button className="icon-button" onClick={onClose} aria-label="关闭详情"><X size={20} /></button>
          <span className="drawer-label"><Database size={16} /> 备选 skill</span>
        </header>
        <div className="drawer-content">
          <div className="drawer-title">
            <span className="asset-icon asset-skill"><Sparkles size={22} /></span>
            <div>
              <h2>{override.zh_name || x.zh_name || x.name}</h2>
              <span>{x.name}{x.version ? ` · v${x.version}` : ''}</span>
            </div>
          </div>

          <DrawerSection title="决策">
            <div className="candidate-drawer-verdicts">
              <button className={`cand-vb k${v === 'keep' ? ' on' : ''}`} onClick={() => onVerdict('keep')}>留</button>
              <button className={`cand-vb d${v === 'drop' ? ' on' : ''}`} onClick={() => onVerdict('drop')}>砍</button>
              <button className={`cand-vb m${v === 'merge' ? ' on' : ''}`} disabled={!validMergeTargets.length}
                onClick={() => setShowMerge((s) => !s)}>并</button>
            </div>
            {showMerge && validMergeTargets.length > 0 && (
              <div className="candidate-merge-pop is-drawer">
                {validMergeTargets.sort((a, b) => b.chars - a.chars).map((t) => (
                  <button key={t.uid} onClick={() => { onVerdict('merge', t.uid); setShowMerge(false) }}>
                    <strong>{t.zh_name || t.name}</strong>
                    <small>{t.name} · {(t.chars / 10000).toFixed(1)} 万字</small>
                  </button>
                ))}
              </div>
            )}
            {v === 'merge' && decision?.merge_into && byUid[decision.merge_into] && (
              <p style={{ marginTop: 10 }}>并入 → <button className="candidates-conflict-link" onClick={() => onGoto(decision.merge_into)}>{byUid[decision.merge_into].zh_name || byUid[decision.merge_into].name}</button></p>
            )}
            <textarea
              className="candidate-note" placeholder="备注：为什么留/砍/并…（失焦自动存）"
              defaultValue={decision?.note || ''} key={x.uid}
              onBlur={(e) => onNote(e.target.value.trim())}
            />
          </DrawerSection>

          <DrawerSection title="完整简介（原文）">
            {descriptionState.parsingError
              ? <p style={{ color: '#946000' }}>⚠️ 这个包的简介解析异常，需要点开原包核对正文</p>
              : <p>{x.desc_full || x.desc || ''}</p>}
            {x.zh_state === 'stale' && (
              <p style={{ marginTop: 6, color: '#946000' }}>
                候选快照中的原包内容已变更，中文说明待复核
                {override.zh_reviewed ? '；当前浏览器已标记确认，但尚未回写候选快照' : ''}
              </p>
            )}
            {x.zh_state === 'missing' && override.zh_reviewed && (
              <p style={{ marginTop: 6, color: '#946000' }}>
                候选快照仍记录为中文说明缺失；当前浏览器的确认尚未回写候选快照
              </p>
            )}
          </DrawerSection>

          <DrawerSection title="中文审校（改动只进 decisions，不回写元数据）">
            <div className="candidate-zh-field">
              <b>中文名</b>
              <textarea className="candidate-zh-input" rows={1} key={`n${x.uid}`}
                defaultValue={override.zh_name != null ? override.zh_name : (x.zh_name || '')}
                onBlur={(e) => {
                  const val = e.target.value.trim()
                  onOverride('zh_name', val === (x.zh_name || '') ? null : (val || null))
                }} />
            </div>
            <div className="candidate-zh-field">
              <b>中文摘要</b>
              <textarea className="candidate-zh-input" rows={3} key={`s${x.uid}`}
                defaultValue={override.zh_sum != null ? override.zh_sum : (x.zh_sum || '')}
                onBlur={(e) => {
                  const val = e.target.value.trim()
                  onOverride('zh_sum', val === (x.zh_sum || '') ? null : (val || null))
                }} />
            </div>
            <div className="candidate-zh-status">
              <button className="cand-greset" style={{ fontSize: 12.5, color: override.zh_reviewed ? '#15803d' : 'var(--blue)' }}
                onClick={() => onOverride('zh_reviewed', override.zh_reviewed ? null : true)}>
                {override.zh_reviewed ? '✓ 已确认' : '✓ 确认这段中文没问题'}
              </button>
              <span className="candidate-zh-stat">
                {override.zh_reviewed ? '已确认'
                  : (override.zh_name != null || override.zh_sum != null) ? '已修改，待确认'
                  : zhState === 'ai_draft' ? 'AI 草稿，未确认'
                  : zhState === 'stale' ? '中文说明待复核'
                  : zhState === 'missing' ? '待补中文' : ''}
              </span>
            </div>
          </DrawerSection>

          <DrawerSection title="推测属性（点标签切换，写进覆盖）">
            {guessRow('platform', '平台', override.platform != null ? override.platform : (x.platform || []))}
            {guessRow('inputs', '输入', override.inputs != null ? override.inputs : (x.inputs || []))}
            {guessRow('outputs', '产出', override.outputs != null ? override.outputs : (x.outputs || []))}
          </DrawerSection>

          <DrawerSection title="包体信息">
            <dl className="identity-grid">
              <div><dt>带料</dt><dd>{CANDIDATE_BUCKETS.filter((b) => x.shape?.[b] > 0).map((b) => `${b} ${x.shape[b]}`).join('、') || '只有一个 SKILL.md'}</dd></div>
              <div><dt>文件数</dt><dd>{x.shape?.total} 个</dd></div>
              <div><dt>正文</dt><dd>{x.chars.toLocaleString()} 字符（约 {(x.chars / 10000).toFixed(1)} 万字）</dd></div>
            </dl>
            <PathRow value={x.src} />
            {x.inner && <PathRow value={`包内：${x.inner}`} />}
          </DrawerSection>

          {x.copies > 1 && (
            <DrawerSection title={`重复副本（${x.copies} 份，内容完全相同，留一份即可）`}>
              {x.copy_srcs.map((s) => <PathRow value={s} key={s} />)}
            </DrawerSection>
          )}

          {x.installed?.hosts?.length > 0 && (
            <DrawerSection title="安装状态">
              {x.installed.hosts.map((h) => (
                <p key={h}>已装到 <strong>{h}</strong>{x.installed.as_name ? `（目录名 ${x.installed.as_name}）` : ''}</p>
              ))}
            </DrawerSection>
          )}
        </div>
        <footer className="drawer-footer">
          <ShieldCheck size={16} /> 决策只保存在当前浏览器，导出 decisions.json 后照清单手动整理。
        </footer>
      </aside>
    </>
  )
}

function CandidateComparePanel({ items, titleOf, sumOf, onClose, onGoto }) {
  return (
    <>
      <button className="drawer-scrim" aria-label="关闭对比" onClick={onClose} style={{ zIndex: 75 }} />
      <div className="candidate-compare-panel" role="dialog" aria-modal="true" aria-label="技能并排对比">
        <header className="drawer-header">
          <span className="drawer-label"><Database size={16} /> 并排对比 · {items.length} 个</span>
          <button className="icon-button" onClick={onClose} aria-label="关闭对比"><X size={20} /></button>
        </header>
        <div className="candidate-compare-grid">
          {items.map((x) => (
            <div className="candidate-compare-col" key={x.uid}>
              <button className="candidates-conflict-link" onClick={() => onGoto(x.uid)}>
                <h3>{titleOf(x)}</h3>
              </button>
              <small>{x.name}{x.version ? ` · v${x.version}` : ''}</small>
              <p>{sumOf(x) || x.desc}</p>
              {x.desc_full && x.desc_full !== (sumOf(x) || x.desc) && (
                <details><summary>看原文</summary><p>{x.desc_full}</p></details>
              )}
              <dl className="identity-grid">
                <div><dt>篇幅</dt><dd>{(x.chars / 10000).toFixed(1)} 万字</dd></div>
                <div><dt>文件</dt><dd>{x.shape?.total} 个</dd></div>
                <div><dt>副本</dt><dd>{x.copies} 份</dd></div>
                <div><dt>已装</dt><dd>{x.installed?.hosts?.length ? x.installed.hosts.join('、') : '未装'}</dd></div>
              </dl>
              <PathRow value={x.src} />
            </div>
          ))}
        </div>
      </div>
    </>
  )
}

function ToolsView(props) {
  const typeCounts = Object.fromEntries(
    ['plugin', 'mcp', 'cli', 'sdk'].map((type) => [
      type,
      props.totalItems.filter((item) => item.type === type).length,
    ]),
  )
  const currentCoverage = coverageNotice(props.snapshot, props.toolType, props.hostFilter)
  const hostsForType = props.hosts.map((host) => ({
    ...host,
    assetCount: host.countsByType?.[props.toolType] || 0,
  }))
  return (
    <section className="library-view">
      <div className="view-intro">
        <div>
          <p className="section-kicker">工具资产</p>
          <h2>Plugin 与扩展接口</h2>
          <p>按“宿主 × 类型”核对覆盖事实；数量只表示白名单边界内的观察结果。</p>
        </div>
      </div>
      <div className="type-tabs" role="tablist" aria-label="工具类型">
        {['plugin', 'mcp', 'cli', 'sdk'].map((type) => (
          <button
            key={type}
            role="tab"
            aria-selected={props.toolType === type}
            className={props.toolType === type ? 'active' : ''}
            onClick={() => props.onType(type)}
          >
            {TYPE_LABELS[type]}
            <span>{typeCounts[type]}</span>
          </button>
        ))}
      </div>
      <Notice tone={currentCoverage.tone}>{currentCoverage.message}</Notice>
      <FilterBar
        hosts={hostsForType}
        hostFilter={props.hostFilter}
        onHostFilter={props.onHostFilter}
        subcategories={props.subcategories}
        subcategoryFilter={props.subcategoryFilter}
        onSubcategoryFilter={props.onSubcategoryFilter}
        favoriteOnly={props.favoriteOnly}
        onFavoriteOnly={props.onFavoriteOnly}
        multiHost={props.multiHost}
        onMultiHost={props.onMultiHost}
      />
      {props.items.length ? (
        <GroupedAssetSections
          items={props.items}
          allItems={props.allItems}
          favorites={props.favorites}
          findings={props.findings}
          onSelectItem={props.onSelectItem}
          onToggleFavorite={props.onToggleFavorite}
        />
      ) : (
        <EmptyState
          title={`没有符合条件的 ${TYPE_LABELS[props.toolType]}`}
          detail="请先核对上方覆盖提示；无结果可能是筛选所致，也可能是该宿主尚未接入观察源。"
          action="清除筛选"
          onAction={props.onClear}
        />
      )}
    </section>
  )
}

function GroupedAssetSections({
  items,
  allItems,
  favorites,
  findings,
  onSelectItem,
  onToggleFavorite,
}) {
  const groups = useMemo(() => groupAssetsByScenario(items), [items])
  return (
    <div className="scenario-collection">
      <div className="relationship-guide" role="note">
        <Info size={17} />
        <div>
          <strong>严格关系怎么读</strong>
          <span>
            “备选”表示同一任务任选其一；“冲突”表示同时使用会碰撞；“互补”表示职责不同且组合增值；“混合关系”表示组内存在多种关系。
          </span>
          <small>“单项”不建立关系；“关系未判定”只表示已经归组。依赖、重复和独立必须有对象级证据，不能从同场景自动推断。</small>
        </div>
      </div>
      {groups.map((group, index) => (
        <ScenarioSection
          key={group.subcategory}
          group={group}
          index={index}
          allItems={allItems}
          favorites={favorites}
          findings={findings}
          onSelectItem={onSelectItem}
          onToggleFavorite={onToggleFavorite}
        />
      ))}
    </div>
  )
}

function ScenarioSection({
  group,
  index,
  allItems,
  favorites,
  findings,
  onSelectItem,
  onToggleFavorite,
}) {
  const context = useMemo(
    () => scenarioContextFor(group.items, allItems, group.subcategory),
    [group.items, allItems, group.subcategory],
  )
  const headingId = `scenario-heading-${index}`
  const currentCounts = Object.entries(context.currentCounts).filter(([, count]) => count > 0)
  const relatedCounts = Object.entries(context.relatedCounts).filter(([, count]) => count > 0)
  const relatedPreview = context.related.slice(0, 6)

  return (
    <section className="scenario-section" aria-labelledby={headingId}>
      <header className="scenario-heading">
        <div className="scenario-heading-main">
          <span className="scenario-parent" title={group.categories.join(' · ')}>
            {group.categories.join(' · ') || '未归入大类'}
          </span>
          <div className="scenario-title-row">
            <h3 id={headingId}>{group.subcategory}</h3>
            <span className={`scenario-relation relation-${context.guide.relationKind}`}>
              {context.guide.relationLabel}
            </span>
          </div>
          <p>{context.guide.description}</p>
        </div>
        <div className="scenario-counts" aria-label="当前分组和相关能力数量">
          {currentCounts.map(([type, count]) => (
            <span key={`current-${type}`} className="current-count">
              当前 {TYPE_LABELS[type]} {count}
            </span>
          ))}
          {relatedCounts.map(([type, count]) => (
            <span key={`related-${type}`}>
              相关 {TYPE_LABELS[type]} {count}
            </span>
          ))}
        </div>
      </header>

      <div className="scenario-relation-note">
        <strong>关系建议</strong>
        <span>{context.guide.relationNote}</span>
        {context.providerLinks.length > 0 && (
          <small className="observed-relation">已观察来源关联 {context.providerLinks.length} 条</small>
        )}
      </div>

      {relatedPreview.length > 0 && (
        <div className="related-assets" aria-label={`${group.subcategory}的相关能力`}>
          <strong>相关能力</strong>
          <div className="related-asset-list">
            {relatedPreview.map(({ item, relation }) => (
              <button
                key={item.asset_id}
                type="button"
                onClick={() => onSelectItem(item)}
                aria-label={`查看${SCENARIO_RELATION_LABELS[relation]}：${itemLabel(item)}`}
              >
                <span>{TYPE_LABELS[item.type]}</span>
                <b>{itemLabel(item)}</b>
                <small>{SCENARIO_RELATION_LABELS[relation]}</small>
              </button>
            ))}
            {context.related.length > relatedPreview.length && (
              <span className="related-more">另有 {context.related.length - relatedPreview.length} 项</span>
            )}
          </div>
          <small>相关能力只作关系提示，不计入本区段当前结果。</small>
        </div>
      )}

      <div className="capability-grid scenario-grid">
        {group.items.map((item) => (
          <AssetCard
            key={item.asset_id}
            item={item}
            favorite={favorites.has(item.asset_id)}
            findingCount={itemFindings(item, findings).length}
            showSubcategory={false}
            showOrigin={item.type === 'skill'}
            onSelect={() => onSelectItem(item)}
            onFavorite={() => onToggleFavorite(item.asset_id)}
          />
        ))}
      </div>
    </section>
  )
}

function AssetCard({ item, favorite, findingCount, onSelect, onFavorite, showSubcategory = true, showOrigin = false }) {
  const Icon = TYPE_ICONS[item.type] || Wrench
  const bindings = item.host_bindings || []
  const origin = showOrigin && item.type === 'skill' ? skillOriginMeta(item) : null
  return (
    <article className="asset-card">
      <button className="asset-card-main" onClick={onSelect}>
        <span className={`asset-icon asset-${item.type}`}>
          <Icon size={20} />
        </span>
        <span className="asset-copy">
          <span className="asset-title-row">
            <strong>{itemLabel(item)}</strong>
            {findingCount > 0 && (
              <small className="issue-count"><AlertTriangle size={12} /> {findingCount}</small>
            )}
          </span>
          <small className="source-name">{item.source_name}</small>
          <span className="asset-summary">{itemSummary(item)}</span>
        </span>
        <ChevronRight className="card-chevron" size={17} />
      </button>
      <footer className="asset-card-footer">
        <span className="classification-badges">
          <span className="type-badge">{TYPE_LABELS[item.type]}</span>
          {origin && <CollectionMetaBadge {...origin} focusable />}
          {showSubcategory && item.curation?.subcategory && (
            <span className="subcategory-badge" title={item.curation.subcategory}>
              {item.curation.subcategory}
            </span>
          )}
        </span>
        <div className="host-dots" aria-label="宿主绑定事实">
          {HOST_ORDER.map((hostId) => {
            const observed = bindings.some((binding) => binding.host_id === hostId)
            return (
              <span key={hostId} className={observed ? 'observed' : ''} title={`${HOST_LABELS[hostId]}：${observed ? '已启用' : '未启用'}`}>
                <span className="host-initial" aria-hidden="true">{HOST_LABELS[hostId].slice(0, 1)}</span>
              </span>
            )
          })}
        </div>
        <button className={`favorite-button${favorite ? ' active' : ''}`} onClick={onFavorite} aria-label={favorite ? '取消收藏' : '收藏'}>
          <Star size={17} fill={favorite ? 'currentColor' : 'none'} />
        </button>
      </footer>
    </article>
  )
}

function HostMark({ hostId }) {
  const label = HOST_LABELS[hostId] || hostId
  return (
    <span className={`host-mark host-${hostId}`} title={label} aria-label={label}>
      <span className="host-initial" aria-hidden="true">{label.slice(0, 1).toUpperCase()}</span>
    </span>
  )
}

function HealthView({
  groups,
  snapshot,
  items,
  candidateCatalog,
  candidateFindings,
  candidateScope,
  onSelectItem,
  onSelectCandidate,
  onBoundary,
}) {
  const errors = snapshot.scan_errors || []
  const lookup = new Map(items.map((item) => [item.asset_id, item]))
  const { attention, designNotes } = splitAttention(groups)
  const totalFindingCount = countFindings(groups)
  const hostFindingCount = snapshot.health_findings?.length || 0
  const candidateFindingCount = candidateFindings.length
  const candidateScopeReady = candidateScope.status === 'ready'
  const candidateScopeLabel = candidateScope.status === 'mismatch'
    ? '候选来源不一致'
    : '候选范围未载入'
  const designNoteCount = countFindings(designNotes)
  const warningCount = attention
    .filter((group) => group.severity === 'warning')
    .reduce((sum, group) => sum + group.findings.length, 0)
  const infoCount = attention
    .filter((group) => group.severity === 'info')
    .reduce((sum, group) => sum + group.findings.length, 0)
  return (
    <section className="health-view">
      <div className="view-intro health-intro">
        <div>
          <p className="section-kicker">观察事实，不是可用性认证</p>
          <h2>{totalFindingCount} 条体检记录</h2>
          <p>按宿主观察与候选质检聚合，逐项回到原对象；不提供自动修复。</p>
          <div className="health-scope-summary" aria-label="体检数据范围">
            <span>
              <b>宿主 {hostFindingCount} 条</b>
              <small>生成于 {formatTimestamp(snapshot.generated_at)}</small>
            </span>
            <span className={candidateScopeReady ? '' : 'is-missing'}>
              <b>{candidateScopeReady ? `候选 ${candidateFindingCount} 条` : candidateScopeLabel}</b>
              <small>
                {candidateScopeReady
                  ? `生成于 ${formatTimestamp(candidateCatalog?.generated_at)}`
                  : '未纳入当前体检总数'}
              </small>
            </span>
          </div>
        </div>
        <button className="secondary-button" onClick={onBoundary}>
          <ShieldCheck size={17} /> 查看数据与扫描边界
        </button>
      </div>
      {!candidateScopeReady && (
        <Notice tone="info">
          {candidateScope.status === 'mismatch'
            ? '候选快照与当前收藏来源不一致，已按边界排除；当前总数只包含宿主体检，未触发额外扫描。'
            : '候选快照或当前收藏来源尚未载入；当前总数只包含宿主体检，未触发额外扫描。'}
        </Notice>
      )}
      <div className="health-summary-grid">
        <article>
          <span className="finding-icon severity-error"><CircleAlert size={18} /></span>
          <strong>{errors.length}</strong>
          <small>扫描错误</small>
        </article>
        <article>
          <span className="finding-icon severity-warning"><AlertTriangle size={18} /></span>
          <strong>{warningCount}</strong>
          <small>警告记录</small>
        </article>
        <article>
          <span className="finding-icon severity-info"><Info size={18} /></span>
          <strong>{infoCount}</strong>
          <small>说明记录</small>
        </article>
        <article className="design-note-tile">
          <span className="finding-icon severity-muted"><Info size={18} /></span>
          <strong>{designNoteCount}</strong>
          <small>设计内跳过</small>
        </article>
      </div>
      {attention.length ? (
        <div className="finding-groups">
          {attention.map((group) => (
            <details className="finding-group" key={group.code} open={attention.length < 5}>
              <summary>
                <span className={`finding-icon severity-${group.severity}`}>
                  {group.severity === 'info' ? <Info size={17} /> : <AlertTriangle size={17} />}
                </span>
                <span>
                  <strong>{group.title}</strong>
                  <small>{group.code}</small>
                </span>
                <b>{group.findings.length}</b>
                <ChevronRight size={17} />
              </summary>
              {findingNote(group.code) && (
                <p className="finding-group-note">{findingNote(group.code)}</p>
              )}
              <div className="finding-rows">
                {group.findings.map((finding, index) => {
                  const item = lookup.get(finding.asset_id)
                  const candidateUid = finding.scope === 'candidate' ? finding.candidate_uid : null
                  const canOpen = Boolean(item || candidateUid)
                  const openFinding = () => {
                    if (item) onSelectItem(item)
                    else if (candidateUid) onSelectCandidate(candidateUid)
                  }
                  return (
                    <button
                      key={`${finding.code}-${finding.asset_id || candidateUid || index}-${index}`}
                      onClick={openFinding}
                      disabled={!canOpen}
                    >
                      <span>
                        <strong>{item ? itemLabel(item) : finding.title || group.title}</strong>
                        <small>
                          {finding.detail || finding.path || '该记录没有附加说明。'}
                          {candidateUid && finding.path ? ` · ${finding.path}` : ''}
                        </small>
                      </span>
                      {candidateUid && <em className="finding-scope-badge">候选</em>}
                      {canOpen && <ChevronRight size={16} />}
                    </button>
                  )
                })}
              </div>
            </details>
          ))}
        </div>
      ) : (
        <EmptyState
          icon={ShieldCheck}
          title="当前观察范围内未发现需处理项"
          detail="这只说明规则没有命中，不表示所有能力已安装、启用或通过验证。"
        />
      )}
      {designNotes.map((group) => (
        <details className="finding-group design-note-group" key={group.code}>
          <summary>
            <span className="finding-icon severity-muted"><Info size={17} /></span>
            <span>
              <strong>{group.title}</strong>
              <small>设计内 · 无需处理 · {group.code}</small>
            </span>
            <b>{group.findings.length}</b>
            <ChevronRight size={17} />
          </summary>
          {findingNote(group.code) && (
            <p className="finding-group-note">{findingNote(group.code)}</p>
          )}
        </details>
      ))}
    </section>
  )
}

function AssetDrawer({ item, findings, favorite, onFavorite, onClose }) {
  const localization = item.localization || {}
  const source = item.source || {}
  const TypeIcon = TYPE_ICONS[item.type] || Wrench
  const useCases = localization.use_cases || []
  const notFor = localization.not_for || []
  const examples = localization.examples || []
  const observation = observationFacts(item)
  const origin = item.type === 'skill' ? skillOriginMeta(item) : null
  return (
    <>
      <button className="drawer-scrim" aria-label="关闭详情" onClick={onClose} />
      <aside className="detail-drawer" aria-label={`${itemLabel(item)}详情`}>
        <header className="drawer-header">
          <button className="icon-button" onClick={onClose} aria-label="关闭详情"><X size={20} /></button>
          <button className={`favorite-button drawer-favorite${favorite ? ' active' : ''}`} onClick={onFavorite}>
            <Star size={17} fill={favorite ? 'currentColor' : 'none'} />
            {favorite ? '已收藏' : '收藏'}
          </button>
        </header>
        <div className="drawer-content">
          <div className="drawer-title">
            <span className={`asset-icon asset-${item.type}`}><TypeIcon size={22} /></span>
            <div>
              <h2>{itemLabel(item)}</h2>
              <span>{item.source_name} · {TYPE_LABELS[item.type]}</span>
            </div>
          </div>
          <div className="locked-banner"><LockKeyhole size={16} /> 锁定 · 只读</div>
          <DrawerSection title="能力简介">
            <p>{itemSummary(item)}</p>
          </DrawerSection>
          {useCases.length > 0 && <ListSection title="适用场景" items={useCases} />}
          {notFor.length > 0 && <ListSection title="不适用场景" items={notFor} tone="muted" />}
          {examples.length > 0 && <ListSection title="使用示意" items={examples} quote />}
          <DrawerSection title="宿主观察关系（不代表可用）">
            <div className="binding-list">
              {(item.host_bindings || []).map((binding) => (
                <article key={`${binding.host_id}-${binding.observed_path}`}>
                  <div>
                    <HostMark hostId={binding.host_id} />
                    <strong>{HOST_LABELS[binding.host_id] || binding.host_id}</strong>
                    <span className="type-badge">{binding.presence}</span>
                  </div>
                  <dl>
                    <div><dt>有效性</dt><dd>{binding.effective === 'unverified' ? '未验证' : binding.effective}</dd></div>
                    <div><dt>启用状态</dt><dd>{binding.activation === 'unknown' ? '未知' : binding.activation}</dd></div>
                    <div><dt>动作</dt><dd>锁定</dd></div>
                  </dl>
                  <PathRow value={binding.observed_path} />
                </article>
              ))}
            </div>
          </DrawerSection>
          <DrawerSection title="来源与身份">
            <dl className="identity-grid">
              {origin && <div><dt>来源</dt><dd><CollectionMetaBadge {...origin} focusable /></dd></div>}
              <div><dt>大类目</dt><dd>{item.curation?.category || '未分类'}</dd></div>
              <div><dt>子类目</dt><dd>{item.curation?.subcategory || '未细分'}</dd></div>
              <div><dt>版本</dt><dd>{source.version || '未声明'}</dd></div>
              <div><dt>作者</dt><dd>{source.author || '未声明'}</dd></div>
              <div><dt>许可证</dt><dd>{source.license || '未声明'}</dd></div>
              <div><dt>中文说明</dt><dd>{localization.status || 'missing'}</dd></div>
              <div><dt>包指纹</dt><dd>{source.package_fingerprint_status || 'unknown'}</dd></div>
            </dl>
            {(source.source_refs || [source.ref]).filter(Boolean).map((path) => <PathRow value={path} key={path} />)}
          </DrawerSection>
          {observation.length > 0 && (
            <DrawerSection title="只读观察依据">
              <dl className="identity-grid">
                {observation.map((fact) => (
                  <div key={fact.label}><dt>{fact.label}</dt><dd>{fact.value}</dd></div>
                ))}
              </dl>
            </DrawerSection>
          )}
          {findings.length > 0 && (
            <DrawerSection title={`关联体检项 · ${findings.length}`}>
              <div className="drawer-findings">
                {findings.map((finding, index) => (
                  <article key={`${finding.code}-${index}`}>
                    <AlertTriangle size={16} />
                    <span><strong>{FINDING_LABELS[finding.code] || finding.title || finding.code}</strong><small>{finding.detail}</small></span>
                  </article>
                ))}
              </div>
            </DrawerSection>
          )}
          {(item.composition || []).length > 0 && (
            <DrawerSection title="组成能力">
              <div className="composition-list">
                {item.composition.map((entry, index) => <span key={`${entry.type}-${entry.name}-${index}`}>{entry.type}: {entry.name}</span>)}
              </div>
            </DrawerSection>
          )}
        </div>
        <footer className="drawer-footer">
          <ShieldCheck size={16} /> 这里只呈现观察结果，不执行、安装或修改能力。
        </footer>
      </aside>
    </>
  )
}

function BoundaryDrawer({ snapshot, health, onClose }) {
  const safety = snapshot?.scan_scope?.safety || {}
  const roots = snapshot?.scan_scope?.roots || []
  const extensionCoverage = ['mcp', 'cli', 'sdk'].map((assetType) => ({
    assetType,
    ...coverageNotice(snapshot, assetType),
  }))
  return (
    <>
      <button className="drawer-scrim" aria-label="关闭观察说明" onClick={onClose} />
      <aside className="detail-drawer boundary-drawer" aria-label="观察说明">
        <header className="drawer-header">
          <span className="drawer-label"><ShieldCheck size={17} /> 观察说明</span>
          <button className="icon-button" onClick={onClose} aria-label="关闭观察说明"><X size={20} /></button>
        </header>
        <div className="drawer-content">
          <div className="boundary-hero">
            <span><LockKeyhole size={24} /></span>
            <h2>只读能力检索台</h2>
            <p>宿主能力只在主动刷新时扫描；同源候选质检只投影已载入的候选快照，不把收藏根加入宿主扫描。全部收藏只在工作台启动时检查一次文件元数据，之后由手动刷新触发；服务停掉后没有后台进程继续观察。</p>
          </div>
          <DrawerSection title="硬边界">
            <div className="boundary-checks">
              <BoundaryCheck label="写入宿主" safe={safety.writes === false} />
              <BoundaryCheck label="执行发现代码" safe={safety.process_execution === false} />
              <BoundaryCheck label="外部网络" safe={safety.network === false} />
              <BoundaryCheck label="越界软链接" safe={safety.follows_outside_symlinks === false} />
              <BoundaryCheck label="模型调用" safe={health?.capabilities?.model_calls === false} />
              <BoundaryCheck label="后台监听" safe={health?.capabilities?.background_watch === false} />
            </div>
          </DrawerSection>
          <DrawerSection title={`观察根目录 · ${roots.length}`}>
            <div className="scope-roots">
              {roots.map((root) => (
                <article key={`${root.host_id}-${root.asset_type}-${root.path}`}>
                  <span className={`scope-status status-${root.status}`} />
                  <div><strong>{HOST_LABELS[root.host_id]} · {TYPE_LABELS[root.asset_type]}</strong><PathRow value={root.path} /></div>
                  <small>
                    {root.status}
                    {root.depth_limited_entries > 0 ? ` · 深度边界 ${root.depth_limited_entries}` : ''}
                  </small>
                </article>
              ))}
            </div>
          </DrawerSection>
          <DrawerSection title="扩展接口覆盖（观察事实）">
            <div className="scope-roots">
              {extensionCoverage.map((coverage) => (
                <article key={coverage.assetType}>
                  <span className={`scope-status status-${coverage.status}`} />
                  <div>
                    <strong>{TYPE_LABELS[coverage.assetType]}</strong>
                    <p>{coverage.message}</p>
                  </div>
                  <small>{coverage.status}</small>
                </article>
              ))}
            </div>
          </DrawerSection>
          <DrawerSection title="明确排除">
            <div className="excluded-list">
              {(snapshot?.scan_scope?.excluded || []).map((entry) => <span key={entry}>{entry}</span>)}
            </div>
          </DrawerSection>
          <DrawerSection title="快照身份">
            <dl className="identity-grid">
              <div><dt>生成时间</dt><dd>{formatTimestamp(snapshot?.generated_at)}</dd></div>
              <div><dt>快照 ID</dt><dd>{snapshot?.generation_id || '—'}</dd></div>
              <div><dt>模式</dt><dd>{snapshot?.mode || 'observe'}</dd></div>
              <div><dt>API</dt><dd>{health?.api_version || 'v1'}</dd></div>
            </dl>
          </DrawerSection>
        </div>
        <footer className="drawer-footer"><Info size={16} /> 收藏只保存在浏览器本地，不改变能力或宿主状态。</footer>
      </aside>
    </>
  )
}

function DrawerSection({ title, children }) {
  return <section className="drawer-section"><h3>{title}</h3>{children}</section>
}

function CollectionMetaBadge({ label, tone = 'slate', detail, focusable = false }) {
  return (
    <span
      className={`collection-meta-badge tone-${tone}`}
      data-tooltip={detail}
      aria-label={detail}
      tabIndex={focusable ? 0 : undefined}
    >
      {label}
    </span>
  )
}

function CollectionVersionRelations({ relations = [], copyPaths = [], duplicateCount = 0 }) {
  const uniqueRelations = [...new Map(
    relations.map((relation) => [
      `${relation.type}:${normalizeCollectionPath(relation.target_relative_path)}`,
      relation,
    ]),
  ).values()]
  const uniqueCopies = [...new Set(copyPaths.map(normalizeCollectionPath).filter(Boolean))]
  const hasCopyGroup = uniqueCopies.length > 1
  const hasEvidence = uniqueRelations.length > 0 || hasCopyGroup || Number(duplicateCount) > 0
  return (
    <DrawerSection title="版本关系">
      {!hasEvidence && <p>未发现已确认的版本关系。</p>}
      {uniqueRelations.length > 0 && (
        <div className="collection-version-relations">
          {uniqueRelations.map((relation) => {
            const label = RELATION_LABELS[relation.type] || relation.type
            const detail = relation.note
              ? `${label}。${relation.note}`
              : `${label}。该关系来自已确认的收藏记录。`
            return (
              <article key={`${relation.type}:${relation.target_relative_path}`}>
                <CollectionMetaBadge
                  label={label}
                  tone={collectionRelationTone(relation.type)}
                  detail={detail}
                  focusable
                />
                <PathRow value={relation.target_relative_path} />
                {relation.note && <small>{relation.note}</small>}
              </article>
            )
          })}
        </div>
      )}
      {hasCopyGroup && (
        <div className="collection-version-relations">
          <article>
            <CollectionMetaBadge
              label="同版本副本"
              tone="rose"
              detail="这些路径记录为同一逻辑 Skill 的副本；尚未自动选择归档对象。"
              focusable
            />
            {uniqueCopies.map((path) => <PathRow value={path} key={path} />)}
          </article>
        </div>
      )}
      {Number(duplicateCount) > 0 && uniqueRelations.length === 0 && !hasCopyGroup && (
        <p>扫描发现 {duplicateCount} 组重复能力，但尚未确认新旧替代关系。</p>
      )}
    </DrawerSection>
  )
}

function ListSection({ title, items, quote = false, tone = '' }) {
  return (
    <DrawerSection title={title}>
      <ul className={`detail-list ${tone}${quote ? ' quote-list' : ''}`}>
        {items.map((item, index) => <li key={`${item}-${index}`}>{item}</li>)}
      </ul>
    </DrawerSection>
  )
}

function PathRow({ value }) {
  const [copied, setCopied] = useState(false)
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(value)
      setCopied(true)
      window.setTimeout(() => setCopied(false), 1200)
    } catch {
      setCopied(false)
    }
  }
  return (
    <div className="path-row">
      <code>{value}</code>
      <button onClick={copy} aria-label="复制路径">{copied ? <Check size={14} /> : <Copy size={14} />}</button>
    </div>
  )
}

function BoundaryCheck({ label, safe }) {
  return <span className={safe ? 'safe' : 'unknown'}>{safe ? <Check size={15} /> : <CircleHelp size={15} />}{label}<strong>{safe ? '关闭' : '未确认'}</strong></span>
}

function EmptyState({ icon: Icon = Search, title, detail, action, onAction }) {
  return (
    <div className="empty-state">
      <span><Icon size={25} /></span>
      <h3>{title}</h3>
      <p>{detail}</p>
      {action && <button className="secondary-button" onClick={onAction}>{action}</button>}
    </div>
  )
}

function InlineEmpty({ title, detail }) {
  return <div className="inline-empty"><CircleHelp size={20} /><span><strong>{title}</strong><small>{detail}</small></span></div>
}
