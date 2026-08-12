export const PROJECT_CLASSIFICATION_LABELS = Object.freeze({
  project: '项目',
  container: '容器',
  archive: '归档',
  excluded: '已排除',
  unclassified: '未分类',
})

export const EVIDENCE_LAYER_ORDER = Object.freeze([
  'file_discovery',
  'project_binding',
  'host_availability',
  'invocation_eligibility',
  'actual_use',
])

export const EVIDENCE_LAYER_LABELS = Object.freeze({
  file_discovery: '文件发现',
  project_binding: '项目绑定',
  host_availability: '宿主可用',
  invocation_eligibility: '调用资格',
  actual_use: '实际使用',
})

const EVIDENCE_STATUS_LABELS = Object.freeze({
  file_discovery: Object.freeze({
    observed: '已观察到文件',
  }),
  project_binding: Object.freeze({
    observed_binding: '已观察到项目绑定候选',
    source_only: '普通来源目录（不等于项目绑定）',
    plugin_bundled_source: '插件包来源（不等于项目绑定）',
    observed_path_only: '已观察到特定路径；项目绑定未核实',
  }),
  host_availability: Object.freeze({
    unverified: '宿主可用性未核实',
  }),
  invocation_eligibility: Object.freeze({
    declaration_only: '仅 Skill 自身调用声明',
    not_observed: '未观察到受批准声明',
    unverified: '本次无法核实调用声明',
  }),
  actual_use: Object.freeze({
    not_connected: '使用证据未接入',
  }),
})

export const PROJECT_SCAN_STATUS_LABELS = Object.freeze({
  complete: '观察完整',
  partial: '本次未能完整确认',
  error: '本次观察失败',
  security_reject: '安全边界已拒绝',
  not_scanned: '未扫描（非项目对象）',
})

const ENTRY_STATUS_LABELS = Object.freeze({
  observed: '已观察到入口内容',
  empty: '入口已观察，当前为空',
  missing: '批准入口不存在',
  partial: '入口观察不完整',
  error: '入口观察失败',
  security_reject: '入口被安全边界拒绝',
})

const INVOCATION_UNVERIFIED_REASON_LABELS = Object.freeze({
  scan_incomplete: '观察不完整',
  invalid_yaml: '声明文件格式无效',
  over_limit: '声明文件超出读取限制',
  permission_denied: '声明文件无法读取',
  security_reject: '安全边界已拒绝',
  path_race: '路径在核验期间发生变化',
})

function array(value) {
  return Array.isArray(value) ? value : []
}

function object(value) {
  return value && typeof value === 'object' && !Array.isArray(value) ? value : {}
}

function normalizedQuery(value) {
  return String(value || '').trim().toLocaleLowerCase('zh-CN')
}

const PROJECT_SKILL_SCENARIOS = Object.freeze([
  {
    name: 'GitHub 与代码协作',
    category: '开发与代码',
    description: '提交、同步、推送、合并与发布等代码协作动作。',
    nameTest: /(^|[\s_-])(commit|push|pull|land|release)([\s_-]|$)|git|github/i,
    descriptionTest: /pull request|merge conflict|代码提交|代码发布/i,
  },
  {
    name: '调试、诊断与重构',
    category: '开发与代码',
    description: '定位运行失败、追踪问题并形成可验证的修复路径。',
    nameTest: /(^|[\s_-])debug([\s_-]|$)|diagnos|refactor|排障|调试|诊断|重构/i,
    descriptionTest: /定位运行失败|故障链路|诊断并修复|debugging/i,
  },
  {
    name: '视频、动画与分镜',
    category: '视觉与媒体',
    description: '从剧本、镜头设计到可执行视频提示词的制作能力。',
    nameTest: /seedance|storyboard|video|director|分镜|视频/i,
    descriptionTest: /video prompt|shot list|camera logic|导演分镜|镜头设计/i,
  },
  {
    name: '编剧与剧本',
    category: '内容与写作',
    description: '故事诊断、结构改写、台词与可拍剧本生产。',
    nameTest: /screenplay|bianju|编剧|剧本/i,
    descriptionTest: /专业中文编剧|可拍剧本|故事诊断与结构改写/i,
  },
  {
    name: '视觉设计与创意生产',
    category: '视觉与媒体',
    description: '科学插图、海报、排版、画布与可编辑视觉资产。',
    nameTest: /scientific|illustration|drawio|powerpoint|indesign|poster|print|canvas|视觉|插图|海报|排版|画布/i,
    descriptionTest: /scientific figure|可编辑视觉资产|平面设计|科学插图/i,
  },
  {
    name: '工具链与工作流设计',
    category: '协作与管理',
    description: '将多阶段任务连接成可复核的执行工作流。',
    nameTest: /pipeline|workflow|orchestrat|编排|工作流/i,
    descriptionTest: /多阶段.*工作流|阶段间人工确认|编排执行/i,
  },
  {
    name: '命令行入口',
    category: '开发与代码',
    description: '通过明确的命令行入口操作项目、素材与工具。',
    nameTest: /(^|[\s_-])cli([\s_-]|$)|command[-_]?line|命令行/i,
    descriptionTest: /官方 CLI|通过命令行入口/i,
  },
  {
    name: 'Agent 协作与执行方法',
    category: '协作与管理',
    description: '项目任务、协作系统与执行过程的连接能力。',
    nameTest: /linear|graphql|task|project[-_]?management|协作|任务管理/i,
    descriptionTest: /项目管理系统|任务协作系统/i,
  },
])

export function projectSkillScenario(skill) {
  const observation = array(skill?.observations)[0]
  const projection = object(observation?.frontmatter_projection)
  const localization = object(skill?.localization)
  const nameText = [skill?.display_name, projection.name, localization.zh_name].filter(Boolean).join(' ')
  const descriptionText = [projection.description, localization.summary].filter(Boolean).join(' ')
  const matchedByName = PROJECT_SKILL_SCENARIOS.find((scenario) => scenario.nameTest.test(nameText))
  const matched =
    matchedByName ||
    PROJECT_SKILL_SCENARIOS.find((scenario) => scenario.descriptionTest.test(descriptionText))
  return matched || {
    name: '未细分',
    category: '尚未归入场景',
    description: '当前只展示受限元数据，尚未形成更具体的使用场景。',
  }
}

export function projectSkillScenarioGroups(project) {
  const groups = new Map()
  for (const skill of array(project?.logical_skills)) {
    const scenario = projectSkillScenario(skill)
    if (!groups.has(scenario.name)) {
      groups.set(scenario.name, { ...scenario, skills: [] })
    }
    groups.get(scenario.name).skills.push(skill)
  }
  return [...groups.values()].sort((left, right) => {
    if (left.name === '未细分') return 1
    if (right.name === '未细分') return -1
    return left.name.localeCompare(right.name, 'zh-CN')
  })
}

export function evidenceStateLabel(layer, status) {
  return EVIDENCE_STATUS_LABELS[layer]?.[status] || '证据状态未核实'
}

export function projectScanStatusLabel(status) {
  return PROJECT_SCAN_STATUS_LABELS[status] || '观察状态未核实'
}

export function entryStatusLabel(status) {
  return ENTRY_STATUS_LABELS[status] || '入口状态未核实'
}

export function evidenceRowsForSkill(skill) {
  return array(skill?.observations).map((observation) => ({
    observationId: observation?.observation_id || '',
    manifestRelativePath: observation?.manifest_relative_path || '',
    sourceKind: observation?.source_kind || '',
    fileType: observation?.file_type || '',
    linkStatus: observation?.link_status || '',
    layers: EVIDENCE_LAYER_ORDER.map((layer) => {
      const status = observation?.evidence?.[layer]?.status || 'unknown'
      let detail = ''
      if (layer === 'invocation_eligibility' && status === 'declaration_only') {
        const declaredValue = observation?.openai_declaration?.allow_implicit_invocation
        detail =
          typeof declaredValue === 'boolean'
            ? `Skill 自身声明${declaredValue ? '允许' : '不允许'}隐式调用；不代表宿主当前会匹配或已调用`
            : '仅确认存在 Skill 自身声明；声明值未核实'
      } else if (layer === 'invocation_eligibility' && status === 'unverified') {
        detail =
          INVOCATION_UNVERIFIED_REASON_LABELS[observation?.openai_declaration?.reason] ||
          '本次无法核实声明文件'
      }
      return {
        layer,
        layerLabel: EVIDENCE_LAYER_LABELS[layer],
        status,
        label: evidenceStateLabel(layer, status),
        detail,
      }
    }),
  }))
}

function observationsForProject(project) {
  return array(project?.logical_skills).flatMap((skill) => array(skill?.observations))
}

export function projectHumanAssociations(snapshot, projectId) {
  return array(snapshot?.human_associations?.items).filter(
    (association) => association?.project_id === projectId,
  )
}

export function projectSkillSummary(project, snapshot = null) {
  const logicalSkills = array(project?.logical_skills)
  const observations = observationsForProject(project)
  const bindingEvidenceCount = observations.filter(
    (row) => row?.evidence?.project_binding?.status === 'observed_binding',
  ).length
  const associations = snapshot
    ? projectHumanAssociations(snapshot, project?.project_id)
    : array(project?.humanAssociations)
  return {
    logicalSkillCount: logicalSkills.length,
    fileObservationCount: observations.length,
    projectBindingEvidenceCount: bindingEvidenceCount,
    pluginSourceEvidenceCount: observations.filter(
      (row) => row?.evidence?.project_binding?.status === 'plugin_bundled_source',
    ).length,
    pathOnlyEvidenceCount: observations.filter(
      (row) => row?.evidence?.project_binding?.status === 'observed_path_only',
    ).length,
    observedEntryCount: array(project?.entries).filter((entry) => entry?.status === 'observed')
      .length,
    issueReferenceCount: array(project?.entries).reduce(
      (sum, entry) => sum + array(entry?.issue_refs).length,
      0,
    ),
    humanAssociationCount: associations.length,
  }
}

export function projectMetrics(snapshot) {
  const projects = array(snapshot?.projects)
  const registeredProjects = projects.filter((project) => project?.classification === 'project')
  const summaries = registeredProjects.map((project) => projectSkillSummary(project, snapshot))
  return {
    registeredObjectCount: projects.length,
    registeredProjectCount: registeredProjects.length,
    containerCount: projects.filter((project) => project?.classification === 'container').length,
    logicalSkillCount: summaries.reduce((sum, row) => sum + row.logicalSkillCount, 0),
    fileObservationCount: summaries.reduce((sum, row) => sum + row.fileObservationCount, 0),
    projectBindingEvidenceCount: summaries.reduce(
      (sum, row) => sum + row.projectBindingEvidenceCount,
      0,
    ),
    humanAssociationCount: array(snapshot?.human_associations?.items).length,
    unclassifiedCandidateCount: array(snapshot?.candidates).filter(
      (candidate) => candidate?.status === 'unclassified',
    ).length,
    issueCount: array(snapshot?.issues).length,
  }
}

export function projectCategories(snapshot) {
  const projects = array(snapshot?.projects)
  return Object.entries(PROJECT_CLASSIFICATION_LABELS)
    .map(([value, label]) => ({
      value,
      label,
      count: projects.filter((project) => project?.classification === value).length,
    }))
    .filter((row) => row.count > 0)
}

function projectSearchText(project, snapshot) {
  const skills = array(project?.logical_skills)
  const associations = snapshot
    ? projectHumanAssociations(snapshot, project?.project_id)
    : array(project?.humanAssociations)
  return [
    project?.display_name,
    project?.project_id,
    project?.relative_path,
    ...skills.flatMap((skill) => [
      skill?.display_name,
      skill?.logical_skill_id,
      skill?.localization?.zh_name,
      skill?.localization?.summary,
      ...array(skill?.localization?.use_cases),
      ...array(skill?.localization?.not_for),
      ...array(skill?.localization?.examples),
      ...array(skill?.observations).flatMap((observation) => [
        observation?.manifest_relative_path,
        observation?.frontmatter_projection?.name,
        observation?.frontmatter_projection?.description,
      ]),
    ]),
    ...associations.flatMap((association) => [
      association?.asset_id,
      association?.unresolved_name,
      association?.reason,
    ]),
  ]
    .filter(Boolean)
    .join(' ')
    .toLocaleLowerCase('zh-CN')
}

function hasEvidenceStatus(project, status) {
  return observationsForProject(project).some((observation) =>
    EVIDENCE_LAYER_ORDER.some((layer) => observation?.evidence?.[layer]?.status === status),
  )
}

export function filterProjects(snapshotOrProjects, filters = {}) {
  const snapshot = Array.isArray(snapshotOrProjects) ? null : snapshotOrProjects
  const projects = Array.isArray(snapshotOrProjects)
    ? snapshotOrProjects
    : array(snapshotOrProjects?.projects)
  const query = normalizedQuery(filters.query)
  const terms = query.split(/\s+/).filter(Boolean)
  return projects.filter((project) => {
    if (filters.classification && project?.classification !== filters.classification) return false
    if (filters.scanStatus && project?.scan_status !== filters.scanStatus) return false
    if (
      filters.entryStatus &&
      !array(project?.entries).some((entry) => entry?.status === filters.entryStatus)
    ) {
      return false
    }
    if (
      filters.linkStatus &&
      !observationsForProject(project).some(
        (observation) => observation?.link_status === filters.linkStatus,
      )
    ) {
      return false
    }
    if (filters.evidence && !hasEvidenceStatus(project, filters.evidence)) return false
    if (!terms.length) return true
    const haystack = project?.searchText || projectSearchText(project, snapshot)
    return terms.every((term) => haystack.includes(term))
  })
}

export function projectSkillChangeSummary(snapshotOrChanges) {
  const changes = object(snapshotOrChanges?.changes || snapshotOrChanges)
  if (changes.status === 'compared') {
    return {
      status: 'compared',
      label: '已与上一份完整快照比较',
      added: array(changes.added),
      changed: array(changes.changed),
      removed: array(changes.removed),
      addedCount: array(changes.added).length,
      changedCount: array(changes.changed).length,
      removedCount: array(changes.removed).length,
      comparedToGenerationId: changes.compared_to_generation_id || null,
    }
  }
  if (changes.status === 'not_comparable') {
    return {
      status: 'not_comparable',
      label: '投影指纹版本不同，本次不比较变化',
      added: [],
      changed: [],
      removed: [],
      addedCount: 0,
      changedCount: 0,
      removedCount: 0,
      comparedToGenerationId: null,
    }
  }
  return {
    status: 'not_available',
    label: '暂无上一份完整快照可比较',
    added: [],
    changed: [],
    removed: [],
    addedCount: 0,
    changedCount: 0,
    removedCount: 0,
    comparedToGenerationId: null,
  }
}

export function projectSkillStatusText(envelope) {
  const snapshot = envelope?.snapshot
  const attempt = envelope?.last_attempt
  const integrity = object(envelope?.integrity)
  if (!snapshot) {
    if (attempt?.scan_status === 'partial') return '首次观察不完整，尚无可展示的完整快照'
    if (attempt?.scan_status === 'security_reject') return '首次观察被安全边界拒绝'
    if (attempt?.scan_status === 'error') return '首次观察失败，尚无可展示的完整快照'
    return '尚无完整的项目 Skill 观察快照'
  }
  const retained = attempt && attempt.promoted === false
  const base = retained
    ? `已保留上次完整快照；本次${projectScanStatusLabel(attempt.scan_status)}`
    : '已加载最近完整的只读观察快照'
  return integrity.degraded ? `${base}；辅助输出完整性降级` : base
}

export function adaptProjectSkillsEnvelope(envelope) {
  const source = object(envelope)
  const snapshot = source.snapshot && typeof source.snapshot === 'object' ? source.snapshot : null
  const projects = array(snapshot?.projects).map((project) => {
    const humanAssociations = projectHumanAssociations(snapshot, project?.project_id)
    const adapted = {
      ...project,
      humanAssociations,
      metrics: projectSkillSummary(project, snapshot),
    }
    return { ...adapted, searchText: projectSearchText(adapted, snapshot) }
  })
  return {
    snapshot,
    lastAttempt: source.last_attempt || null,
    report: {
      available: source.report?.available === true,
      generationId: source.report?.generation_id || null,
      content: typeof source.report?.content === 'string' ? source.report.content : null,
    },
    integrity: {
      degraded: source.integrity?.degraded === true,
      errors: array(source.integrity?.errors),
    },
    projects,
    candidates: array(snapshot?.candidates),
    metrics: projectMetrics(snapshot),
    changes: projectSkillChangeSummary(snapshot?.changes),
    status: projectSkillStatusText(source),
  }
}
