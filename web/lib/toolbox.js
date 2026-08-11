export const HOST_ORDER = ['codex', 'claude', 'hermes', 'workbuddy', 'antigravity']

export const HOST_LABELS = {
  codex: 'Codex',
  claude: 'Claude',
  hermes: 'Hermes',
  workbuddy: 'WorkBuddy',
  antigravity: 'Antigravity',
}

export const TYPE_LABELS = {
  skill: 'Skill',
  plugin: 'Plugin',
  mcp: 'MCP',
  cli: 'CLI',
  sdk: 'SDK',
}

export const COVERAGE_SOURCE_NOTES = {
  mcp: 'Codex 的 MCP 声明字段仅从版本化缓存 .mcp.json 读取，并只投影 server ID、传输形态和敏感配置标志；插件版本、作者与许可证沿用同版本缓存中已观察的 Plugin manifest。命令、参数及检测到的敏感配置不会进入快照。',
  cli: 'Codex 只观察固定 ~/.local/bin/codex 链接及允许目标的文件元数据；不读取或执行二进制体。',
  sdk: 'Codex 只观察版本化插件包根的直接 dependencies、peerDependencies、optionalDependencies 严格白名单及同包缓存 manifest；不导入 SDK。',
}

const COVERAGE_TYPES = Object.keys(TYPE_LABELS)
const CONNECTED_COVERAGE_STATUSES = new Set(['observed', 'missing', 'partial', 'error'])

export const FINDING_LABELS = {
  localization_missing: '中文说明缺失',
  localization_stale: '中文说明待复核',
  name_collision: '同名能力需确认',
  package_divergence: '同名能力包存在分歧',
  package_fingerprint_incomplete: '包指纹不完整',
  // 与 toolbox_scan.py 实际发出的 code 对齐（旧键 broken_symlink / out_of_scope_symlink 已停用）
  symlink_outside_allowlist: '软链接越出允许范围',
  symlink_broken: '发现断开的软链接',
  symlink_unresolvable: '软链接无法解析',
  exact_duplicate_observed: '同一资产多处出现',
  broken_symlink: '发现断开的链接',
  out_of_scope_symlink: '链接超出观察范围',
  scan_budget_exceeded: '观察预算已用完',
  root_unreadable: '宿主目录不可读',
}

// 设计内、无需处理的观察项：从"需要关注"移出，仅在体检页作灰色说明保留
export const DESIGN_NOTE_CODES = new Set(['package_fingerprint_incomplete'])

// 每类观察项的大白话说明，直接显示在工作台，避免"看着不知道怎么回事"
export const FINDING_NOTES = {
  symlink_outside_allowlist:
    '宿主里有指向“观察范围外”的软链接，观察器只登记、不跟随、不读取目标——不是故障，也不改动宿主。常见于把桌面或外部目录里的能力链接进各宿主；确认目标可信即可忽略，想彻底消警可把该目录加入白名单。',
  symlink_broken: '软链接的目标不存在，未计入可用资产。核对链接指向或删除失效链接即可。',
  name_collision:
    '同名但完整包内容不同，已各自保留为锁定变体，不会被静默合并。确认以哪一份为准后在宿主侧对齐即可；工具只观察、不改宿主。',
  localization_missing:
    '该能力还没有中文名称/简介/适用场景，只读显示但暂不进入完整推荐。补齐中文说明后即可纳入。',
  exact_duplicate_observed:
    '同一份能力在多个位置被观察到，已合并为一个资产并各自保留宿主绑定，无需处理。',
  package_fingerprint_incomplete:
    '只读模式下，包里除清单外还有脚本/子目录等正文时，为避免读到密钥或运行数据，观察器故意不取完整指纹。属设计内行为，不影响查看，只是不参与精确去重与受控开启。',
}

// 从分组结果中拆出"需处理"与"设计内说明"两部分
export function splitAttention(groups) {
  const attention = []
  const designNotes = []
  for (const group of groups || []) {
    ;(DESIGN_NOTE_CODES.has(group.code) ? designNotes : attention).push(group)
  }
  return { attention, designNotes }
}

export function countFindings(groups) {
  return (groups || []).reduce((sum, group) => sum + (group.findings?.length || 0), 0)
}

export function findingNote(code) {
  return FINDING_NOTES[code] || ''
}

export function localizationFor(item) {
  return item?.localization && typeof item.localization === 'object'
    ? item.localization
    : {}
}

export function itemLabel(item) {
  const localization = localizationFor(item)
  return localization.zh_name || item?.source?.display_name || item?.source_name || '未命名能力'
}

export function itemSummary(item) {
  const localization = localizationFor(item)
  return localization.summary || item?.description || '暂无用途说明。'
}

function flattenStrings(value, output = []) {
  if (typeof value === 'string') output.push(value)
  if (Array.isArray(value)) value.forEach((entry) => flattenStrings(entry, output))
  return output
}

export function buildSearchText(item) {
  const localization = localizationFor(item)
  return [
    itemLabel(item),
    item?.source_name,
    itemSummary(item),
    item?.curation?.category,
    item?.curation?.subcategory,
    ...flattenStrings(localization.synonyms),
    ...flattenStrings(localization.use_cases),
    ...flattenStrings(localization.not_for),
    ...flattenStrings(localization.examples),
  ]
    .filter(Boolean)
    .join(' ')
    .toLocaleLowerCase('zh-CN')
}

export function matchesSearch(item, query) {
  const terms = String(query || '')
    .trim()
    .toLocaleLowerCase('zh-CN')
    .split(/\s+/)
    .filter(Boolean)
  if (!terms.length) return true
  const haystack = buildSearchText(item)
  return terms.every((term) => haystack.includes(term))
}

export function filterAssets(items, filters = {}) {
  const favorites = filters.favorites || new Set()
  const types = filters.types ? new Set(filters.types) : null
  return (Array.isArray(items) ? items : []).filter((item) => {
    if (types && !types.has(item.type)) return false
    if (filters.host && !item.host_bindings?.some((binding) => binding.host_id === filters.host)) {
      return false
    }
    if (filters.category && item.curation?.category !== filters.category) return false
    if (filters.subcategory && item.curation?.subcategory !== filters.subcategory) return false
    if (filters.localization && item.localization?.status !== filters.localization) return false
    if (filters.favoriteOnly && !favorites.has(item.asset_id)) return false
    if (filters.multiHost && (item.host_bindings?.length || 0) < 2) return false
    return matchesSearch(item, filters.query)
  })
}

export function rankedAssets(items, favorites = new Set()) {
  return [...(items || [])].sort((left, right) => {
    const score = (item) =>
      (favorites.has(item.asset_id) ? 100 : 0) +
      (item.curation?.common ? 30 : 0) +
      Math.min(item.host_bindings?.length || 0, 4) * 5 +
      (item.localization?.coverage_complete ? 3 : 0) -
      (item.health_findings?.length || 0)
    return score(right) - score(left) || itemLabel(left).localeCompare(itemLabel(right), 'zh-CN')
  })
}

export function categoriesFor(items) {
  return [...new Set((items || []).map((item) => item.curation?.category).filter(Boolean))].sort(
    (left, right) => left.localeCompare(right, 'zh-CN'),
  )
}

export function subcategoriesFor(items, category = '') {
  return [
    ...new Set(
      (items || [])
        .filter((item) => !category || item.curation?.category === category)
        .map((item) => item.curation?.subcategory)
        .filter(Boolean),
    ),
  ].sort((left, right) => left.localeCompare(right, 'zh-CN'))
}

export function hostStats(snapshot) {
  const items = snapshot?.items || []
  const scopeRoots = snapshot?.scan_scope?.roots || []
  return HOST_ORDER.map((hostId) => {
    const host = snapshot?.hosts?.find((entry) => entry.id === hostId)
    const related = items.filter((item) =>
      item.host_bindings?.some((binding) => binding.host_id === hostId),
    )
    const scopes = scopeRoots.filter((root) => root.host_id === hostId)
    const countsByType = Object.fromEntries(
      COVERAGE_TYPES.map((assetType) => [
        assetType,
        related.filter((item) => item.type === assetType).length,
      ]),
    )
    return {
      id: hostId,
      label: host?.label || HOST_LABELS[hostId],
      status: host?.status || 'missing',
      safeMetadataOnly: Boolean(host?.safe_metadata_only),
      assetCount: related.length,
      countsByType,
      skillCount: countsByType.skill,
      pluginCount: countsByType.plugin,
      roots: scopes,
    }
  })
}

function coverageRowsFor(snapshot, assetType) {
  const rows = snapshot?.scan_scope?.coverage
  if (!Array.isArray(rows)) return []
  return rows.filter((row) => row?.asset_type === assetType)
}

function coverageHostList(rows) {
  return rows.map((row) => HOST_LABELS[row.host_id] || row.host_id).join('、')
}

function coverageTone(status) {
  return status === 'error' || status === 'partial' ? 'error' : 'info'
}

/**
 * Turn the host×asset coverage contract into user-facing scope language.
 * Counts never determine coverage: observed+0 is a completed bounded check,
 * while not_connected+0 is explicitly unknown outside the declared scope.
 */
export function coverageNotice(snapshot, assetType, hostId = '') {
  const typeLabel = TYPE_LABELS[assetType] || assetType
  const sourceNote = COVERAGE_SOURCE_NOTES[assetType]
    ? ` ${COVERAGE_SOURCE_NOTES[assetType]}`
    : ''
  const rows = coverageRowsFor(snapshot, assetType)

  if (!rows.length) {
    return {
      status: 'error',
      tone: 'error',
      message: `当前快照缺少 ${typeLabel} 覆盖合同，请刷新观察；现有数量不能解释为本机总量。`,
    }
  }

  if (hostId) {
    const hostLabel = HOST_LABELS[hostId] || hostId
    const row = rows.find((entry) => entry.host_id === hostId)
    if (!row) {
      return {
        status: 'error',
        tone: 'error',
        message: `当前快照缺少 ${hostLabel} × ${typeLabel} 的覆盖事实，无法解释当前结果。`,
      }
    }
    const count = Number.isInteger(row.item_count) ? row.item_count : 0
    const prefix = `${hostLabel} × ${typeLabel}`
    const messages = {
      observed:
        count === 0
          ? `已覆盖 ${prefix} 的限定观察源，本次未观察到声明；这不证明本机其他位置不存在。`
          : `已覆盖 ${prefix} 的限定观察源，观察到 ${count} 项声明。发现不等于安装、启用、有效或可执行。`,
      partial: `${prefix} 本次观察不完整；现有 ${count} 项不能作为完整盘点数量。`,
      missing: `${prefix} 已配置观察源，但预期来源不存在；当前 0 只表示该限定入口无结果，不代表本机不存在。`,
      not_connected: `${prefix} 尚未接入观察源；当前 0 或无结果只表示“未观察”，不表示本机不存在。`,
      placeholder: `${prefix} 仅保留合同占位，尚未接入可安全读取的观察源。`,
      error: `${prefix} 本次观察失败；现有数量不能作为完整盘点结果。`,
    }
    const status = messages[row.status] ? row.status : 'error'
    return {
      status,
      tone: coverageTone(status),
      message: `${messages[row.status] || `${prefix} 的覆盖状态无法识别。`}${sourceNote}`,
    }
  }

  const observed = rows.filter((row) => row.status === 'observed')
  const missing = rows.filter((row) => row.status === 'missing')
  const partial = rows.filter((row) => row.status === 'partial')
  const errors = rows.filter((row) => row.status === 'error')
  const notConnected = rows.filter((row) => row.status === 'not_connected')
  const placeholders = rows.filter((row) => row.status === 'placeholder')
  const connected = rows.filter((row) => CONNECTED_COVERAGE_STATUSES.has(row.status))
  const facts = [
    observed.length ? `已完成：${coverageHostList(observed)}` : '',
    missing.length ? `来源缺失：${coverageHostList(missing)}` : '',
    partial.length ? `不完整：${coverageHostList(partial)}` : '',
    errors.length ? `失败：${coverageHostList(errors)}` : '',
    notConnected.length ? `未接入：${coverageHostList(notConnected)}` : '',
    placeholders.length ? `占位：${coverageHostList(placeholders)}` : '',
  ].filter(Boolean)

  const hasScanFailure = partial.length > 0 || errors.length > 0
  const hasScopeGap = connected.length > 0 && notConnected.length > 0
  const observedItemCount = observed.reduce(
    (total, row) => total + (Number.isInteger(row.item_count) ? row.item_count : 0),
    0,
  )
  const observedEmptyNote =
    observed.length > 0 && observedItemCount === 0
      ? '已完成的限定来源本次未观察到声明；这不证明本机其他位置不存在。'
      : ''
  let status = 'not_connected'
  let lead = `${typeLabel} 尚未接入观察源`
  if (hasScanFailure) {
    status = errors.length ? 'error' : 'partial'
    lead = `${typeLabel} 观察不完整`
  } else if (hasScopeGap) {
    status = 'limited'
    lead = `${typeLabel} 当前为局部覆盖`
  } else if (observed.length > 0) {
    status = 'observed'
    lead = `${typeLabel} 已完成合同范围观察`
  } else if (missing.length > 0) {
    status = 'missing'
    lead = `${typeLabel} 已配置观察源，但来源缺失`
  }

  return {
    status,
    tone: hasScanFailure ? 'error' : 'info',
    message: `${lead}：${facts.join('；')}。${observedEmptyNote}数量只代表已声明边界内的观察结果，不是全机或全部宿主总量；发现不等于安装、启用、有效或可执行。${sourceNote}`,
  }
}

export function observationFacts(item) {
  const observation = item?.source?.observation
  if (!observation || typeof observation !== 'object') return []
  const common = [
    { label: '观察适配器', value: observation.adapter_id || '未声明' },
  ]
  if (item?.type === 'mcp') {
    return [
      ...common,
      { label: '观察依据', value: '版本化插件缓存 .mcp.json 的安全投影' },
      { label: '服务标识', value: observation.server_id || '未声明' },
      { label: '传输形态', value: observation.transport_kind || '未声明' },
      {
        label: '敏感配置形状',
        value: observation.sensitive_config_present
          ? '已检测；具体字段与值已省略'
          : '未检测到',
      },
    ]
  }
  if (item?.type === 'cli') {
    return [
      ...common,
      { label: '观察依据', value: '固定 CLI 链接与允许目标的文件元数据' },
      { label: '边界', value: '未读取或执行二进制体' },
    ]
  }
  if (item?.type === 'sdk') {
    return [
      ...common,
      { label: '观察依据', value: '插件包根的直接依赖白名单与同包缓存 manifest' },
      { label: 'SDK 包', value: observation.sdk_package_name || '未声明' },
      { label: '边界', value: '未导入或执行 SDK' },
    ]
  }
  return common
}

export function reconcileSelectedItem(current, nextItems) {
  if (!current) return null
  return (nextItems || []).find((item) => item.asset_id === current.asset_id) || null
}

export function issueGroups(findings) {
  const groups = new Map()
  for (const finding of findings || []) {
    const key = finding.code || 'unknown'
    const group = groups.get(key) || {
      code: key,
      title: FINDING_LABELS[key] || finding.title || '其他观察项',
      severity: finding.severity || 'info',
      findings: [],
    }
    group.findings.push(finding)
    if (finding.severity === 'error') group.severity = 'error'
    if (finding.severity === 'warning' && group.severity !== 'error') group.severity = 'warning'
    groups.set(key, group)
  }
  const severityOrder = { error: 0, warning: 1, info: 2 }
  return [...groups.values()].sort(
    (left, right) =>
      (severityOrder[left.severity] ?? 3) - (severityOrder[right.severity] ?? 3) ||
      right.findings.length - left.findings.length ||
      left.title.localeCompare(right.title, 'zh-CN'),
  )
}

export function itemFindings(item, findings) {
  return (findings || []).filter((finding) => finding.asset_id === item?.asset_id)
}

export function formatTimestamp(value) {
  if (!value) return '尚未观察'
  const date = new Date(value)
  if (Number.isNaN(date.valueOf())) return String(value)
  return new Intl.DateTimeFormat('zh-CN', {
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(date)
}

export function snapshotAge(value, now = Date.now()) {
  const timestamp = new Date(value).valueOf()
  if (!Number.isFinite(timestamp)) return '时间未知'
  const minutes = Math.max(0, Math.round((now - timestamp) / 60000))
  if (minutes < 1) return '刚刚'
  if (minutes < 60) return `${minutes} 分钟前`
  const hours = Math.round(minutes / 60)
  if (hours < 24) return `${hours} 小时前`
  return `${Math.round(hours / 24)} 天前`
}
