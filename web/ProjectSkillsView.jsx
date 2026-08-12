import { useEffect, useMemo, useRef, useState } from 'react'
import { createPortal } from 'react-dom'
import {
  AlertTriangle,
  Archive,
  Boxes,
  ChevronDown,
  ChevronRight,
  CircleAlert,
  CircleHelp,
  Database,
  FileText,
  Folder,
  Layers3,
  LockKeyhole,
  RefreshCw,
  ShieldCheck,
  Sparkles,
  UserRound,
  X,
} from 'lucide-react'
import {
  EVIDENCE_LAYER_LABELS,
  PROJECT_CLASSIFICATION_LABELS,
  entryStatusLabel,
  evidenceRowsForSkill,
  evidenceStateLabel,
  projectHumanAssociations,
  projectMetrics,
  projectScanStatusLabel,
  projectSkillScenario,
  projectSkillScenarioGroups,
  projectSkillChangeSummary,
  projectSkillSummary,
} from './lib/projectSkills'

const INTEGRITY_LABELS = {
  last_attempt_invalid: '最近尝试回执无效',
  last_attempt_stale: '最近尝试回执与当前快照不同代',
  last_attempt_missing: '最近尝试回执缺失',
  report_invalid: 'Markdown 总览未通过同代校验',
  report_missing: 'Markdown 总览缺失',
}

function formatTimestamp(value) {
  if (!value) return '尚未观察'
  const parsed = new Date(value)
  if (Number.isNaN(parsed.getTime())) return '时间未核实'
  return new Intl.DateTimeFormat('zh-CN', {
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(parsed)
}

function statusTone(status) {
  if (status === 'complete' || status === 'observed') return 'success'
  if (status === 'partial' || status === 'not_scanned' || status === 'empty') return 'warning'
  if (status === 'error' || status === 'security_reject') return 'error'
  return 'neutral'
}

function evidenceTone(status) {
  if (status === 'observed' || status === 'observed_binding') return 'positive'
  if (status === 'partial' || status === 'unverified' || status === 'not_connected') return 'pending'
  if (status === 'error' || status === 'security_reject') return 'danger'
  return 'neutral'
}

function StatePanel({ icon: Icon, tone = 'neutral', title, detail, children }) {
  return (
    <section className={`project-skills-state state-${tone}`} role={tone === 'error' ? 'alert' : 'status'}>
      <span className="project-skills-state-icon"><Icon size={24} /></span>
      <div>
        <h2>{title}</h2>
        <p>{detail}</p>
        {children}
      </div>
    </section>
  )
}

function StatusSummary({ snapshot, attempt, integrity, report, onOpenReport, reportTriggerRef }) {
  const incompleteAttempt = attempt && attempt.scan_status !== 'complete'
  const integrityErrors = integrity?.errors || []
  const currentIssues = incompleteAttempt ? (attempt.issues || []) : (snapshot.issues || [])
  const issueCounts = currentIssues.reduce((counts, issue) => {
    const status = issue.status || 'partial'
    counts[status] = (counts[status] || 0) + 1
    return counts
  }, {})
  const issueText = `问题 ${issueCounts.partial || 0} 部分 · ${issueCounts.error || 0} 错误 · ${issueCounts.security_reject || 0} 拒绝`
  return (
    <section className="project-skills-status" aria-label="项目 Skill 观察状态">
      <div className="project-skills-status-line">
        <span className="project-skills-status-lead">
          <ShieldCheck size={16} />
          <strong>只读观察</strong>
        </span>
        <span className="project-skills-status-meta">
          <span>快照 {projectScanStatusLabel(snapshot.scan_status)}</span>
          <span>{formatTimestamp(snapshot.generated_at)}</span>
          <span title={snapshot.generation_id}>快照 <code>{snapshot.generation_id?.slice(0, 8) || '未知'}</code></span>
          <span className={`status-${statusTone(attempt?.scan_status)}`}>
            最近尝试 {attempt ? projectScanStatusLabel(attempt.scan_status) : '回执未提供'}
          </span>
          <span title={issueText} className={integrity?.degraded ? 'status-warning' : ''}>
            辅助对象 {integrity?.degraded ? '降级' : '完整'}
          </span>
        </span>
        <span className="project-skills-status-actions">
          {report?.available && (
            <button ref={reportTriggerRef} className="project-skills-report-open" type="button" onClick={onOpenReport}>
              Markdown 总览
            </button>
          )}
          <span className="project-skills-contract-ref" title={`边界合同 ${snapshot.observation_boundary_ref} · ${issueText}`}>
            {snapshot.observation_boundary_ref}
          </span>
        </span>
      </div>
      {incompleteAttempt && (
        <div className="project-skills-retained-note" role="alert">
          <AlertTriangle size={17} />
          <div>
            <span>本次未能确认；保留上一份完整结果，不把未观察到的对象解释为已删除。</span>
            {currentIssues.length > 0 && (
              <ul className="project-skills-attempt-issues">
                {currentIssues.map((issue, index) => (
                  <li key={`${issue.status}:${issue.code}:${issue.project_id || ''}:${issue.relative_path || ''}:${index}`}>
                    <code>{issue.code}</code>
                    {issue.project_id && <span>项目 {issue.project_id}</span>}
                    {issue.relative_path && <span>路径 <code>{issue.relative_path}</code></span>}
                  </li>
                ))}
              </ul>
            )}
          </div>
        </div>
      )}
      {integrity?.degraded && (
        <div className="project-skills-integrity-note" role="status">
          <CircleAlert size={17} />
          <span>
            辅助输出异常，机器快照仍可读；降级项：
            {integrityErrors.map((code) => INTEGRITY_LABELS[code] || code).join('、') || '未指明'}。
          </span>
        </div>
      )}
    </section>
  )
}

function MarkdownReportDrawer({ report, onClose, triggerRef }) {
  const closeRef = useRef(null)
  useEffect(() => {
    closeRef.current?.focus()
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const dialog = closeRef.current?.closest('[role="dialog"]')
      const focusable = dialog ? [...dialog.querySelectorAll('button:not(:disabled), [tabindex="0"]')] : []
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
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      triggerRef.current?.focus()
    }
  }, [onClose, triggerRef])

  return (
    <div className="project-skill-drawer-backdrop" onMouseDown={(event) => event.target === event.currentTarget && onClose()}>
      <section className="project-skill-drawer project-skills-report-drawer" role="dialog" aria-modal="true" aria-labelledby="project-skills-report-title">
        <header>
          <div>
            <span>只读派生视图</span>
            <h2 id="project-skills-report-title">项目 Skill Markdown 总览</h2>
          </div>
          <button ref={closeRef} type="button" className="icon-button" aria-label="关闭 Markdown 总览" onClick={onClose}><X size={18} /></button>
        </header>
        <p className="project-skill-drawer-boundary">内容已经服务端按当前快照同代校验；页面不暴露生成目录，也不提供编辑或回写。</p>
        <div className="project-skill-drawer-content">
          <pre className="project-skills-markdown-content" tabIndex={0} aria-label="Markdown 总览内容">{report.content}</pre>
        </div>
      </section>
    </div>
  )
}

function Metrics({ snapshot, onOpenClassification }) {
  const metrics = projectMetrics(snapshot)
  const rows = [
    { key: 'registeredProjectCount', label: '登记项目', Icon: Folder, classification: 'project' },
    { key: 'containerCount', label: '容器', Icon: Boxes, classification: 'container' },
    { key: 'logicalSkillCount', label: '项目内逻辑 Skill', Icon: Sparkles },
    { key: 'unclassifiedCandidateCount', label: '未分类候选', Icon: CircleHelp },
  ]
  return (
    <section className="project-skills-metrics" aria-label="项目 Skill 计数">
      {rows.map(({ key, label, Icon, classification }) => classification ? (
        <button
          className="project-skill-metric-card project-skill-metric-action"
          type="button"
          aria-label={`查看${label}详情`}
          onClick={(event) => onOpenClassification(classification, event.currentTarget)}
          key={key}
        >
          <Icon size={18} />
          <div><span>{label}</span><strong>{metrics[key] || 0}</strong></div>
          <ChevronRight size={15} className="project-metric-chevron" />
        </button>
      ) : (
        <article className="project-skill-metric-card" key={key}>
          <Icon size={18} />
          <div><span>{label}</span><strong>{metrics[key] || 0}</strong></div>
        </article>
      ))}
    </section>
  )
}

function ClassificationDrawer({ classification, projects, snapshot, onClose, triggerRef }) {
  const closeRef = useRef(null)
  const isProject = classification === 'project'
  const Icon = isProject ? Folder : Boxes
  const title = isProject ? '登记项目' : '登记容器'

  useEffect(() => {
    closeRef.current?.focus()
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const dialog = closeRef.current?.closest('[role="dialog"]')
      const focusable = dialog ? [...dialog.querySelectorAll('button:not(:disabled), [tabindex="0"]')] : []
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
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      triggerRef.current?.focus()
    }
  }, [onClose, triggerRef])

  return createPortal(
    <>
      <button className="drawer-scrim" aria-label={`关闭${title}详情`} onClick={onClose} />
      <aside className="detail-drawer project-classification-drawer" role="dialog" aria-modal="true" aria-labelledby="project-classification-drawer-title">
        <header className="drawer-header">
          <button ref={closeRef} type="button" className="icon-button" aria-label={`关闭${title}详情`} onClick={onClose}><X size={20} /></button>
        </header>
        <div className="drawer-content">
          <div className="drawer-title">
            <span className="asset-icon asset-skill"><Icon size={22} /></span>
            <div><h2 id="project-classification-drawer-title">{title}</h2><span>{projects.length} 个对象 · 只读登记信息</span></div>
          </div>
          <div className="locked-banner"><LockKeyhole size={16} /> 锁定 · 只读观察</div>
          <ProjectDrawerSection title={isProject ? '独立项目' : '容器对象'}>
            <p>{isProject ? '只有明确登记为项目的对象才会读取批准的固定入口。' : '容器只用于界定层级，不递归扫描其中的历史目录或嵌套项目。'}</p>
          </ProjectDrawerSection>
          <ProjectDrawerSection title="对象详情">
            <div className="project-classification-object-list">
              {projects.map((project) => {
                const summary = projectSkillSummary(project, snapshot)
                return (
                  <article key={project.project_id}>
                    <header>
                      <span className="project-classification-object-icon"><Icon size={17} /></span>
                      <div><strong>{project.display_name}</strong><code>{project.project_id}</code></div>
                      <span className={`project-scan-label status-${statusTone(project.scan_status)}`}>{projectScanStatusLabel(project.scan_status)}</span>
                    </header>
                    <div className="path-row"><code>{project.relative_path}</code></div>
                    <dl className="identity-grid">
                      <div><dt>分类</dt><dd>{PROJECT_CLASSIFICATION_LABELS[project.classification]}</dd></div>
                      <div><dt>扫描资格</dt><dd>{isProject ? '固定入口只读观察' : '不扫描'}</dd></div>
                      <div><dt>逻辑 Skill</dt><dd>{summary.logicalSkillCount}</dd></div>
                      <div><dt>文件观察</dt><dd>{summary.fileObservationCount}</dd></div>
                    </dl>
                  </article>
                )
              })}
            </div>
          </ProjectDrawerSection>
        </div>
        <footer className="drawer-footer"><ShieldCheck size={15} /> 登记分类不代表宿主已加载或可调用</footer>
      </aside>
    </>,
    document.body,
  )
}

function CandidateProjects({ candidates }) {
  if (!candidates?.length) return null
  return (
    <details className="project-skills-candidates">
      <summary>
        <Database size={17} />
        <span><strong>{candidates.length} 个未分类候选</strong><small>仅显示一级目录，不读取其 Skill 内容</small></span>
        <ChevronDown size={16} />
      </summary>
      <ul>
        {candidates.map((candidate) => (
          <li key={candidate.candidate_id}>
            <Folder size={15} />
            <code>{candidate.relative_path}</code>
            <span>{PROJECT_CLASSIFICATION_LABELS[candidate.status] || '未分类'}</span>
          </li>
        ))}
      </ul>
    </details>
  )
}

function EvidenceLayer({ layer, layerLabel, status, label, detail }) {
  return (
    <div className={`project-skill-evidence-layer evidence-${evidenceTone(status)}`}>
      <dt>{layerLabel || EVIDENCE_LAYER_LABELS[layer] || layer}</dt>
      <dd>
        <strong>{label || evidenceStateLabel(layer, status)}</strong>
        {detail && <span>{detail}</span>}
      </dd>
    </div>
  )
}

function projectObservationSourceLabel(observation) {
  const labels = {
    entity: '项目入口',
    same_project_symlink: '同项目链接入口',
    plugin_bundled_source: '插件包来源',
    source_only: '普通来源目录',
    observed_path_only: '特定路径观察',
  }
  return labels[observation.source_kind] || '文件观察'
}

function Observation({ observation, evidenceRow }) {
  return (
    <article className="project-skill-observation project-skill-binding-card">
      <div className="project-skill-binding-heading">
        <span className="project-skill-binding-icon"><FileText size={16} /></span>
        <div>
          <strong>{projectObservationSourceLabel(observation)}</strong>
          <code>{observation.manifest_relative_path}</code>
        </div>
        <span className="type-badge">{observation.file_type === 'symlink' ? '同项目链接' : '普通文件'}</span>
      </div>
      <dl className="project-skill-evidence-grid" aria-label="Skill 五层证据">
        {evidenceRow.layers.map((layer) => <EvidenceLayer {...layer} key={layer.layer} />)}
      </dl>
      <div className="path-row project-skill-entry-path"><span>入口</span><code>{observation.entry?.relative_path}</code></div>
      {observation.resolved_relative_path && <div className="path-row"><span>链接目标</span><code>{observation.resolved_relative_path}</code></div>}
    </article>
  )
}

function ProjectDrawerSection({ title, tag, children }) {
  return <section className="drawer-section"><h3>{title}{tag ? <span className="ai-generated-tag">{tag}</span> : null}</h3>{children}</section>
}

function SkillEvidenceDrawer({ skill, project, scenario, onClose, triggerRef }) {
  const closeRef = useRef(null)
  useEffect(() => {
    closeRef.current?.focus()
    const handleKeyDown = (event) => {
      if (event.key === 'Escape') {
        event.preventDefault()
        onClose()
        return
      }
      if (event.key !== 'Tab') return
      const dialog = closeRef.current?.closest('[role="dialog"]')
      const focusable = dialog ? [...dialog.querySelectorAll('button:not(:disabled), [tabindex="0"]')] : []
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
    return () => {
      document.removeEventListener('keydown', handleKeyDown)
      triggerRef.current?.focus()
    }
  }, [onClose, triggerRef])

  const evidenceRows = evidenceRowsForSkill(skill)
  const projection = skill.observations?.[0]?.frontmatter_projection || {}
  const localization = skill.localization || { status: 'missing', coverage_complete: false }
  const localized = localization.coverage_complete === true
  const aiDraft = localized && localization.status === 'ai_draft'
  const localizationLabel = localization.status === 'reviewed'
    ? '中文覆盖 · 已复核'
    : localization.status === 'ai_draft'
      ? '中文覆盖 · AI 生成'
      : localization.status === 'stale'
        ? '译文待更新'
        : '暂无中文覆盖'
  const firstObservation = skill.observations?.[0] || {}
  const fingerprintStatus = firstObservation.projection_sha256_v1 ? 'complete' : '未生成'
  return createPortal(
    <>
      <button className="drawer-scrim" aria-label="关闭详情" onClick={onClose} />
      <aside className="detail-drawer project-skill-library-drawer" role="dialog" aria-modal="true" aria-labelledby="project-skill-drawer-title">
        <header className="drawer-header">
          <button ref={closeRef} type="button" className="icon-button" aria-label="关闭详情" onClick={onClose}><X size={20} /></button>
        </header>
        <div className="drawer-content">
          <div className="drawer-title">
            <span className="asset-icon asset-skill"><Sparkles size={22} /></span>
            <div><h2 id="project-skill-drawer-title">{localized ? localization.zh_name : skill.display_name}</h2><span>{skill.display_name} · {project.display_name} · Skill</span></div>
          </div>
          <div className="locked-banner"><LockKeyhole size={16} /> 锁定 · 项目内只读观察</div>
          <div className={`project-skill-localization-status status-${localization.status}`}>{localizationLabel}</div>
          <ProjectDrawerSection title="能力简介" tag={aiDraft ? 'AI 生成' : undefined}>
            <p>{localized ? localization.summary : projection.description || '已安全读取受限元数据，未读取 Skill 正文。'}</p>
            {localized && projection.description && (
              <p className="project-skill-original-text"><span>frontmatter 原文</span>{projection.description}</p>
            )}
            {!localized && <p className="project-skill-detail-empty">当前显示 frontmatter 原文；中文覆盖缺失或已过期。</p>}
          </ProjectDrawerSection>
          <ProjectDrawerSection title="适用场景" tag={aiDraft ? 'AI 生成' : undefined}>
            <ul className="detail-list">
              {(localized ? localization.use_cases : [scenario.name]).map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}
            </ul>
          </ProjectDrawerSection>
          <ProjectDrawerSection title="不适用场景" tag={aiDraft ? 'AI 生成' : undefined}>
            {localized
              ? <ul className="detail-list">{localization.not_for.map((item, index) => <li key={`${index}:${item}`}>{item}</li>)}</ul>
              : <p className="project-skill-detail-empty">暂无中文覆盖；当前不从 Skill 正文推断不适用场景。</p>}
          </ProjectDrawerSection>
          <ProjectDrawerSection title="使用示意" tag={aiDraft ? 'AI 生成' : undefined}>
            {localized
              ? <div className="usage-example-list">{localization.examples.map((item, index) => <blockquote key={`${index}:${item}`}>{item}</blockquote>)}</div>
              : <p className="project-skill-detail-empty">暂无中文覆盖；只读观察未读取 Skill 正文或 examples/。</p>}
          </ProjectDrawerSection>
          <ProjectDrawerSection title="项目观察关系（不代表宿主可用）">
            <div className="binding-list project-skill-drawer-content">
              {(skill.observations || []).map((observation, index) => (
                <Observation observation={observation} evidenceRow={evidenceRows[index]} key={observation.observation_id} />
              ))}
            </div>
          </ProjectDrawerSection>
          <ProjectDrawerSection title="来源与身份">
            <dl className="identity-grid">
              <div><dt>来源</dt><dd>{projectObservationSourceLabel(firstObservation)}</dd></div>
              <div><dt>大类目</dt><dd>{scenario.category}</dd></div>
              <div><dt>子类目</dt><dd>{scenario.name}</dd></div>
              <div><dt>版本</dt><dd>{projection.version || '未声明'}</dd></div>
              <div><dt>作者</dt><dd>{projection.author || '未声明'}</dd></div>
              <div><dt>许可证</dt><dd>{projection.license || '未声明'}</dd></div>
              <div><dt>受限投影</dt><dd>frontmatter-only</dd></div>
              <div><dt>投影指纹</dt><dd>{fingerprintStatus}</dd></div>
            </dl>
            <div className="path-row"><code>{firstObservation.manifest_relative_path}</code></div>
            <dl className="identity-grid project-skill-identity-secondary">
              <div><dt>项目</dt><dd>{project.display_name}</dd></div>
              <div><dt>机器证据</dt><dd>{skill.observations?.length || 0} 条</dd></div>
            </dl>
          </ProjectDrawerSection>
        </div>
        <footer className="drawer-footer"><ShieldCheck size={15} /> 文件发现不代表已安装、可调用或实际使用</footer>
      </aside>
    </>,
    document.body,
  )
}

function LogicalSkill({ skill, project, scenario }) {
  const projection = skill.observations?.[0]?.frontmatter_projection || {}
  const localization = skill.localization || { status: 'missing', coverage_complete: false }
  const localized = localization.coverage_complete === true
  const aiDraft = localized && localization.status === 'ai_draft'
  const bindingStates = [...new Set((skill.observations || []).map(
    (observation) => observation.evidence?.project_binding?.status,
  ).filter(Boolean))]
  const [drawerOpen, setDrawerOpen] = useState(false)
  const triggerRef = useRef(null)
  return (
    <article className="asset-card project-skill-library-card">
      <button ref={triggerRef} className="asset-card-main" type="button" onClick={() => setDrawerOpen(true)}>
        <span className="asset-icon asset-skill"><Sparkles size={20} /></span>
        <span className="asset-copy">
          <span className="asset-title-row"><strong>{localized ? localization.zh_name : skill.display_name}</strong></span>
          <small className="source-name">{skill.display_name} · {project.display_name}</small>
          <span className="asset-summary">{localized ? localization.summary : projection.description || '已安全读取受限元数据，未读取正文。'}</span>
        </span>
        <ChevronRight className="card-chevron" size={17} />
      </button>
      <footer className="asset-card-footer">
        <span className="classification-badges">
          <span className="type-badge">Skill</span>
          <span className="subcategory-badge">{scenario.name}</span>
          {aiDraft && <span className="localization-chip">中文 · AI 生成</span>}
        </span>
        <div className="project-skill-card-evidence" aria-label="项目绑定证据">
          {bindingStates.map((status) => <span key={status}>{evidenceStateLabel('project_binding', status)}</span>)}
        </div>
      </footer>
      {drawerOpen && (
        <SkillEvidenceDrawer skill={skill} project={project} scenario={scenario} triggerRef={triggerRef} onClose={() => setDrawerOpen(false)} />
      )}
    </article>
  )
}

function ProjectScenarioSection({ group, project, index }) {
  const headingId = `project-scenario-${index}`
  const single = group.skills.length === 1
  return (
    <section className="scenario-section project-scenario-section" aria-labelledby={headingId}>
      <header className="scenario-heading">
        <div className="scenario-heading-main">
          <span className="scenario-parent">{group.category}</span>
          <div className="scenario-title-row">
            <h3 id={headingId}>{group.name}</h3>
            <span className={`scenario-relation ${single ? 'relation-single' : 'relation-unknown'}`}>
              {single ? '单项' : '关系未判定'}
            </span>
          </div>
          <p>{group.description}</p>
        </div>
        <div className="scenario-counts"><span className="current-count">当前 Skill {group.skills.length}</span></div>
      </header>
      <div className="capability-grid scenario-grid">
        {group.skills.map((skill) => (
          <LogicalSkill key={skill.logical_skill_id} skill={skill} project={project} scenario={projectSkillScenario(skill)} />
        ))}
      </div>
    </section>
  )
}

const OTHER_PROJECT_FOLDER_VALUE = '__choose_other_project_folder__'

function ProjectFolderSelection({ projects, value, onChange, onChooseOtherFolder, hasSelection }) {
  return (
    <section className="project-folder-selection" aria-label="项目文件夹选择">
      <div>
        <span className="project-folder-selection-icon"><Folder size={19} /></span>
        <div>
          <strong>选择项目文件夹</strong>
          <small>{hasSelection ? '一次只展示一个项目；可切换到其他项目。' : '一次只展示一个项目中的场景分组与 Skill 卡片；不选择时不展开任何项目内容。'}</small>
        </div>
      </div>
      <label>
        <span className="sr-only">选择项目文件夹</span>
        <select
          aria-label="选择项目文件夹"
          value={value}
          onChange={(event) => {
            if (event.target.value === OTHER_PROJECT_FOLDER_VALUE) {
              onChooseOtherFolder?.(event.currentTarget)
              return
            }
            onChange(event.target.value)
          }}
        >
          <option value="">请选择项目文件夹</option>
          {projects.map((project) => (
            <option value={project.project_id} key={project.project_id}>
              {project.temporary ? `临时：${project.display_name}` : project.display_name}
            </option>
          ))}
          <option value={OTHER_PROJECT_FOLDER_VALUE}>选择其他文件夹…</option>
        </select>
      </label>
    </section>
  )
}

function SelectedProjectSkills({ project, snapshot }) {
  const groups = projectSkillScenarioGroups(project)
  const metrics = projectSkillSummary(project, snapshot)
  const associations = projectHumanAssociations(snapshot, project.project_id)
  return (
    <section className="project-selected-library" aria-label={`${project.display_name} Skill`}>
      <div className="view-intro project-selected-intro">
        <div>
          <p className="section-kicker">当前项目</p>
          <h2>{project.display_name}</h2>
          <p>{metrics.logicalSkillCount} 个 Skill · {metrics.fileObservationCount} 条文件观察；按场景浏览，点击卡片查看详情。</p>
        </div>
        <code>{project.relative_path}</code>
      </div>
      {groups.length ? (
        <div className="scenario-collection project-scenario-collection">
          {groups.map((group, index) => <ProjectScenarioSection group={group} project={project} index={index} key={group.name} />)}
        </div>
      ) : (
        <StatePanel icon={CircleHelp} title="当前项目没有可展示的 Skill" detail="批准入口为空或未观察到 Skill，不代表扫描失败。" />
      )}
      <HumanAssociations associations={associations} />
    </section>
  )
}

function EntrySummary({ entries }) {
  return (
    <div className="project-skill-entry-list">
      {(entries || []).map((row) => (
        <div key={`${row.entry.kind}:${row.entry.relative_path}`}>
          <code>{row.entry.relative_path}</code>
          <span>{row.entry.host_hint || '通用来源'}</span>
          <b className={`status-${statusTone(row.status)}`}>{entryStatusLabel(row.status)}</b>
          <small>{row.observed_entry_count || 0} 条</small>
        </div>
      ))}
    </div>
  )
}

function HumanAssociations({ associations }) {
  return (
    <section className="project-skill-human" aria-label="人工关联">
      <header>
        <UserRound size={17} />
        <div><h4>人工关联</h4><p>人的记忆与机器事实分开显示，不改变五层证据。</p></div>
      </header>
      {associations.length ? (
        <ul>
          {associations.map((association) => (
            <li key={association.association_id}>
              <strong>{association.asset_id || association.unresolved_name}</strong>
              <span>{association.reason}</span>
              <small>人工关联 · 不代表项目内已安装或当前宿主可用 · {association.source}</small>
            </li>
          ))}
        </ul>
      ) : <p className="project-skill-human-empty">当前没有经评审登记的人工关联。</p>}
    </section>
  )
}

function ProjectRow({ project, snapshot, open, onToggle }) {
  const associations = projectHumanAssociations(snapshot, project.project_id)
  const metrics = projectSkillSummary(project, snapshot)
  const issues = (snapshot.issues || []).filter((issue) => issue.project_id === project.project_id)
  return (
    <article className={`project-skill-project${open ? ' is-open' : ''}`}>
      <button
        className="project-skill-project-summary"
        type="button"
        aria-expanded={open}
        onClick={onToggle}
      >
        <span className={`project-classification-icon classification-${project.classification}`}>
          {project.classification === 'archive' ? <Archive size={18} /> : <Folder size={18} />}
        </span>
        <span className="project-skill-project-name">
          <strong>{project.display_name}</strong>
          <small><code>{project.relative_path}</code></small>
        </span>
        <span className="project-classification-label">
          {PROJECT_CLASSIFICATION_LABELS[project.classification] || project.classification}
        </span>
        <span className={`project-scan-label status-${statusTone(project.scan_status)}`}>
          {projectScanStatusLabel(project.scan_status)}
        </span>
        <span className="project-skill-project-counts">
          <b>{metrics.logicalSkillCount}</b> 逻辑 Skill
          <i aria-hidden="true" />
          <b>{metrics.fileObservationCount}</b> 文件观察
        </span>
        <ChevronDown size={17} />
      </button>
      {open && (
        <div className="project-skill-project-detail">
          <div className="project-detail-metrics">
            <span><strong>{metrics.logicalSkillCount}</strong>项目内逻辑 Skill</span>
            <span><strong>{metrics.fileObservationCount}</strong>文件观察</span>
            <span><strong>{metrics.projectBindingEvidenceCount}</strong>项目绑定证据</span>
            <span><strong>{metrics.humanAssociationCount}</strong>人工关联</span>
          </div>

          <section className="project-skill-machine" aria-label="机器观察">
            <header>
              <Layers3 size={17} />
              <div><h4>机器观察</h4><p>逻辑 Skill {metrics.logicalSkillCount} · 依据批准入口的磁盘事实；低层证据不自动升级高层结论。</p></div>
            </header>
            <EntrySummary entries={project.entries} />
            {project.logical_skills?.length ? (
              <div className="project-skill-logical-list">
                {project.logical_skills.map((skill) => (
                  <LogicalSkill skill={skill} key={skill.logical_skill_id} />
                ))}
              </div>
            ) : (
              <div className="project-skill-inline-empty">
                <CircleHelp size={18} />
                <span>当前批准范围内没有观察到项目 Skill。空入口不是扫描失败。</span>
              </div>
            )}
          </section>

          <HumanAssociations associations={associations} />

          {issues.length > 0 && (
            <section className="project-skill-issues" aria-label="项目观察问题">
              <header><AlertTriangle size={17} /><h4>本项目观察问题</h4></header>
              <ul>{issues.map((issue) => <li key={issue.issue_id}>{issue.message}</li>)}</ul>
            </section>
          )}
        </div>
      )}
    </article>
  )
}

function ChangesSummary({ changes }) {
  if (!changes) return null
  const summary = projectSkillChangeSummary(changes)
  const compared = summary.status === 'compared'
  return (
    <details className="project-skills-changes">
      <summary>
        <Boxes size={17} />
        <span>
          <strong>{compared ? '最近完整变化' : '变化基线'}</strong>
          <small>{compared ? '只比较两份完整快照' : '尚无可比较的完整基线'}</small>
        </span>
        <b>{compared ? `${summary.addedCount} 新增 · ${summary.changedCount} 变化 · ${summary.removedCount} 未再观察` : summary.label}</b>
        <ChevronDown size={16} />
      </summary>
      {compared && (
        <div className="project-skills-change-grid">
          {[
            ['新增', summary.added], ['内容变化', summary.changed], ['未再观察', summary.removed],
          ].map(([label, rows]) => (
            <div key={label}><strong>{label}</strong><span>{rows.length}</span>{rows.length > 0 && <ul>{rows.map((id) => <li key={id}><code>{id}</code></li>)}</ul>}</div>
          ))}
        </div>
      )}
    </details>
  )
}

export default function ProjectSkillsView({
  snapshot,
  attempt,
  integrity,
  report,
  loading = false,
  refreshing = false,
  error = null,
  onRefresh,
  onReload,
  temporaryProjectSkill,
  onChooseOtherFolder,
}) {
  const [selectedProjectId, setSelectedProjectId] = useState('')
  const [reportOpen, setReportOpen] = useState(false)
  const [classificationDrawer, setClassificationDrawer] = useState(null)
  const reportTriggerRef = useRef(null)
  const classificationTriggerRef = useRef(null)

  const temporaryProject = temporaryProjectSkill?.snapshot?.projects?.[0]
  const selectableProjects = useMemo(() => {
    const registered = (snapshot?.projects || []).filter((project) => project.classification === 'project')
    return temporaryProject
      ? [...registered, { ...temporaryProject, temporary: true }]
      : registered
  }, [snapshot, temporaryProject])
  const selectedProject = selectableProjects.find((project) => project.project_id === selectedProjectId) || null
  const selectedProjectSnapshot = selectedProject?.temporary ? temporaryProjectSkill.snapshot : snapshot
  const classificationProjects = useMemo(
    () => (snapshot?.projects || []).filter((project) => project.classification === classificationDrawer),
    [classificationDrawer, snapshot],
  )

  useEffect(() => {
    if (selectedProjectId && !selectedProject) setSelectedProjectId('')
  }, [selectedProject, selectedProjectId])

  useEffect(() => {
    if (temporaryProject?.project_id) setSelectedProjectId(temporaryProject.project_id)
  }, [temporaryProject?.project_id])

  if (loading && !snapshot) {
    return (
      <section className="project-skills-view project-skills-loading" aria-label="正在读取项目 Skill">
        <div className="skeleton skeleton-strip" />
        <div className="project-skills-metrics">
          {Array.from({ length: 5 }, (_, index) => <div className="skeleton skeleton-metric" key={index} />)}
        </div>
        <div className="skeleton skeleton-panel" />
      </section>
    )
  }

  if (!snapshot) {
    const failedAttempt = attempt && attempt.scan_status !== 'complete'
    return (
      <section className="project-skills-view project-skills-empty">
        <div className="view-intro">
          <div>
            <p className="section-kicker">只读观察</p>
            <h2>项目 Skill</h2>
            <p>分开查看项目内机器观察与经评审的人工关联。</p>
          </div>
        </div>
        {failedAttempt ? (
          <StatePanel icon={AlertTriangle} tone="warning" title="首次观察未建立完整快照" detail="本次未能确认，且尚无上一份完整结果可保留。项目内容不会因此被推断为空。">
            <span className={`project-attempt-badge status-${statusTone(attempt.scan_status)}`}>
              最近尝试：{projectScanStatusLabel(attempt.scan_status)}
            </span>
            {attempt.issues?.length > 0 && (
              <ul className="project-skills-attempt-issues">
                {attempt.issues.map((issue, index) => (
                  <li key={`${issue.status}:${issue.code}:${issue.project_id || ''}:${issue.relative_path || ''}:${index}`}>
                    <code>{issue.code}</code>
                    {issue.project_id && <span>项目 {issue.project_id}</span>}
                    {issue.relative_path && <span>路径 <code>{issue.relative_path}</code></span>}
                  </li>
                ))}
              </ul>
            )}
          </StatePanel>
        ) : error ? (
          <StatePanel icon={CircleAlert} tone="error" title="项目 Skill 数据未能读取" detail={error || '本地服务没有返回可验证的项目观察数据。'} />
        ) : (
          <StatePanel icon={Database} title="尚未建立项目 Skill 观察快照" detail="手动刷新只会观察批准的项目与固定入口，结果仅保存在 AI-Toolbox 的专用生成目录。" />
        )}
        {error && onReload && <button className="secondary-button" type="button" onClick={onReload}>重新读取</button>}
        <button className="refresh-button project-skills-first-refresh" aria-label="刷新项目 Skill" type="button" onClick={onRefresh} disabled={refreshing || !onRefresh}>
          <RefreshCw size={17} className={refreshing ? 'spin' : ''} />
          {refreshing ? '正在手动观察…' : '手动刷新项目观察'}
        </button>
      </section>
    )
  }

  return (
    <section className="project-skills-view">
      <div className="view-intro project-skills-intro">
        <div>
          <p className="section-kicker">只读观察</p>
          <h2>项目 Skill</h2>
          <p>回答“哪个项目观察到什么、证据到哪一层”；项目与宿主保持不变。</p>
        </div>
        <button className="secondary-button project-skills-refresh" aria-label="刷新项目 Skill" type="button" onClick={onRefresh} disabled={refreshing || !onRefresh}>
          <RefreshCw size={17} className={refreshing ? 'spin' : ''} />
          {refreshing ? '观察中…' : '手动刷新项目观察'}
        </button>
      </div>

      {error && (
        <div className="project-skills-api-error" role="alert">
          <CircleAlert size={17} />
          <span>{error} 页面继续显示已验证的本地快照。</span>
          {onReload && <button type="button" className="text-button" onClick={onReload}>重新读取</button>}
        </div>
      )}

      <ProjectFolderSelection
        projects={selectableProjects}
        value={selectedProjectId}
        onChange={setSelectedProjectId}
        onChooseOtherFolder={onChooseOtherFolder}
        hasSelection={Boolean(selectedProject)}
      />

      {selectedProject && (
        <SelectedProjectSkills project={selectedProject} snapshot={selectedProjectSnapshot} />
      )}

      <StatusSummary
        snapshot={snapshot}
        attempt={attempt}
        integrity={integrity}
        report={report}
        reportTriggerRef={reportTriggerRef}
        onOpenReport={() => setReportOpen(true)}
      />
      <Metrics
        snapshot={snapshot}
        onOpenClassification={(classification, trigger) => {
          classificationTriggerRef.current = trigger
          setClassificationDrawer(classification)
        }}
      />

      <CandidateProjects candidates={snapshot.candidates} />
      <ChangesSummary changes={snapshot.changes} />

      {snapshot.issues?.length > 0 && (
        <section className="project-skills-global-issues" aria-label="项目 Skill 观察问题">
          <header><AlertTriangle size={17} /><div><h2>观察问题</h2><p>partial、error 与 security reject 不会被解释为对象删除。</p></div></header>
          <ul>{snapshot.issues.map((issue) => <li key={issue.issue_id}><code>{issue.code}</code><span>{issue.message}</span></li>)}</ul>
        </section>
      )}
      {reportOpen && report?.available && report?.content && (
        <MarkdownReportDrawer report={report} triggerRef={reportTriggerRef} onClose={() => setReportOpen(false)} />
      )}
      {classificationDrawer && (
        <ClassificationDrawer
          classification={classificationDrawer}
          projects={classificationProjects}
          snapshot={snapshot}
          triggerRef={classificationTriggerRef}
          onClose={() => setClassificationDrawer(null)}
        />
      )}
    </section>
  )
}
