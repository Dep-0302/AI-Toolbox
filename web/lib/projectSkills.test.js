import { describe, expect, it } from 'vitest'
import {
  adaptProjectSkillsEnvelope,
  EVIDENCE_LAYER_ORDER,
  evidenceRowsForSkill,
  evidenceStateLabel,
  filterProjects,
  projectCategories,
  projectHumanAssociations,
  projectMetrics,
  projectSkillChangeSummary,
  projectSkillScenario,
  projectSkillScenarioGroups,
  projectSkillStatusText,
} from './projectSkills'

function observation(id, binding = 'observed_binding', overrides = {}) {
  return {
    observation_id: `observation:${id}`,
    manifest_relative_path: `.agents/skills/${id}/SKILL.md`,
    source_kind: binding === 'plugin_bundled_source' ? 'plugin_bundled_source' : 'entity',
    file_type: 'regular_file',
    link_status: 'not_applicable',
    frontmatter_projection: { name: id, description: `${id} description` },
    evidence: {
      file_discovery: { status: 'observed' },
      project_binding: { status: binding },
      host_availability: { status: 'unverified' },
      invocation_eligibility: { status: 'not_observed' },
      actual_use: { status: 'not_connected' },
    },
    ...overrides,
  }
}

function envelope() {
  const shared = observation('shared', 'plugin_bundled_source')
  const linked = observation('shared-link', 'observed_binding', {
    file_type: 'symlink',
    link_status: 'healthy_same_project',
    source_kind: 'same_project_symlink',
    openai_declaration: { status: 'declared', allow_implicit_invocation: false },
    evidence: {
      ...shared.evidence,
      project_binding: { status: 'observed_binding' },
      invocation_eligibility: { status: 'declaration_only' },
    },
  })
  return {
    snapshot: {
      generation_id: '1234567890abcdef',
      generated_at: '2026-08-11T12:00:00Z',
      scan_status: 'complete',
      candidates: [{ candidate_id: 'candidate:one', relative_path: '009-PPT', status: 'unclassified' }],
      projects: [
        {
          project_id: 'project-alpha',
          display_name: '示例项目 Alpha',
          relative_path: 'example-project-alpha',
          classification: 'project',
          scan_status: 'complete',
          entries: [{ status: 'observed', issue_refs: [] }],
          logical_skills: [
            { logical_skill_id: 'project-skill:shared', display_name: 'shared', observations: [shared, linked] },
          ],
        },
        {
          project_id: 'project-beta',
          display_name: '示例项目 Beta',
          relative_path: 'example-project-beta',
          classification: 'project',
          scan_status: 'complete',
          entries: [{ status: 'empty', issue_refs: [] }],
          logical_skills: [],
        },
        {
          project_id: 'project-gamma',
          display_name: '示例项目 Gamma',
          relative_path: 'example-project-gamma',
          classification: 'project',
          scan_status: 'complete',
          entries: [{ status: 'observed', issue_refs: [] }],
          logical_skills: [
            {
              logical_skill_id: 'project-skill:pull',
              display_name: 'pull',
              observations: [observation('pull', 'observed_path_only', { source_kind: 'observed_path_only' })],
            },
          ],
        },
        {
          project_id: 'container-example',
          display_name: '示例容器',
          relative_path: 'example-container',
          classification: 'container',
          scan_status: 'not_scanned',
          entries: [],
          logical_skills: [],
        },
      ],
      human_associations: {
        items: [
          {
            association_id: 'human-association:project-beta:skill:apple-design',
            project_id: 'project-beta',
            asset_id: 'skill:apple-design',
            reason: '人工确认的设计关联',
          },
        ],
      },
      issues: [],
      changes: {
        status: 'compared',
        compared_to_generation_id: 'fedcba0987654321',
        added: ['project-skill:new'],
        changed: [],
        removed: ['project-skill:old'],
      },
    },
    last_attempt: {
      scan_status: 'complete',
      promoted: true,
      complete_snapshot_generation_id: '1234567890abcdef',
    },
    report: { available: true, generation_id: '1234567890abcdef' },
    integrity: { degraded: false, errors: [] },
  }
}

describe('project Skill data adapter', () => {
  it('uses approved Chinese display metadata for scenario matching and search', () => {
    const source = envelope()
    const skill = source.snapshot.projects[0].logical_skills[0]
    skill.localization = {
      status: 'reviewed',
      coverage_complete: true,
      zh_name: '三阶段内容生产编排',
      summary: '连接剧本、分镜与视频提示词的多阶段工作流。',
      use_cases: ['审核每个生产阶段'],
      not_for: ['跳过人工确认'],
      examples: ['从剧本阶段开始。'],
    }
    expect(projectSkillScenario(skill).name).toBe('工具链与工作流设计')
    expect(filterProjects(source.snapshot, { query: '跳过人工确认' })).toHaveLength(1)
  })

  it('keeps four counts separate and excludes human associations from machine counts', () => {
    const source = envelope()
    const metrics = projectMetrics(source.snapshot)
    expect(metrics).toMatchObject({
      registeredObjectCount: 4,
      registeredProjectCount: 3,
      containerCount: 1,
      logicalSkillCount: 2,
      fileObservationCount: 3,
      projectBindingEvidenceCount: 1,
      humanAssociationCount: 1,
      unclassifiedCandidateCount: 1,
    })
    expect(projectHumanAssociations(source.snapshot, 'project-beta')).toHaveLength(1)
    expect(projectHumanAssociations(source.snapshot, 'project-alpha')).toEqual([])
  })

  it('renders all five evidence layers without upgrading unavailable states', () => {
    const skill = envelope().snapshot.projects[0].logical_skills[0]
    const rows = evidenceRowsForSkill(skill)
    expect(rows[0].layers.map((row) => row.layer)).toEqual(EVIDENCE_LAYER_ORDER)
    expect(evidenceStateLabel('file_discovery', 'observed')).toBe('已观察到文件')
    expect(evidenceStateLabel('host_availability', 'unverified')).toBe('宿主可用性未核实')
    expect(evidenceStateLabel('invocation_eligibility', 'declaration_only')).toBe(
      '仅 Skill 自身调用声明',
    )
    expect(evidenceStateLabel('actual_use', 'not_connected')).toBe('使用证据未接入')
    expect(rows[1].layers.find((layer) => layer.layer === 'invocation_eligibility').detail).toContain(
      '不代表宿主当前会匹配或已调用',
    )
    const labels = rows.flatMap((row) => row.layers.map((layer) => layer.label)).join(' ')
    expect(labels).not.toMatch(/已安装|已启用|已使用|可调用/)
    expect(evidenceStateLabel('host_availability', 'invented')).toBe('证据状态未核实')
  })

  it('preserves one logical Skill with multiple machine observations', () => {
    const adapted = adaptProjectSkillsEnvelope(envelope())
    const alpha = adapted.projects.find((project) => project.project_id === 'project-alpha')
    expect(alpha.metrics.logicalSkillCount).toBe(1)
    expect(alpha.metrics.fileObservationCount).toBe(2)
    expect(alpha.metrics.projectBindingEvidenceCount).toBe(1)
    expect(alpha.metrics.pluginSourceEvidenceCount).toBe(1)
  })

  it('keeps the 008 human association separate from machine observations and evidence filters', () => {
    const adapted = adaptProjectSkillsEnvelope(envelope())
    const uiProject = adapted.projects.find((project) => project.project_id === 'project-beta')
    expect(uiProject.humanAssociations[0].asset_id).toBe('skill:apple-design')
    expect(uiProject.metrics.logicalSkillCount).toBe(0)
    expect(uiProject.metrics.fileObservationCount).toBe(0)
    expect(filterProjects(adapted.projects, { evidence: 'observed_binding' })).not.toContain(uiProject)
    expect(filterProjects(adapted.projects, { query: 'apple-design' })).toContain(uiProject)
  })

  it('does not count observed_path_only as a project binding', () => {
    const adapted = adaptProjectSkillsEnvelope(envelope())
    const gamma = adapted.projects.find((project) => project.project_id === 'project-gamma')
    expect(gamma.metrics.projectBindingEvidenceCount).toBe(0)
    expect(gamma.metrics.pathOnlyEvidenceCount).toBe(1)
    expect(filterProjects(adapted.projects, { evidence: 'observed_path_only' })).toEqual([gamma])
  })

  it('filters by combined search, classification, entry, link, scan, and evidence facts', () => {
    const source = envelope()
    expect(filterProjects(source.snapshot, { query: 'Alpha shared', classification: 'project' })).toHaveLength(1)
    expect(filterProjects(source.snapshot, { entryStatus: 'empty' }).map((row) => row.project_id)).toEqual([
      'project-beta',
    ])
    expect(filterProjects(source.snapshot, { linkStatus: 'healthy_same_project' }).map((row) => row.project_id)).toEqual([
      'project-alpha',
    ])
    expect(filterProjects(source.snapshot, { scanStatus: 'not_scanned' }).map((row) => row.project_id)).toEqual([
      'container-example',
    ])
    expect(projectCategories(source.snapshot)).toEqual([
      { value: 'project', label: '项目', count: 3 },
      { value: 'container', label: '容器', count: 1 },
    ])
  })

  it('derives safe change summaries without treating incomplete absence as removal', () => {
    const compared = projectSkillChangeSummary(envelope().snapshot)
    expect(compared).toMatchObject({ addedCount: 1, changedCount: 0, removedCount: 1 })
    const unavailable = projectSkillChangeSummary({ status: 'not_available' })
    expect(unavailable.removed).toEqual([])
    expect(unavailable.label).toContain('暂无')
    const unknown = projectSkillChangeSummary({ status: 'unexpected', removed: ['unsafe'] })
    expect(unknown.removed).toEqual([])
  })

  it('describes retained last-known-good data and degraded auxiliary outputs honestly', () => {
    const source = envelope()
    source.last_attempt = { scan_status: 'partial', promoted: false }
    source.integrity = { degraded: true, errors: ['report_invalid'] }
    const text = projectSkillStatusText(source)
    expect(text).toContain('保留上次完整快照')
    expect(text).toContain('本次未能完整确认')
    expect(text).toContain('辅助输出完整性降级')
    expect(projectSkillStatusText({ snapshot: null, last_attempt: { scan_status: 'error' } })).toContain(
      '尚无可展示的完整快照',
    )
  })

  it('normalizes optional envelope fields without mutating the API payload', () => {
    const source = envelope()
    const original = structuredClone(source)
    const adapted = adaptProjectSkillsEnvelope(source)
    expect(adapted.report).toEqual({ available: true, generationId: '1234567890abcdef', content: null })
    expect(adapted.integrity).toEqual({ degraded: false, errors: [] })
    expect(adapted.status).toContain('只读观察快照')
    expect(source).toEqual(original)

    const empty = adaptProjectSkillsEnvelope({})
    expect(empty.projects).toEqual([])
    expect(empty.metrics.logicalSkillCount).toBe(0)
    expect(empty.status).toContain('尚无完整')
  })

  it('groups project Skills into presentation scenarios without inferring object relationships', () => {
    const project = {
      logical_skills: [
        {
          logical_skill_id: 'project-skill:commit',
          display_name: 'commit',
          observations: [observation('commit', 'observed_path_only', {
            frontmatter_projection: { name: 'commit', description: 'Create a well-formed git commit.' },
          })],
        },
        {
          logical_skill_id: 'project-skill:push',
          display_name: 'push',
          observations: [observation('push', 'observed_path_only', {
            frontmatter_projection: { name: 'push', description: 'Push the branch and update a pull request.' },
          })],
        },
        {
          logical_skill_id: 'project-skill:debug',
          display_name: 'debug',
          observations: [observation('debug', 'observed_path_only', {
            frontmatter_projection: { name: 'debug', description: 'Investigate stuck runs and failures.' },
          })],
        },
      ],
    }
    const groups = projectSkillScenarioGroups(project)
    expect(groups.map((group) => [group.name, group.skills.length])).toEqual([
      ['调试、诊断与重构', 1],
      ['GitHub 与代码协作', 2],
    ])
    expect(projectSkillScenario(project.logical_skills[0]).category).toBe('开发与代码')
  })
})
