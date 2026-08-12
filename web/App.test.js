import React, { act } from 'react'
import { createRoot } from 'react-dom/client'
import { afterEach, describe, expect, it, vi } from 'vitest'
import App from './App'

globalThis.IS_REACT_ACT_ENVIRONMENT = true

function response(payload, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    json: async () => payload,
  }
}

function snapshot(skillCount, generationId, items = []) {
  return {
    schema_version: 1,
    generation_id: generationId,
    generated_at: '2026-08-06T12:00:00Z',
    mode: 'observe',
    summary: {
      asset_count: skillCount,
      counts_by_type: {
        skill: skillCount,
        plugin: items.filter((item) => item.type === 'plugin').length,
        mcp: 0,
        cli: 0,
        sdk: 0,
      },
      host_binding_count: 0,
      localized_skill_count: 0,
      stale_localization_count: 0,
      locked_asset_count: skillCount,
      health_finding_count: 0,
      scan_error_count: 0,
    },
    hosts: [
      { id: 'codex', label: 'Codex', status: 'missing', safe_metadata_only: false },
      { id: 'claude', label: 'Claude', status: 'missing', safe_metadata_only: false },
      { id: 'hermes', label: 'Hermes', status: 'missing', safe_metadata_only: false },
      { id: 'workbuddy', label: 'WorkBuddy', status: 'missing', safe_metadata_only: false },
      { id: 'antigravity', label: 'Antigravity', status: 'placeholder', safe_metadata_only: true },
    ],
    items,
    health_findings: [],
    scan_scope: { roots: [], coverage: [], safety: {} },
    scan_errors: [],
  }
}

const health = {
  ok: true,
  csrf_token: 'fixture-csrf',
  api_version: 'v1',
  folder_picker: {
    available: true,
    provider: 'tkinter-native',
    reason: '',
  },
  source_session: {
    mode: 'default',
    display_path: '~/Example/Collection',
    source_key: 'default',
  },
  capabilities: {
    model_calls: false,
    background_watch: false,
  },
}

function collectionSnapshot(sourceCount = 6) {
  const base = (overrides) => ({
    source_id: 'src:fixture',
    name: 'fixture',
    relative_path: 'fixture',
    kind: 'archive',
    size_bytes: 2048,
    modified_at: '2026-08-09T20:00:00Z',
    origin_kind: 'unknown',
    origin_basis: 'unavailable',
    version: '',
    scenarios: [],
    capabilities: [],
    shape: { files: 5, documents: 2, scripts: 1, images: 0, other: 2 },
    host_links: [],
    installation_status: 'not_observed',
    usage_status: 'unrecorded',
    classification: {
      primary_type: 'unknown',
      functional_type: 'other_material',
      primary_scenario: null,
      display_label: '其他资料',
      component_types: [],
      is_composite: false,
      status: 'classified',
      confidence: 'high',
      readiness: 'reference_only',
      summary: 'Fixture 收藏对象。',
      basis: ['fixture'],
    },
    relations: [],
    proposed_bucket: '30_文档与方法资料',
    move_safety: 'review',
    duplicate_capability_count: 0,
    scan_errors: [],
    ...overrides,
  })
  const capability = (id, type, name, scenario) => ({
    capability_id: id,
    type,
    name,
    description: `${name} fixture`,
    version: '',
    relative_path: `${name}/SKILL.md`,
    scenario,
    fingerprint: id.repeat(64).slice(0, 64),
    entry_status: type === 'skill' ? 'standard' : 'supporting',
  })
  const items = [
    base({
      source_id: 'src:skill',
      name: '电影生图 Skill',
      relative_path: 'incoming/cinema.skill',
      kind: 'skill_archive',
      origin_kind: 'chat',
      origin_basis: 'collection_path',
      scenarios: ['电影感生图与视觉资产'],
      capabilities: [capability('a', 'skill', 'cinema-image', '电影感生图与视觉资产')],
      classification: {
        primary_type: 'skill_bundle', functional_type: 'skill', primary_scenario: '电影感生图与视觉资产',
        display_label: '标准 Skill', component_types: ['skill_bundle'], is_composite: false,
        status: 'classified', confidence: 'high', readiness: 'standard_source',
        summary: '单个电影感生图 Skill。', basis: ['检测到一个标准 SKILL.md'],
      },
    }),
    base({
      source_id: 'src:repo',
      name: 'sample-repo',
      relative_path: 'archive/sample-repo',
      kind: 'repository',
      origin_kind: 'github',
      origin_basis: 'git_remote',
      version: '2.0.0',
      scenarios: ['工具类'],
      capabilities: [capability('b', 'cli', 'repo-cli', '工具类')],
      classification: {
        primary_type: 'project', functional_type: 'project', primary_scenario: '工具类',
        display_label: '项目 / 整库', component_types: ['tool'], is_composite: false,
        status: 'classified', confidence: 'high', readiness: 'not_assessed',
        summary: '包含一个 CLI 的完整项目。', basis: ['检测到 Git 仓库'],
      },
      proposed_bucket: '20_整库与项目',
      relations: [
        { type: 'supersedes', target_relative_path: 'archive/sample-repo-v1', note: '2.0.0 升级版' },
      ],
    }),
    base({
      source_id: 'src:bundle',
      name: '剧本创作复合包',
      relative_path: '1.剧本skill.zip',
      origin_kind: 'website',
      origin_basis: 'download_metadata',
      scenarios: ['编剧与剧本'],
      capabilities: [
        capability('c', 'skill_candidate', '剧本总控', '编剧与剧本'),
        capability('d', 'skill_candidate', '剧本医生', '编剧与剧本'),
      ],
      classification: {
        primary_type: 'skill_bundle', functional_type: 'composite_skill_bundle', primary_scenario: '编剧与剧本',
        display_label: '剧本创作复合 Skill 组', component_types: ['skill_bundle', 'workflow'], is_composite: true,
        status: 'reviewed', confidence: 'high', readiness: 'needs_adaptation',
        summary: '包含多个剧本创作 Skill 的套件。', basis: ['人工复核'],
      },
    }),
    base({
      source_id: 'src:plugin',
      name: '创意 Plugin',
      relative_path: 'incoming/creative-plugin.zip',
      origin_kind: 'chat',
      origin_basis: 'collection_path',
      capabilities: [capability('e', 'plugin', 'creative-plugin', '工具类')],
      classification: {
        primary_type: 'plugin', functional_type: 'plugin', primary_scenario: '工具类',
        display_label: 'Plugin 包', component_types: ['plugin'], is_composite: false,
        status: 'classified', confidence: 'high', readiness: 'not_assessed',
        summary: '带 Plugin manifest 的插件包。', basis: ['检测到 Plugin manifest'],
      },
    }),
    base({
      source_id: 'src:anchor',
      name: 'shared-workflow',
      relative_path: 'shared/shared-workflow',
      kind: 'repository',
      origin_kind: 'local',
      origin_basis: 'manual',
      size_bytes: 4096,
      scenarios: ['工具类'],
      capabilities: [capability('f', 'workflow', '共享工作流', '工具类')],
      host_links: [
        { host_id: 'codex', link_path: '~/.codex/skills/qunce', target_path: '/fixture/qunce' },
      ],
      installation_status: 'linked',
      classification: {
        primary_type: 'workflow', functional_type: 'workflow', primary_scenario: '工具类',
        display_label: '多 Agent 工作流项目', component_types: ['workflow'], is_composite: false,
        status: 'reviewed', confidence: 'high', readiness: 'not_assessed',
        summary: '宿主正在引用的工作流项目。', basis: ['宿主软链接命中'],
      },
      proposed_bucket: '原位保留_活动锚点',
      move_safety: 'keep_anchor',
    }),
    base({
      source_id: 'src:case',
      name: '示例案例包',
      relative_path: 'incoming/example-case.zip',
      scenarios: [],
      capabilities: [capability('g', 'case_study', '完整对话案例', '广告与爆款分析')],
      classification: {
        primary_type: 'case_archive', functional_type: 'other_material', primary_scenario: null,
        display_label: '案例 / 对话归档', component_types: ['case_archive'], is_composite: false,
        status: 'reviewed', confidence: 'high', readiness: 'reference_only',
        summary: '完整制作对话，仅作为其他资料保存。', basis: ['人工复核'],
      },
    }),
  ].slice(0, sourceCount)
  return {
    schema_version: 3,
    generated_at: '2026-08-09T20:00:00Z',
    mode: 'collection-observe',
    source_root: '/private/snapshots/collection-source',
    summary: {
      source_count: items.length,
      capability_count: items.flatMap((item) => item.capabilities).length,
      unique_capability_count: items.flatMap((item) => item.capabilities).length,
      repository_count: items.filter((item) => item.kind === 'repository').length,
      document_count: 0,
      archive_count: 0,
      classified_count: items.length,
      unclassified_count: 0,
      project_count: items.length,
      composite_count: 0,
      anchored_source_count: items.filter((item) => item.host_links.length > 0).length,
      duplicate_group_count: 0,
      container_count: 1,
      scan_error_count: 0,
    },
    items,
    scan_errors: [],
  }
}

function candidateCatalog() {
  return {
    schema_version: 3,
    source_dir: '/private/snapshots/collection-source',
    groups: [
      {
        name: '电影感生图与视觉资产',
        desc: '把想法或参考图编译成电影质感的生图提示词',
        competing: true,
        order: 3,
        count: 1,
      },
    ],
    items: [
      {
        uid: 'candidate-cinema',
        name: 'cinema-image',
        version: '1.2.0',
        zh_name: '电影生图 Skill',
        zh_sum: '把参考图整理成电影感视觉资产。',
        desc: 'cinematic fixture',
        desc_full: 'cinematic fixture full description',
        group: '电影感生图与视觉资产',
        src: 'incoming/cinema.skill',
        copy_srcs: ['incoming/cinema.skill', 'archive/cinema.skill'],
        tags: ['中文包'],
        platform: ['GPT Image'],
        inputs: ['参考图'],
        outputs: ['视觉资产'],
        shape: { 参考资料: 2, total: 2 },
        copies: 2,
        chars: 1200,
        installed: { hosts: [], as_name: '' },
      },
    ],
  }
}

function projectSkillSnapshot(overrides = {}) {
  const observedSkill = {
    logical_skill_id: 'project-skill:fixture-observed',
    display_name: 'reviewed-content-pipeline',
    localization: {
      status: 'reviewed',
      coverage_complete: true,
      asset_id: 'skill:reviewed-content-pipeline',
      zh_name: '三阶段内容生产编排',
      summary: '编排从剧本、导演分镜到可执行视频提示词的三阶段生产流程。',
      use_cases: ['启动并审核三阶段内容生产流程。'],
      not_for: ['跳过阶段间人工确认。'],
      examples: ['从这版剧本启动流程，等我批准分镜后再继续。'],
      translated_from_projection_sha256_v1: '1'.repeat(64),
    },
    observations: [
      {
        observation_id: 'observation:fixture-observed',
        observed_at: '2026-08-11T12:00:00Z',
        source_kind: 'entity',
        entry: {
          relative_path: '.agents/skills',
          kind: 'host_entry',
          host_hint: 'codex_compatible',
          binding_semantics: 'project_binding_candidate',
        },
        manifest_relative_path: '.agents/skills/reviewed-content-pipeline/SKILL.md',
        file_type: 'regular_file',
        link_status: 'not_applicable',
        frontmatter_projection: {
          name: 'reviewed-content-pipeline',
          description: '三阶段内容制作流程。',
          version: null,
          author: null,
          license: null,
          agent_created: null,
        },
        evidence: {
          file_discovery: { status: 'observed' },
          project_binding: { status: 'observed_binding' },
          host_availability: { status: 'unverified' },
          invocation_eligibility: { status: 'not_observed' },
          actual_use: { status: 'not_connected' },
        },
      },
    ],
  }
  const payload = {
    schema_version: 1,
    observation_boundary_ref: 'project-skill-observation-boundary-v1',
    generation_id: 'a1b2c3d4e5f60001',
    generated_at: '2026-08-11T12:00:00Z',
    scan_status: 'complete',
    scan_scope: {
      trigger: 'manual',
      execution: 'foreground',
      network: 'disabled',
      limits: {},
    },
    candidates: [
      {
        candidate_id: 'candidate:fixture',
        relative_path: 'candidate-project',
        status: 'unclassified',
        observed_at: '2026-08-11T12:00:00Z',
      },
    ],
    projects: [
      {
        project_id: 'project-alpha',
        relative_path: 'example-project-alpha',
        display_name: '示例项目 Alpha',
        classification: 'project',
        scan_status: 'complete',
        entries: [
          {
            entry: {
              relative_path: '.agents/skills',
              kind: 'host_entry',
              host_hint: 'codex_compatible',
              binding_semantics: 'project_binding_candidate',
            },
            status: 'observed',
            observed_entry_count: 1,
            issue_refs: [],
          },
        ],
        logical_skills: [observedSkill],
      },
      {
        project_id: 'project-beta',
        relative_path: 'example-project-beta',
        display_name: '示例项目 Beta',
        classification: 'project',
        scan_status: 'complete',
        entries: [],
        logical_skills: [],
      },
      {
        project_id: 'container-codex',
        relative_path: 'Codex',
        display_name: 'Codex',
        classification: 'container',
        scan_status: 'not_scanned',
        entries: [],
        logical_skills: [],
      },
    ],
    human_associations: {
      registry_id: 'project-skill-associations-v1',
      confirmed_on: '2026-08-11',
      items: [
        {
          association_id: 'human-association:project-beta:skill:apple-design',
          project_id: 'project-beta',
          asset_id: 'skill:apple-design',
          relationship: 'human_association',
          reason: '人工确认该 Skill 与此前端 UI 项目的设计需求相关。',
          source: 'gate_a_user_confirmation',
        },
      ],
    },
    changes: {
      status: 'compared',
      compared_to_generation_id: 'a1b2c3d4e5f60000',
      fingerprint_basis: 'projection_sha256_v1',
      added: [],
      changed: [],
      removed: [],
    },
    issues: [],
  }
  return { ...payload, ...overrides }
}

function projectSkillView(snapshotPayload = projectSkillSnapshot(), overrides = {}) {
  return {
    snapshot: snapshotPayload,
    last_attempt: {
      schema_version: 1,
      observation_boundary_ref: 'project-skill-observation-boundary-v1',
      attempted_at: snapshotPayload.generated_at,
      attempt_generation_id: snapshotPayload.generation_id,
      scan_status: snapshotPayload.scan_status,
      promoted: snapshotPayload.scan_status === 'complete',
      matches_complete_snapshot: snapshotPayload.scan_status === 'complete',
      complete_snapshot_generation_id: snapshotPayload.generation_id,
      issues: [],
    },
    report: {
      available: true,
      generation_id: snapshotPayload.generation_id,
      content: `# 项目 Skill 总览\n\n快照 ${snapshotPayload.generation_id}\n`,
    },
    integrity: { degraded: false, errors: [] },
    ...overrides,
  }
}

function projectWorkbenchFetch({
  projectGet = response(projectSkillView()),
  projectRefresh = response(projectSkillSnapshot({
    generation_id: 'a1b2c3d4e5f60002',
    generated_at: '2026-08-11T13:00:00Z',
  })),
} = {}) {
  return vi.fn(async (url, options = {}) => {
    if (url === '/api/health') {
      return response({
        ...health,
        project_skill_snapshot: {
          available: true,
          refresh_state: 'idle',
          integrity: { degraded: false, errors: [] },
        },
      })
    }
    if (url === '/api/snapshot') return response(snapshot(3, 'abcdefabcdef0001'))
    if (url === '/api/collections/check') {
      return response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collectionSnapshot(),
      })
    }
    if (url === '/candidates/data.json') return response(candidateCatalog())
    if (url === '/api/project-skills') {
      return typeof projectGet === 'function' ? projectGet(url, options) : projectGet
    }
    if (url === '/api/project-skills/refresh') {
      return typeof projectRefresh === 'function' ? projectRefresh(url, options) : projectRefresh
    }
    throw new Error(`unexpected ${url}`)
  })
}

async function renderProjectWorkbench(fetchMock = projectWorkbenchFetch()) {
  vi.stubGlobal('fetch', fetchMock)
  vi.stubGlobal('scrollTo', vi.fn())
  const container = document.createElement('div')
  document.body.append(container)
  const root = createRoot(container)
  await act(async () => {
    root.render(React.createElement(App))
    await flush()
    await flush()
  })
  return { root, fetchMock }
}

function projectNavButton(selector = '.sidebar-nav') {
  return [...document.querySelectorAll(`${selector} button`)].find((button) =>
    button.textContent.includes('项目 Skill'),
  )
}

async function openProjectSkills(selector = '.sidebar-nav') {
  const button = projectNavButton(selector)
  expect(button).toBeTruthy()
  await act(async () => {
    button.dispatchEvent(new MouseEvent('click', { bubbles: true }))
    await flush()
    await flush()
  })
}

async function flush() {
  await Promise.resolve()
  await new Promise((resolve) => setTimeout(resolve, 0))
}

afterEach(() => {
  vi.restoreAllMocks()
  vi.unstubAllGlobals()
  localStorage.clear()
  document.body.replaceChildren()
})

describe('App refresh state', () => {
  it('keeps a saved snapshot when the follow-up health receipt fails', async () => {
    const initial = snapshot(1, '1111111111111111')
    const refreshed = snapshot(9, '9999999999999999')
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collectionSnapshot(),
      }))
      .mockResolvedValueOnce(response(candidateCatalog()))
      .mockResolvedValueOnce(response(refreshed))
      .mockRejectedValueOnce(new Error('health unavailable'))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })

    const refresh = [...document.querySelectorAll('button')].find((button) =>
      button.textContent.includes('刷新观察'),
    )
    expect(refresh).toBeTruthy()
    await act(async () => {
      refresh.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(document.querySelector('.metric-card strong')?.textContent).toBe('9')
    expect(document.body.textContent).toContain('只读观察已完成并保存；服务状态回执暂未更新。')
    const receiptNotice = document.querySelector('.notice-info[role="status"]')
    expect(receiptNotice).toBeTruthy()
    expect(receiptNotice.textContent).toContain('只读观察已完成并保存')
    expect(document.querySelector('.notice-error[role="alert"]')).toBeNull()
    expect(refresh.disabled).toBe(false)
    expect(fetchMock).toHaveBeenCalledTimes(6)

    await act(async () => root.unmount())
  })

  it('uses candidate-specific chrome without unrelated scan controls', async () => {
    const initial = snapshot(3, '3333333333333333')
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collectionSnapshot(),
      }))
      .mockResolvedValueOnce(response(candidateCatalog()))
      .mockResolvedValueOnce(response(candidateCatalog()))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })

    const candidateNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('备选 skill 库'),
    )
    expect(candidateNav).toBeTruthy()
    await act(async () => {
      candidateNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(document.querySelector('.candidates-view')).toBeTruthy()
    expect(document.querySelector('iframe[title="备选 skill 库"]')).toBeNull()
    expect(document.querySelector('.candidate-topbar-context')?.textContent).toContain('收藏夹决策区')
    expect(document.querySelector('.global-search')).toBeNull()
    expect(document.querySelector('.candidates-guide')).toBeNull()
    expect(document.querySelector('.candidates-intro .section-kicker')?.textContent).toBe('留 / 砍 / 并')
    expect(document.querySelector('.candidates-intro h2')?.textContent).toBe('1 个备选 Skill')
    expect(document.querySelector('.candidates-intro')?.textContent).toContain('把同类技能收敛成清晰的保留方案')
    expect(document.querySelector('.candidates-group-badge')?.textContent.trim()).toBe('单项 1 个')
    expect(document.body.textContent).not.toContain('直接竞争 1 个')
    expect(document.body.textContent).not.toContain('互补 1 个')
    const candidateToolbar = document.querySelector('.candidates-toolbar')
    const candidateRefresh = candidateToolbar?.querySelector('button[aria-label="刷新候选数据"]')
    expect(candidateRefresh).toBeTruthy()
    const toolbarButtons = [...candidateToolbar.querySelectorAll('button')]
    expect(toolbarButtons.indexOf(candidateRefresh)).toBeLessThan(
      toolbarButtons.findIndex((button) => button.textContent.includes('导入')),
    )
    expect([...document.querySelectorAll('button')].some((button) => button.textContent.includes('刷新观察'))).toBe(false)
    expect(document.querySelector('.status-strip')).toBeNull()

    await act(async () => root.unmount())
  })

  it('aggregates same-source candidate quality, preserves host findings, and deep-links precisely', async () => {
    const hostAsset = {
      asset_id: 'cli:codex',
      type: 'cli',
      source_name: 'codex',
      localization: { zh_name: 'Codex CLI', summary: 'Codex 命令行入口', status: 'stale' },
      curation: { category: '开发与工程', subcategory: '命令行工具' },
      host_bindings: [],
    }
    const initial = snapshot(1, 'health-aggregate', [hostAsset])
    initial.health_findings = [{
      code: 'localization_stale',
      severity: 'warning',
      asset_id: 'cli:codex',
      detail: '原文指纹已经变化，需要重新核对中文说明。',
    }]
    initial.summary.health_finding_count = 1

    const collections = collectionSnapshot()
    const candidates = candidateCatalog()
    candidates.generated_at = '2026-08-11T05:33:52-07:00'
    Object.assign(candidates.items[0], {
      zh_state: 'stale',
      desc_flags: ['truncated'],
      tags: ['中文包', '简介有问题'],
    })
    candidates.items.push({
      ...candidates.items[0],
      uid: 'candidate-broken',
      name: 'broken-cinema-image',
      zh_name: '简介异常候选',
      src: 'incoming/broken.skill',
      copy_srcs: ['incoming/broken.skill'],
      zh_state: 'ai_draft',
      desc_flags: ['desc_broken'],
      tags: ['简介有问题'],
    })
    candidates.groups[0].count = 2
    candidates.facets = {
      platform: [{ value: 'GPT Image', count: 2 }],
      inputs: [{ value: '参考图', count: 2 }],
      outputs: [{ value: '视觉资产', count: 2 }],
      zh_state: [
        { value: 'stale', count: 1 },
        { value: 'ai_draft', count: 1 },
      ],
      has: [
        { value: '中文包', count: 1 },
        { value: '简介有问题', count: 2 },
      ],
    }

    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') {
        return response({
          status: 'unchanged',
          reason: 'input_signature_match',
          stable: true,
          snapshot: collections,
        })
      }
      if (url === '/candidates/data.json') return response(candidates)
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
      await flush()
    })

    const healthNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('系统体检'),
    )
    await act(async () => {
      healthNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    expect(document.querySelector('.health-intro h2')?.textContent).toBe('4 条体检记录')
    expect(document.querySelector('.health-scope-summary')?.textContent).toContain('宿主 1 条')
    expect(document.querySelector('.health-scope-summary')?.textContent).toContain('候选 3 条')
    expect(document.body.textContent).toContain('候选 · 中文说明待复核')
    expect(document.body.textContent).toContain('候选 · 简介解析异常')
    expect(document.body.textContent).toContain('候选 · 卡片简介已截短')

    const findingGroup = (title) => [...document.querySelectorAll('.finding-group')].find((group) =>
      group.querySelector('summary strong')?.textContent === title,
    )
    const hostGroup = findingGroup('中文说明待复核')
    await act(async () => {
      hostGroup.querySelector('.finding-rows button').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    expect(document.querySelector('aside[aria-label="Codex CLI详情"]')).toBeTruthy()
    await act(async () => {
      document.querySelector('aside[aria-label="Codex CLI详情"] button[aria-label="关闭详情"]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const candidateGroup = findingGroup('候选 · 中文说明待复核')
    await act(async () => {
      candidateGroup.querySelector('.finding-rows button').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(document.querySelector('.candidates-view')).toBeTruthy()
    expect(document.querySelector('aside[aria-label="电影生图 Skill详情"]')).toBeTruthy()
    const cardTags = [...document.querySelectorAll('.candidate-card-tags')].map((node) => node.textContent)
    expect(cardTags.some((text) => text.includes('中文说明待复核'))).toBe(true)
    expect(cardTags.some((text) => text.includes('简介已截短'))).toBe(true)
    expect(cardTags.some((text) => text.includes('简介解析异常'))).toBe(true)
    expect(cardTags.join(' ')).not.toContain('中文已过期')
    expect(cardTags.join(' ')).not.toContain('简介有问题')

    await act(async () => {
      document.querySelector('aside[aria-label="电影生图 Skill详情"] button[aria-label="关闭详情"]')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      const filterButton = [...document.querySelectorAll('.candidates-toolbar button')].find((button) =>
        button.textContent.includes('筛选'),
      )
      filterButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    const filterPanel = document.querySelector('.candidates-filter-panel')
    expect(filterPanel.textContent).not.toContain('简介有问题')
    const parsingFilter = [...filterPanel.querySelectorAll('button')].find((button) =>
      button.textContent.includes('简介解析异常'),
    )
    const truncatedFilter = [...filterPanel.querySelectorAll('button')].find((button) =>
      button.textContent.includes('简介已截短'),
    )
    expect(parsingFilter.textContent).toContain('1')
    expect(truncatedFilter.textContent).toContain('1')

    await act(async () => {
      parsingFilter.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    expect(document.querySelectorAll('.candidate-card')).toHaveLength(1)
    expect(document.querySelector('.candidate-card')?.textContent).toContain('简介异常候选')
    await act(async () => {
      parsingFilter.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      truncatedFilter.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    expect(document.querySelectorAll('.candidate-card')).toHaveLength(1)
    expect(document.querySelector('.candidate-card')?.textContent).toContain('电影生图 Skill')

    for (const url of ['/api/refresh', '/api/candidates/refresh', '/api/collections/refresh']) {
      expect(fetchMock.mock.calls.some(([calledUrl]) => calledUrl === url)).toBe(false)
    }

    await act(async () => root.unmount())
  })

  it('fails closed when candidate and collection sources differ', async () => {
    const initial = snapshot(0, 'candidate-source-mismatch')
    const collections = collectionSnapshot()
    const candidates = candidateCatalog()
    candidates.source_dir = '/private/snapshots/other-collection-source'
    Object.assign(candidates.items[0], { zh_state: 'stale', desc_flags: ['truncated'] })

    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') {
        return response({ status: 'unchanged', stable: true, snapshot: collections })
      }
      if (url === '/candidates/data.json') return response(candidates)
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
      await flush()
    })
    const healthNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('系统体检'),
    )
    await act(async () => {
      healthNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    expect(document.querySelector('.health-intro h2')?.textContent).toBe('0 条体检记录')
    expect(document.querySelector('.health-scope-summary')?.textContent).toContain('候选来源不一致')
    expect(document.body.textContent).toContain('已按边界排除')
    expect(document.body.textContent).not.toContain('候选 · 中文说明待复核')

    await act(async () => root.unmount())
  })

  it('treats a same-source malformed candidate catalog as unavailable', async () => {
    const initial = snapshot(0, 'candidate-catalog-malformed')
    const collections = collectionSnapshot()
    const candidates = { ...candidateCatalog(), items: 'not-an-array' }

    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') {
        return response({ status: 'unchanged', stable: true, snapshot: collections })
      }
      if (url === '/candidates/data.json') return response(candidates)
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
      await flush()
    })
    const healthNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('系统体检'),
    )
    await act(async () => {
      healthNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    expect(document.querySelector('.health-scope-summary')?.textContent).toContain('候选范围未载入')
    expect(document.querySelector('.health-scope-summary')?.textContent).not.toContain('候选 0 条')

    await act(async () => root.unmount())
  })

  it('keeps malformed rows and duplicate UIDs out of candidate cards and health deep-links', async () => {
    const initial = snapshot(0, 'candidate-row-malformed')
    const collections = collectionSnapshot()
    const baseItem = candidateCatalog().items[0]
    const candidates = candidateCatalog()
    candidates.items = [
      null,
      {
        ...baseItem,
        uid: 'safe-stale',
        name: 'safe-stale-name',
        zh_name: { bad: true },
        zh_state: 'stale',
        desc_flags: [],
      },
      { ...baseItem, uid: 'duplicate', name: 'duplicate-first', zh_state: 'stale' },
      { ...baseItem, uid: 'duplicate', name: 'duplicate-second', zh_state: 'stale' },
    ]
    const consoleError = vi.spyOn(console, 'error').mockImplementation(() => {})

    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') {
        return response({ status: 'unchanged', stable: true, snapshot: collections })
      }
      if (url === '/candidates/data.json') return response(candidates)
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
      await flush()
    })
    const healthNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('系统体检'),
    )
    await act(async () => {
      healthNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const staleGroup = [...document.querySelectorAll('.finding-group')].find((group) =>
      group.querySelector('summary strong')?.textContent === '候选 · 中文说明待复核',
    )
    expect(staleGroup.querySelectorAll('.finding-rows button')).toHaveLength(1)
    expect(staleGroup.textContent).toContain('safe-stale-name')
    expect(staleGroup.textContent).not.toContain('duplicate-first')
    expect(staleGroup.textContent).not.toContain('duplicate-second')

    await act(async () => {
      staleGroup.querySelector('.finding-rows button').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(document.querySelector('.candidates-intro h2')?.textContent).toBe('1 个备选 Skill')
    expect(document.querySelectorAll('.candidate-card')).toHaveLength(1)
    expect(document.querySelector('aside[aria-label="safe-stale-name详情"]')).toBeTruthy()
    expect(document.body.textContent).not.toContain('duplicate-first')
    expect(document.body.textContent).not.toContain('duplicate-second')
    expect(consoleError).not.toHaveBeenCalled()

    await act(async () => root.unmount())
  })

  it('keeps snapshot stale findings visible after a browser-only Chinese review', async () => {
    const initial = snapshot(0, 'candidate-local-review')
    const collections = collectionSnapshot()
    const candidates = candidateCatalog()
    Object.assign(candidates.items[0], { zh_state: 'stale', desc_flags: [] })
    localStorage.setItem('skill-workbench:decisions:v1', JSON.stringify({
      schema_version: 1,
      decisions: {},
      overrides: { 'candidate-cinema': { zh_reviewed: true } },
    }))

    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') {
        return response({ status: 'unchanged', stable: true, snapshot: collections })
      }
      if (url === '/candidates/data.json') return response(candidates)
      throw new Error(`unexpected request: ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
      await flush()
    })
    const healthNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('系统体检'),
    )
    await act(async () => {
      healthNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    const candidateGroup = [...document.querySelectorAll('.finding-group')].find((group) =>
      group.querySelector('summary strong')?.textContent === '候选 · 中文说明待复核',
    )
    expect(candidateGroup).toBeTruthy()
    await act(async () => {
      candidateGroup.querySelector('.finding-rows button').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    const drawer = document.querySelector('aside[aria-label="电影生图 Skill详情"]')
    expect(drawer.textContent).toContain('候选快照中的原包内容已变更，中文说明待复核')
    expect(drawer.textContent).toContain('当前浏览器已标记确认，但尚未回写候选快照')

    await act(async () => root.unmount())
  })

  it('refreshes candidate data in place and preserves browser decisions', async () => {
    const initial = snapshot(3, '4444444444444444')
    const refreshed = candidateCatalog()
    refreshed.items.push({
      ...refreshed.items[0],
      uid: 'candidate-new',
      name: 'new-candidate',
      zh_name: '新增候选 Skill',
      src: 'incoming/new-candidate.skill',
      copy_srcs: ['incoming/new-candidate.skill'],
    })
    refreshed.groups[0].count = 2
    localStorage.setItem('skill-workbench:decisions:v1', JSON.stringify({
      schema_version: 1,
      decisions: {
        'candidate-cinema': { verdict: 'keep', decided_at: '2026-08-10T12:00:00Z' },
      },
      overrides: {},
    }))
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collectionSnapshot(),
      }))
      .mockResolvedValueOnce(response(candidateCatalog()))
      .mockResolvedValueOnce(response(candidateCatalog()))
      .mockResolvedValueOnce(response(refreshed))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })
    const candidateNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('备选 skill 库'),
    )
    await act(async () => {
      candidateNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    const refreshCandidate = document.querySelector('button[aria-label="刷新候选数据"]')
    expect(refreshCandidate).toBeTruthy()
    await act(async () => {
      refreshCandidate.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(document.body.textContent).toContain('新增候选 Skill')
    expect(document.body.textContent).toContain('候选数据已刷新，共 2 个 Skill')
    expect(document.querySelector('.candidates-intro h2')?.textContent).toBe('2 个备选 Skill')
    expect(document.querySelector('.candidates-group-badge')?.textContent.trim()).toBe('备选 2 个')
    expect(JSON.parse(localStorage.getItem('skill-workbench:decisions:v1')).decisions['candidate-cinema'].verdict).toBe('keep')
    expect(fetchMock).toHaveBeenCalledWith('/api/candidates/refresh', expect.objectContaining({
      method: 'POST',
      body: '{}',
      headers: expect.objectContaining({ 'X-AI-Toolbox-CSRF': 'fixture-csrf' }),
    }))

    await act(async () => root.unmount())
  })

  it('adds an independent all-collections view and preserves the candidate entry', async () => {
    const initial = snapshot(3, '5555555555555555')
    const collections = collectionSnapshot()
    const candidates = candidateCatalog()
    candidates.items.push({
      ...candidates.items[0],
      uid: 'candidate-deleted-source',
      name: 'deleted-source-skill',
      zh_name: '已删除源候选 Skill',
      src: '已删除/deleted.skill',
      copy_srcs: ['已删除/deleted.skill'],
    })
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collections,
      }))
      .mockResolvedValueOnce(response(candidates))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })

    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('全部收藏'),
    )
    const candidateNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('备选 skill 库'),
    )
    expect(collectionsNav).toBeTruthy()
    expect(candidateNav).toBeTruthy()
    expect([...document.querySelectorAll('.sidebar-nav button')].map((button) => button.textContent.trim())).toEqual([
      '总览',
      'Skill 库',
      '项目 Skill',
      'Plugin 与工具',
      '备选 skill 库',
      '全部收藏',
      '系统体检',
    ])

    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(document.querySelector('.collections-view')).toBeTruthy()
    expect(document.querySelectorAll('.collection-source-card')).toHaveLength(6)
    expect(document.body.textContent).toContain('所有收藏，一处看全')
    expect(document.body.textContent).toContain('按功能类型与使用场景整理')
    expect(document.body.textContent).toContain('1 个收藏源正在被宿主入口引用')
    expect(document.body.textContent).toContain('原备选库 1 张已保留')
    expect(document.body.textContent).not.toContain('已删除源候选 Skill')
    expect(fetchMock).toHaveBeenCalledWith('/api/collections/check', expect.objectContaining({
      method: 'POST',
      body: '{}',
      headers: expect.objectContaining({ 'X-AI-Toolbox-CSRF': 'fixture-csrf' }),
    }))
    expect(fetchMock).toHaveBeenCalledWith('/candidates/data.json', { cache: 'no-store' })

    const typeFilter = document.querySelector('select[aria-label="对象类型筛选"]')
    expect(typeFilter).toBeTruthy()
    expect([...typeFilter.options].map((option) => option.textContent)).toEqual([
      '全部功能类型', 'Skill', '项目', '复合 Skill 包', 'Plugin', '工作流', '其他资料',
    ])
    expect([...document.querySelectorAll('.collection-type-heading h3')].map((heading) => heading.textContent)).toEqual([
      'Skill', '项目', '复合 Skill 包', 'Plugin', '工作流', '其他资料',
    ])
    const singletonCollectionGroup = document.querySelector('[data-functional-type="skill"] [data-scenario="电影感生图与视觉资产"]')
    expect(singletonCollectionGroup).toBeTruthy()
    expect(singletonCollectionGroup.querySelector('.collection-scenario-badge')?.textContent.trim()).toBe('单项 1 个')
    expect(singletonCollectionGroup.querySelector('.collection-scenario-badge')?.classList.contains('relation-single')).toBe(true)
    expect(document.querySelector('[data-functional-type="other_material"] .collection-scenario-section')).toBeNull()
    await act(async () => {
      typeFilter.value = 'workflow'
      typeFilter.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })
    expect(document.querySelectorAll('.collection-source-card')).toHaveLength(1)
    expect(document.body.textContent).toContain('多 Agent 工作流项目')
    expect(document.body.textContent).not.toContain('sample-repo')

    await act(async () => {
      typeFilter.value = 'other_material'
      typeFilter.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })
    const otherSection = document.querySelector('[data-functional-type="other_material"]')
    expect(otherSection.querySelectorAll('.collection-source-card')).toHaveLength(1)
    expect(otherSection.querySelector('.collection-scenario-section')).toBeNull()
    expect(document.querySelector('select[aria-label="收藏场景筛选"]').disabled).toBe(true)
    expect(document.querySelector('select[aria-label="收藏场景筛选"] option:checked').textContent).toBe('其他资料不分场景')

    await act(async () => {
      candidateNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })
    expect(document.querySelector('.candidates-view')).toBeTruthy()
    expect(document.querySelector('iframe[title="备选 skill 库"]')).toBeNull()

    await act(async () => root.unmount())
  })

  it('shows safe collection skips as expandable non-error details with the affected source', async () => {
    const initial = snapshot(3, '5656565656565656')
    const collections = collectionSnapshot()
    collections.scan_errors = [{
      code: 'symlink_skipped',
      path: 'archive/sample-repo/.opencode/skills',
    }]
    collections.summary.scan_error_count = 1
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collections,
      }))
      .mockResolvedValueOnce(response(candidateCatalog()))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('全部收藏'),
    )
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    const disclosure = document.querySelector('.collection-scan-notice.safe-only')
    expect(disclosure).toBeTruthy()
    expect(disclosure.open).toBe(false)
    expect(disclosure.querySelector('summary').textContent).toContain('发现 1 个安全跳过项')
    expect(disclosure.querySelector('.collection-scan-summary-copy').getAttribute('role')).toBe('status')
    expect(disclosure.textContent).not.toContain('未完整读取')
    expect(disclosure.textContent).toContain('符号链接未跟随')
    expect(disclosure.textContent).toContain('symlink_skipped')
    expect(disclosure.textContent).toContain(
      '~/Example/Collection/archive/sample-repo/.opencode/skills',
    )
    expect(disclosure.querySelector('button[aria-label="复制路径"]')).toBeTruthy()

    await act(async () => {
      disclosure.querySelector('summary').click()
      await flush()
    })
    expect(disclosure.open).toBe(true)
    await act(async () => {
      disclosure.querySelector('summary').click()
      await flush()
    })
    expect(disclosure.open).toBe(false)

    const relatedButton = [...disclosure.querySelectorAll('button')].find((button) =>
      button.textContent.includes('查看对应收藏'),
    )
    expect(relatedButton).toBeTruthy()
    await act(async () => {
      relatedButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    expect(document.querySelector('aside[aria-label="sample-repo详情"]')).toBeTruthy()

    await act(async () => root.unmount())
  })

  it('keeps archive guard failures reviewable and counts symlink skips separately', async () => {
    const initial = snapshot(3, '5656565656565657')
    const collections = collectionSnapshot()
    const archiveErrors = [
      { code: 'archive_compression_ratio', path: 'archive/sample-repo/ratio.zip' },
      { code: 'archive_duplicate_path', path: 'archive/sample-repo/duplicate.zip' },
      { code: 'archive_encrypted_entry', path: 'archive/sample-repo/encrypted.zip' },
      { code: 'archive_entry_limit', path: 'archive/sample-repo/many-files.zip' },
      { code: 'archive_member_too_large', path: 'archive/sample-repo/large-member.zip' },
      { code: 'archive_path_invalid', path: 'archive/sample-repo/unsafe-path.zip' },
      { code: 'archive_total_size_limit', path: 'archive/sample-repo/large-expanded.zip' },
      { code: 'archive_unreadable', path: 'archive/sample-repo/broken.zip' },
    ]
    const failedAttempt = {
      generated_at: '2026-08-11T10:11:00Z',
      summary: { scan_error_count: archiveErrors.length + 1 },
      scan_errors: [
        ...archiveErrors,
        {
          code: 'symlink_skipped',
          path: 'archive/sample-repo/.opencode/skills',
        },
      ],
    }
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        error: 'collection_scan_incomplete',
        message: '本次收藏索引不完整，上一份有效数据未被覆盖',
        attempt: failedAttempt,
      }, 422)
      if (url === '/api/collections') return response(collections)
      if (url === '/candidates/data.json') return response(candidateCatalog())
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('全部收藏'),
    )
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush(); await flush()
    })

    const disclosure = document.querySelector('.collection-scan-notice.needs-review')
    expect(disclosure).toBeTruthy()
    expect(disclosure.querySelector('summary').textContent).toContain(
      '本次检查有 8 个对象未完整读取，另记录 1 个安全跳过项',
    )
    expect(disclosure.querySelector('.collection-scan-summary-copy').getAttribute('role')).toBe('alert')
    expect(disclosure.querySelectorAll('.collection-scan-row.needs-review')).toHaveLength(8)
    expect(disclosure.querySelectorAll('.collection-scan-row.is-safe')).toHaveLength(1)

    archiveErrors.forEach(({ code }) => {
      const row = [...disclosure.querySelectorAll('.collection-scan-row')].find((candidate) =>
        candidate.textContent.includes(code),
      )
      expect(row).toBeTruthy()
      expect(row.classList.contains('needs-review')).toBe(true)
      expect(row.textContent).toContain('需要复核')
    })
    const safeRow = disclosure.querySelector('.collection-scan-row.is-safe')
    expect(safeRow.textContent).toContain('symlink_skipped')
    expect(safeRow.textContent).toContain('安全跳过')
    const adviceSnippets = [
      '不要仅为消除提示调高安全上限',
      '移除规范化后重复的成员路径',
      'Toolbox 不接收解密密码',
      '不要仅为消除提示调高条目上限',
      '确认超大成员是否需要索引',
      '不含绝对路径、上级跳转或控制字符',
      '不要调高展开体积上限',
      '确为受支持的 ZIP 且当前可读',
    ]
    adviceSnippets.forEach((advice) => expect(disclosure.textContent).toContain(advice))

    await act(async () => root.unmount())
  })

  it('keeps failed collection attempt details visible while falling back, then clears them on success', async () => {
    const initial = snapshot(3, '5757575757575757')
    const collections = collectionSnapshot()
    const failedAttempt = {
      generated_at: '2026-08-11T10:10:00Z',
      summary: { scan_error_count: 1 },
      scan_errors: [{
        code: 'manifest_unreadable',
        path: 'archive/sample-repo/plugin.json',
        detail: 'fixture permission denied',
      }],
    }
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        error: 'collection_scan_incomplete',
        message: '本次收藏索引不完整，上一份有效数据未被覆盖',
        attempt: failedAttempt,
      }, 422)
      if (url === '/api/collections') return response(collections)
      if (url === '/api/collections/refresh') return response(collections)
      if (url === '/candidates/data.json') return response(candidateCatalog())
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('全部收藏'),
    )
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush(); await flush()
    })

    expect(document.querySelectorAll('.collection-source-card')).toHaveLength(6)
    const disclosure = document.querySelector('.collection-scan-notice.needs-review')
    expect(disclosure).toBeTruthy()
    expect(disclosure.open).toBe(false)
    expect(disclosure.querySelector('summary').textContent).toContain('本次检查有 1 个对象未完整读取')
    expect(disclosure.querySelector('.collection-scan-summary-copy').getAttribute('role')).toBe('alert')
    expect(disclosure.textContent).toContain('入口清单无法读取')
    expect(disclosure.textContent).toContain('manifest_unreadable')
    expect(disclosure.textContent).toContain('fixture permission denied')
    expect(disclosure.textContent).toContain(
      '~/Example/Collection/archive/sample-repo/plugin.json',
    )

    const manualRefresh = [...document.querySelectorAll('button')].find((button) =>
      button.textContent.includes('手动刷新收藏索引'),
    )
    await act(async () => {
      manualRefresh.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(document.querySelector('.collection-scan-notice.needs-review')).toBeNull()

    await act(async () => root.unmount())
  })

  it('keeps card provenance light and puts version, status, path, tooltips, and relations in details', async () => {
    const initial = snapshot(3, '6666666666666666')
    const collections = collectionSnapshot()
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collections,
      }))
      .mockResolvedValueOnce(response(candidateCatalog()))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('全部收藏'),
    )
    expect(collectionsNav).toBeTruthy()
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    const cards = [...document.querySelectorAll('.collection-source-card')]
    const repoCard = cards.find((card) => card.textContent.includes('sample-repo'))
    expect(repoCard).toBeTruthy()
    expect(repoCard.textContent).toContain('GitHub')
    expect(repoCard.textContent).not.toContain('archive/sample-repo')
    expect(repoCard.querySelector('.collection-meta-badge')?.dataset.tooltip).toContain('根据 Git 仓库来源识别')
    expect(repoCard.querySelector('.collection-meta-badge')?.classList.contains('tone-violet')).toBe(true)

    await act(async () => {
      repoCard.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    let drawer = document.querySelector('aside[aria-label="sample-repo详情"]')
    expect(drawer).toBeTruthy()
    expect(drawer.textContent).toContain('v2.0.0')
    expect(drawer.textContent).toContain('仅收藏')
    expect(drawer.textContent).toContain('较新版本')
    expect(drawer.textContent).toContain('路径')
    expect(drawer.textContent).toContain('archive/sample-repo')
    expect(drawer.textContent).toContain('升级自')
    expect(drawer.textContent).toContain('archive/sample-repo-v1')
    expect(drawer.querySelectorAll('.collection-meta-badge[data-tooltip]').length).toBeGreaterThanOrEqual(4)

    await act(async () => {
      drawer.querySelector('button[aria-label="关闭详情"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const skillCard = [...document.querySelectorAll('.collection-source-card')].find((card) =>
      card.textContent.includes('电影生图 Skill'),
    )
    expect(skillCard.textContent).toContain('微信或聊天')
    expect(skillCard.textContent).not.toContain('v1.2.0')
    const featureTags = skillCard.querySelector('.collection-card-tags')
    expect(featureTags.textContent).toContain('中文包')
    expect(featureTags.textContent).toContain('GPT Image')
    expect(featureTags.textContent).not.toContain('微信或聊天')
    expect(featureTags.textContent).not.toContain('Skill')
    const identity = skillCard.querySelector('.collection-card-identity')
    expect(identity.textContent).toContain('微信或聊天')
    expect(identity.textContent).toContain('Skill')
    expect(identity.querySelector('.collection-meta-badge')?.dataset.tooltip).toContain('根据收藏目录中的来源标记识别')
    await act(async () => {
      skillCard.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    drawer = document.querySelector('aside[aria-label="电影生图 Skill详情"]')
    expect(drawer.textContent).toContain('v1.2.0')
    expect(drawer.textContent).toContain('仅收藏')
    expect(drawer.textContent).toContain('发现重复')
    expect(drawer.textContent).toContain('同版本副本')
    expect(drawer.textContent).toContain('incoming/cinema.skill')
    expect(drawer.textContent).toContain('archive/cinema.skill')

    await act(async () => root.unmount())
  })

  it('checks collections once on startup, does not recheck on tab entry, and keeps manual refresh', async () => {
    const initial = snapshot(3, '7777777777777777')
    const firstCollections = collectionSnapshot(5)
    const manualCollections = collectionSnapshot(4)
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
      .mockResolvedValueOnce(response({
        status: 'refreshed',
        reason: 'collection_inputs_changed',
        stable: true,
        snapshot: firstCollections,
      }))
      .mockResolvedValueOnce(response(candidateCatalog()))
      .mockResolvedValueOnce(response(manualCollections))
      .mockResolvedValueOnce(response(candidateCatalog()))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
      await flush()
    })

    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/collections/check')).toHaveLength(1)

    const navButton = (label) => [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes(label))

    await act(async () => {
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })
    expect(document.querySelectorAll('.collection-source-card')).toHaveLength(5)
    expect(document.body.textContent).toContain('检测到收藏目录变化')
    expect(document.body.textContent).toContain('启动时已检查')

    await act(async () => {
      navButton('总览').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })
    expect(document.querySelectorAll('.collection-source-card')).toHaveLength(5)
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/collections/check')).toHaveLength(1)

    const manualRefresh = [...document.querySelectorAll('button')].find((button) =>
      button.textContent.includes('手动刷新收藏索引'),
    )
    expect(manualRefresh).toBeTruthy()
    await act(async () => {
      manualRefresh.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })
    expect(document.querySelectorAll('.collection-source-card')).toHaveLength(4)
    expect(fetchMock).toHaveBeenCalledWith('/api/collections/refresh', expect.objectContaining({
      method: 'POST',
      body: '{}',
    }))

    await act(async () => root.unmount())
  })

  it('starts only one collection check under the real StrictMode entry', async () => {
    const initial = snapshot(3, '7878787878787878')
    const collections = collectionSnapshot(5)
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged',
        reason: 'input_signature_match',
        stable: true,
        snapshot: collections,
      })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(
        React.StrictMode,
        null,
        React.createElement(App),
      ))
      await flush()
      await flush()
    })

    const navButton = (label) => [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes(label))
    await act(async () => {
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      navButton('总览').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/collections/check')).toHaveLength(1)
    expect(document.querySelectorAll('.collection-source-card')).toHaveLength(5)

    await act(async () => root.unmount())
  })

  it('explains folder selection before opening the native picker and switches both pages atomically', async () => {
    const initial = snapshot(3, '8888888888888888')
    const defaults = collectionSnapshot()
    const defaultCandidates = candidateCatalog()
    const temporaryRoot = '/tmp/临时 收藏'
    const temporaryCollections = collectionSnapshot(1)
    temporaryCollections.source_root = temporaryRoot
    const temporaryCandidates = candidateCatalog()
    temporaryCandidates.source_dir = temporaryRoot
    let temporary = false
    const temporarySession = {
      mode: 'temporary', display_path: temporaryRoot, source_key: 'temporary-fixture',
    }
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true,
        snapshot: temporary ? temporaryCollections : defaults,
      })
      if (url === '/candidates/data.json') {
        return response(temporary ? temporaryCandidates : defaultCandidates)
      }
      if (url === '/api/folder-picker') return response({
        selected: true,
        selection_token: 'opaque-selection-token',
        display_path: temporaryRoot,
        expires_in: 300,
      })
      if (url === '/api/folder-selection/confirm') {
        temporary = true
        return response({
          source_session: temporarySession,
          collection_snapshot: temporaryCollections,
          candidate_catalog: temporaryCandidates,
        })
      }
      if (url === '/api/folder-source/restore') {
        temporary = false
        return response({
          source_session: health.source_session,
          collection_snapshot: defaults,
          candidate_catalog: defaultCandidates,
        })
      }
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })
    const navButton = (label) => [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes(label))
    await act(async () => {
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    const choose = document.querySelector('.collection-source-button')
    expect(choose).toBeTruthy()
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-picker')).toBe(false)
    await act(async () => {
      choose.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog).toBeTruthy()
    expect(dialog.textContent).toContain('选择前说明')
    expect(dialog.textContent).toContain('不移动、重命名、删除')
    expect(dialog.textContent).toContain('只影响当前本地服务会话中的两个页面')
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-picker')).toBe(false)

    const understand = [...dialog.querySelectorAll('button')]
      .find((button) => button.textContent.includes('了解并选择'))
    await act(async () => {
      understand.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/folder-picker')).toHaveLength(1)
    expect(document.querySelector('[role="dialog"] code')?.textContent).toBe(temporaryRoot)
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-selection/confirm')).toBe(false)

    const confirm = [...document.querySelectorAll('[role="dialog"] button')]
      .find((button) => button.textContent.includes('确认并建立只读索引'))
    await act(async () => {
      confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.querySelector('.collection-source-line')?.textContent).toContain('临时自选')
    expect(document.querySelector('.collection-source-line code')?.textContent).toBe(temporaryRoot)
    expect(fetchMock).toHaveBeenCalledWith('/api/folder-selection/confirm', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ selection_token: 'opaque-selection-token' }),
    }))

    await act(async () => {
      navButton('备选 skill 库').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(document.querySelector('.candidate-source-line code')?.textContent).toBe(temporaryRoot)
    expect(document.querySelector('.candidate-source-line')?.textContent).toContain('临时自选')

    const restore = document.querySelector('.candidate-source-line .source-restore-button')
    await act(async () => {
      restore.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(document.querySelector('.candidate-source-line code')?.textContent)
      .toBe(health.source_session.display_path)
    expect(localStorage.getItem('skill-workbench:decisions:v1:source:temporary-fixture')).toBeNull()

    await act(async () => root.unmount())
  })

  it('keeps the current source unchanged when the native picker is cancelled', async () => {
    const initial = snapshot(2, '9999999999999998')
    const defaults = collectionSnapshot(2)
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      if (url === '/api/folder-picker') return response({ selected: false, cancelled: true })
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes('全部收藏'))
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    await act(async () => {
      document.querySelector('.collection-source-button').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      ;[...document.querySelectorAll('[role="dialog"] button')]
        .find((button) => button.textContent.includes('了解并选择'))
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.querySelector('.collection-source-line code')?.textContent)
      .toBe(health.source_session.display_path)
    expect(document.body.textContent).not.toContain(defaults.source_root)
    expect(document.body.textContent).toContain('已取消选择，当前收藏来源和索引均未改变')
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-selection/confirm')).toBe(false)

    await act(async () => root.unmount())
  })

  it('explains that the native system window is pending instead of looking frozen', async () => {
    const initial = snapshot(2, '9999999999999997')
    const defaults = collectionSnapshot(2)
    let resolvePicker
    const pickerPending = new Promise((resolve) => { resolvePicker = resolve })
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      if (url === '/api/folder-picker') return pickerPending
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes('全部收藏'))
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    await act(async () => {
      document.querySelector('.collection-source-button').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      ;[...document.querySelectorAll('[role="dialog"] button')]
        .find((button) => button.textContent.includes('了解并选择'))
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog.getAttribute('aria-busy')).toBe('true')
    expect(dialog.textContent).toContain('系统文件夹窗口正在等待操作')
    expect(dialog.textContent).toContain('按 Esc 取消')
    expect(dialog.textContent).toContain('其他显示器或 Dock')
    expect(dialog.textContent).toContain('等待系统窗口选择')
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-selection/confirm')).toBe(false)

    await act(async () => {
      resolvePicker(response({ selected: false, cancelled: true }))
      await flush(); await flush()
    })
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.body.textContent).toContain('已取消选择，当前收藏来源和索引均未改变')

    await act(async () => root.unmount())
  })

  it('closes the folder explanation with Escape and restores focus to its trigger', async () => {
    const initial = snapshot(2, '9999999999999997')
    const defaults = collectionSnapshot(2)
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes('全部收藏'))
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    const trigger = document.querySelector('.collection-source-button')
    trigger.focus()
    await act(async () => {
      trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    expect(document.querySelector('[role="dialog"]')).toBeTruthy()
    expect(document.activeElement?.getAttribute('aria-label')).toBe('关闭文件夹选择说明')

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Escape', bubbles: true, cancelable: true,
      }))
      await flush(); await flush()
    })
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(trigger)
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-picker')).toBe(false)

    await act(async () => root.unmount())
  })

  it('traps forward and reverse Tab focus inside the folder explanation', async () => {
    const initial = snapshot(2, '9999999999999996')
    const defaults = collectionSnapshot(2)
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes('全部收藏'))
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    await act(async () => {
      document.querySelector('.collection-source-button')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const dialog = document.querySelector('[role="dialog"]')
    const focusable = [...dialog.querySelectorAll(
      'button:not([disabled]), [href], input:not([disabled]), select:not([disabled]), [tabindex]:not([tabindex="-1"])',
    )]
    const first = focusable[0]
    const last = focusable[focusable.length - 1]
    expect(focusable.length).toBeGreaterThanOrEqual(3)

    last.focus()
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Tab', bubbles: true, cancelable: true,
      }))
      await flush()
    })
    expect(document.activeElement).toBe(first)

    first.focus()
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Tab', shiftKey: true, bubbles: true, cancelable: true,
      }))
      await flush()
    })
    expect(document.activeElement).toBe(last)

    await act(async () => root.unmount())
  })

  it('explains an unavailable picker and never requests the native endpoint', async () => {
    const initial = snapshot(2, '9999999999999995')
    const defaults = collectionSnapshot(2)
    const unavailableHealth = {
      ...health,
      folder_picker: {
        available: false,
        provider: 'unavailable',
        reason: '当前 Python 环境缺少系统文件夹选择器。',
      },
    }
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(unavailableHealth)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const collectionsNav = [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes('全部收藏'))
    await act(async () => {
      collectionsNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    await act(async () => {
      document.querySelector('.collection-source-button')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog.textContent).toContain('当前 Python 环境缺少系统文件夹选择器')
    const understand = [...dialog.querySelectorAll('button')]
      .find((button) => button.textContent.includes('了解并选择'))
    expect(understand.disabled).toBe(true)
    await act(async () => {
      understand.click()
      await flush()
    })
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-picker')).toBe(false)

    await act(async () => root.unmount())
  })

  it('shows candidate-specific reasons before requesting the native picker', async () => {
    const initial = snapshot(2, '9999999999999994')
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/candidates/data.json') return response(candidateCatalog())
      if (url === '/api/folder-picker') return response({ selected: false, cancelled: true })
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const candidateNav = [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes('备选 skill 库'))
    await act(async () => {
      candidateNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    const trigger = document.querySelector('.candidate-source-button')
    expect(trigger).toBeTruthy()
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-picker')).toBe(false)
    await act(async () => {
      trigger.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog.textContent).toContain('为备选 Skill 库选择收藏文件夹')
    expect(dialog.textContent).toContain('只识别所选目录中可评估的 Skill 包')
    expect(dialog.textContent).toContain('确认后同一目录也会成为本次会话的“全部收藏”来源')
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-picker')).toBe(false)

    await act(async () => {
      ;[...dialog.querySelectorAll('button')]
        .find((button) => button.textContent.includes('了解并选择'))
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/folder-picker')).toHaveLength(1)
    expect(document.querySelector('[role="dialog"]')).toBeNull()

    await act(async () => root.unmount())
  })

  it('uses the session display path while retaining canonical roots for source matching', async () => {
    const initial = snapshot(2, '9999999999999994')
    const defaults = collectionSnapshot()
    const abbreviatedHealth = {
      ...health,
      source_session: {
        ...health.source_session,
        display_path: '~/Example/AbbreviatedCollection',
      },
    }
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(abbreviatedHealth)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const navButton = (label) => [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes(label))

    await act(async () => {
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
      navButton('备选 skill 库').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    expect(document.querySelector('.candidate-card')).toBeTruthy()
    expect(document.body.textContent).not.toContain('候选索引来源与当前会话收藏来源不一致')
    expect(document.querySelector('.candidate-source-line code')?.textContent)
      .toBe(abbreviatedHealth.source_session.display_path)
    expect(document.body.textContent).not.toContain(candidateCatalog().source_dir)

    await act(async () => root.unmount())
  })

  it('does not merge or display candidate records from a different source root', async () => {
    const initial = snapshot(2, '9999999999999993')
    const defaults = collectionSnapshot()
    const mismatchedCandidates = candidateCatalog()
    mismatchedCandidates.source_dir = '/tmp/另一个收藏根'
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(mismatchedCandidates)
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const navButton = (label) => [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes(label))
    await act(async () => {
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    expect(document.body.textContent).toContain('备选 Skill 数据与当前收藏来源不一致')
    expect(document.body.textContent).not.toContain('原备选库 1 张已保留')
    expect(document.body.textContent).not.toContain('把参考图整理成电影感视觉资产。')
    expect(document.body.textContent).toContain('cinema-image fixture')

    await act(async () => {
      navButton('备选 skill 库').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(document.body.textContent).toContain('候选索引来源与当前会话收藏来源不一致')
    expect(document.querySelector('.candidate-card')).toBeNull()

    await act(async () => root.unmount())
  })

  it('keeps the active source and catalogs unchanged when folder confirmation fails', async () => {
    const initial = snapshot(2, '9999999999999992')
    const defaults = collectionSnapshot()
    const defaultCandidates = candidateCatalog()
    const selectedPath = '/tmp/不稳定收藏根'
    const fetchMock = vi.fn(async (url) => {
      if (url === '/api/health') return response(health)
      if (url === '/api/snapshot') return response(initial)
      if (url === '/api/collections/check') return response({
        status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: defaults,
      })
      if (url === '/candidates/data.json') return response(defaultCandidates)
      if (url === '/api/folder-picker') return response({
        selected: true,
        selection_token: 'failing-selection-token',
        display_path: selectedPath,
        expires_in: 300,
      })
      if (url === '/api/folder-selection/confirm') return response({
        error: 'folder_source_unstable',
        message: '本次临时来源扫描失败，当前会话数据未被覆盖。',
      }, 422)
      throw new Error(`unexpected ${url}`)
    })
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => { root.render(React.createElement(App)); await flush() })
    const navButton = (label) => [...document.querySelectorAll('.sidebar-nav button')]
      .find((button) => button.textContent.includes(label))
    await act(async () => {
      navButton('全部收藏').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    await act(async () => {
      document.querySelector('.collection-source-button')
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      ;[...document.querySelectorAll('[role="dialog"] button')]
        .find((button) => button.textContent.includes('了解并选择'))
        .dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    const confirm = [...document.querySelectorAll('[role="dialog"] button')]
      .find((button) => button.textContent.includes('确认并建立只读索引'))
    await act(async () => {
      confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })

    const dialog = document.querySelector('[role="dialog"]')
    expect(dialog).toBeTruthy()
    expect(dialog.textContent).toContain('本次临时来源扫描失败，当前会话数据未被覆盖')
    expect(document.querySelector('.collection-source-line code')?.textContent)
      .toBe(health.source_session.display_path)
    expect(document.body.textContent).not.toContain(defaults.source_root)
    expect(document.querySelector('.collection-source-line')?.textContent).toContain('默认目录')
    expect(document.body.textContent).not.toContain('已为本次本地服务会话切换收藏来源')

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', {
        key: 'Escape', bubbles: true, cancelable: true,
      }))
      await flush(); await flush()
      navButton('备选 skill 库').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(document.querySelector('.candidate-source-line code')?.textContent)
      .toBe(health.source_session.display_path)
    expect(document.body.textContent).not.toContain(defaultCandidates.source_dir)
    expect(document.querySelector('.candidate-card')).toBeTruthy()

    await act(async () => root.unmount())
  })

  it('keeps Skill broad categories clean and filters by scene subcategory', async () => {
    const skillItems = [
      {
        asset_id: 'skill:seedance-generic',
        type: 'skill',
        source_name: 'seedance-generic',
        localization: { zh_name: 'Seedance 通用提示词', summary: '通用视频提示词', status: 'reviewed' },
        curation: { category: '创意生产', subcategory: 'Seedance 提示词 · 通用型' },
        source: {
          origin_kind: 'system',
          origin_basis: 'hermes_bundled_manifest',
          version: '1.0.0',
          source_refs: ['~/.hermes/skills/seedance-generic/SKILL.md'],
        },
        host_bindings: [],
      },
      {
        asset_id: 'skill:seedance-whitebox',
        type: 'skill',
        source_name: 'seedance-whitebox',
        localization: { zh_name: 'Seedance 白模特化', summary: '白模转视频', status: 'reviewed' },
        curation: { category: '创意生产', subcategory: 'Seedance 提示词 · 特化场景' },
        host_bindings: [],
      },
      {
        asset_id: 'skill:gitnexus',
        type: 'skill',
        source_name: 'gitnexus-exploring',
        localization: { zh_name: 'GitNexus 代码探索', summary: '查看代码图谱', status: 'reviewed' },
        curation: { category: '开发与代码', subcategory: 'GitNexus 代码图谱' },
        host_bindings: [],
      },
      {
        asset_id: 'plugin:creative',
        type: 'plugin',
        source_name: 'creative-production',
        localization: { zh_name: '创意生产插件', summary: '创意资产工具', status: 'reviewed' },
        curation: { category: '插件', subcategory: '视觉设计与创意生产' },
        host_bindings: [],
      },
    ]
    const initial = snapshot(3, '4444444444444444', skillItems)
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })

    const skillNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('Skill 库'),
    )
    await act(async () => {
      skillNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const category = document.querySelector('select[aria-label="分类筛选"]')
    const subcategory = document.querySelector('select[aria-label="子类目筛选"]')
    expect([...category.options].map((option) => option.textContent)).not.toContain('插件')
    expect(document.querySelector('.relationship-guide')?.textContent).toContain('关系怎么读')
    expect([...document.querySelectorAll('.scenario-section h3')].map((heading) => heading.textContent)).toEqual([
      'GitNexus 代码图谱',
      'Seedance 提示词 · 通用型',
      'Seedance 提示词 · 特化场景',
    ])
    const genericCard = [...document.querySelectorAll('.scenario-grid .asset-card')].find((card) =>
      card.textContent.includes('Seedance 通用提示词'),
    )
    const originBadge = genericCard.querySelector('.collection-meta-badge')
    expect(originBadge.textContent).toBe('系统自带')
    expect(originBadge.classList.contains('tone-green')).toBe(true)
    expect(originBadge.dataset.tooltip).toContain('Hermes 随附 Skill 清单')
    expect(originBadge.dataset.tooltip).toContain('不代表当前会话已经调用')

    await act(async () => {
      genericCard.querySelector('.asset-card-main').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    const drawer = document.querySelector('aside[aria-label="Seedance 通用提示词详情"]')
    expect(drawer.textContent).toContain('来源与身份')
    expect(drawer.textContent).toContain('系统自带')
    expect(drawer.textContent).toContain('~/.hermes/skills/seedance-generic/SKILL.md')
    await act(async () => {
      drawer.querySelector('button[aria-label="关闭详情"]').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    await act(async () => {
      category.value = '创意生产'
      category.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })
    expect([...subcategory.options].map((option) => option.textContent)).toEqual([
      '全部子类目',
      'Seedance 提示词 · 特化场景',
      'Seedance 提示词 · 通用型',
    ])

    await act(async () => {
      subcategory.value = 'Seedance 提示词 · 通用型'
      subcategory.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })
    expect(document.querySelectorAll('.scenario-grid .asset-card')).toHaveLength(1)
    expect(document.querySelector('.scenario-section h3')?.textContent).toBe('Seedance 提示词 · 通用型')
    expect(document.querySelector('.scenario-relation')?.textContent).toBe('单项')
    expect(document.querySelector('.scenario-relation')?.classList.contains('relation-single')).toBe(true)
    expect(document.querySelector('.scenario-grid')?.textContent).toContain('Seedance 通用提示词')

    await act(async () => {
      category.value = '开发与代码'
      category.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })
    expect(subcategory.value).toBe('')
    expect([...subcategory.options].map((option) => option.textContent)).toEqual(['全部子类目', 'GitNexus 代码图谱'])

    await act(async () => root.unmount())
  })

  it('shows related Plugin and MCP counts without adding them to the Skill result', async () => {
    const sceneItems = [
      {
        asset_id: 'skill:storyboard-method',
        type: 'skill',
        source_name: 'storyboard-method',
        localization: { zh_name: '分镜方法', summary: '规划视频分镜', status: 'reviewed' },
        curation: { category: '创意生产', subcategory: '视频、动画与分镜' },
        host_bindings: [],
      },
      {
        asset_id: 'skill:animation-method',
        type: 'skill',
        source_name: 'animation-method',
        localization: { zh_name: '动画方法', summary: '制作动画', status: 'reviewed' },
        curation: { category: '产品与视觉', subcategory: '视频、动画与分镜' },
        host_bindings: [],
      },
      {
        asset_id: 'plugin:storyboard',
        type: 'plugin',
        source_name: 'storyboard',
        localization: { zh_name: '分镜插件', summary: '分镜工具包', status: 'reviewed' },
        curation: { category: '插件', subcategory: '视频、动画与分镜' },
        host_bindings: [],
      },
      {
        asset_id: 'mcp:storyboard',
        type: 'mcp',
        source_name: 'storyboard-mcp',
        localization: { zh_name: '分镜连接', summary: '分镜 MCP', status: 'reviewed' },
        curation: { category: '连接', subcategory: '视频、动画与分镜' },
        source: { observation: { plugin_name: 'storyboard', basis: 'cache_manifest_projection' } },
        host_bindings: [],
      },
    ]
    const initial = snapshot(4, '5555555555555555', sceneItems)
    const fetchMock = vi.fn()
      .mockResolvedValueOnce(response(health))
      .mockResolvedValueOnce(response(initial))
    vi.stubGlobal('fetch', fetchMock)
    vi.stubGlobal('scrollTo', vi.fn())

    const container = document.createElement('div')
    document.body.append(container)
    const root = createRoot(container)
    await act(async () => {
      root.render(React.createElement(App))
      await flush()
    })

    const skillNav = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('Skill 库'),
    )
    await act(async () => {
      skillNav.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    expect(document.querySelector('.view-intro h2')?.textContent).toBe('2 / 2 个 Skill')
    expect(document.querySelector('.scenario-counts')?.textContent).toContain('当前 Skill 2')
    expect(document.querySelector('.scenario-counts')?.textContent).toContain('相关 Plugin 1')
    expect(document.querySelector('.scenario-counts')?.textContent).toContain('相关 MCP 1')
    expect(document.querySelector('.scenario-relation')?.textContent).toBe('混合关系')
    expect(document.querySelector('.observed-relation')?.textContent).toContain('1 条')
    expect(document.querySelectorAll('.scenario-grid .asset-card')).toHaveLength(2)
    expect(document.querySelectorAll('.related-asset-list button')).toHaveLength(2)

    const relatedMcp = [...document.querySelectorAll('.related-asset-list button')].find((button) =>
      button.textContent.includes('分镜连接'),
    )
    await act(async () => {
      relatedMcp.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    expect(document.querySelector('.detail-drawer')?.textContent).toContain('分镜连接')

    await act(async () => root.unmount())
  })
})

describe('项目 Skill 只读工作台', () => {
  it('启动时不请求项目 API，首次导航才惰性 GET 且只读一次', async () => {
    const { root, fetchMock } = await renderProjectWorkbench()

    expect(fetchMock.mock.calls.some(([url]) => url === '/api/project-skills')).toBe(false)

    await openProjectSkills()
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/project-skills')).toHaveLength(1)
    expect(document.body.textContent).toContain('示例项目 Alpha')

    const overview = [...document.querySelectorAll('.sidebar-nav button')].find((button) =>
      button.textContent.includes('总览'),
    )
    await act(async () => {
      overview.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    await openProjectSkills()
    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/project-skills')).toHaveLength(1)

    await act(async () => root.unmount())
  })

  it('将项目快照 404 显示为独立空态，不冒充宿主快照空态', async () => {
    const missing = response({
      error: 'project_skill_snapshot_missing',
      message: '尚未建立项目 Skill 观察快照',
    }, 404)
    const { root } = await renderProjectWorkbench(projectWorkbenchFetch({ projectGet: missing }))

    await openProjectSkills()

    const emptyState = document.querySelector('.project-skills-view .project-skills-state[role="status"]')
    expect(emptyState).toBeTruthy()
    expect(emptyState.textContent).toContain('尚未建立项目 Skill')
    expect(emptyState.textContent).toContain('手动')
    expect(document.querySelector('.first-observation')).toBeNull()
    expect(document.querySelector('button[aria-label="刷新项目 Skill"]')).toBeTruthy()

    await act(async () => root.unmount())
  })

  it('手动刷新只调用专用端点并携带 CSRF，不调用宿主 /api/refresh', async () => {
    const refreshed = projectSkillSnapshot({
      generation_id: 'a1b2c3d4e5f60003',
      generated_at: '2026-08-11T14:00:00Z',
      projects: [
        ...projectSkillSnapshot().projects,
        {
          project_id: 'project-gamma',
          relative_path: 'example-project-gamma',
          display_name: '示例项目 Gamma',
          classification: 'project',
          scan_status: 'complete',
          entries: [],
          logical_skills: [],
        },
      ],
    })
    let projectReadCount = 0
    const { root, fetchMock } = await renderProjectWorkbench(projectWorkbenchFetch({
      projectGet: () => {
        projectReadCount += 1
        return response(projectSkillView(projectReadCount === 1 ? projectSkillSnapshot() : refreshed))
      },
      projectRefresh: response(refreshed),
    }))
    await openProjectSkills()

    const refreshButton = document.querySelector('button[aria-label="刷新项目 Skill"]')
    expect(refreshButton).toBeTruthy()
    await act(async () => {
      refreshButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(fetchMock).toHaveBeenCalledWith('/api/project-skills/refresh', expect.objectContaining({
      method: 'POST',
      headers: expect.objectContaining({
        'Content-Type': 'application/json',
        'X-AI-Toolbox-CSRF': 'fixture-csrf',
      }),
      body: '{}',
    }))
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/refresh')).toBe(false)
    expect(document.body.textContent).toContain('示例项目 Gamma')
    expect(document.body.textContent).toContain('项目 Skill 只读观察已更新')

    await act(async () => root.unmount())
  })

  it('完整快照展示独立计数和五层证据，不把低层事实升级为可用或已使用', async () => {
    const { root } = await renderProjectWorkbench()
    await openProjectSkills()

    const page = document.querySelector('.project-skills-view')
    expect(page).toBeTruthy()
    expect(page.textContent).toContain('只读观察')
    const metricValue = (label) => [...page.querySelectorAll('.project-skill-metric-card')]
      .find((card) => card.querySelector('span')?.textContent === label)?.querySelector('strong')?.textContent
    expect(metricValue('登记对象')).toBeUndefined()
    expect(metricValue('登记项目')).toBe('2')
    expect(metricValue('容器')).toBe('1')
    expect(metricValue('未分类候选')).toBe('1')
    expect(metricValue('项目内逻辑 Skill')).toBe('1')
    expect(metricValue('文件观察')).toBeUndefined()
    expect(metricValue('项目绑定证据')).toBeUndefined()
    expect(metricValue('人工关联')).toBeUndefined()
    expect(page.textContent).not.toContain('项目分类')

    const projectMetric = page.querySelector('button[aria-label="查看登记项目详情"]')
    await act(async () => {
      projectMetric.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    let classificationDrawer = document.querySelector('[role="dialog"]')
    expect(classificationDrawer.textContent).toContain('登记项目')
    expect(classificationDrawer.textContent).toContain('示例项目 Alpha')
    expect(classificationDrawer.textContent).toContain('示例项目 Beta')
    expect(classificationDrawer.textContent).not.toContain('container-codex')
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flush()
    })
    expect(document.activeElement).toBe(projectMetric)

    const containerMetric = page.querySelector('button[aria-label="查看容器详情"]')
    await act(async () => {
      containerMetric.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    classificationDrawer = document.querySelector('[role="dialog"]')
    expect(classificationDrawer.textContent).toContain('登记容器')
    expect(classificationDrawer.textContent).toContain('container-codex')
    expect(classificationDrawer.textContent).toContain('不扫描')
    expect(classificationDrawer.textContent).not.toContain('示例项目 Alpha')
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flush()
    })

    const projectSelect = page.querySelector('select[aria-label="选择项目文件夹"]')
    expect(projectSelect).toBeTruthy()
    expect(document.querySelector('.project-selected-library')).toBeNull()
    await act(async () => {
      projectSelect.value = 'project-alpha'
      projectSelect.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })

    expect(document.querySelector('.project-selected-library')?.textContent).toContain('工具链与工作流设计')
    expect(document.querySelectorAll('.project-selected-library')).toHaveLength(1)
    const skillSummary = document.querySelector('.project-skill-library-card .asset-card-main')
    expect(skillSummary).toBeTruthy()
    await act(async () => {
      skillSummary.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    const evidence = document.querySelector('[aria-label="Skill 五层证据"]')
    expect(evidence).toBeTruthy()
    expect(evidence.textContent).toContain('文件发现')
    expect(evidence.textContent).toContain('项目绑定')
    expect(evidence.textContent).toContain('宿主可用')
    expect(evidence.textContent).toContain('调用资格')
    expect(evidence.textContent).toContain('实际使用')
    expect(evidence.textContent).toContain('已观察到文件')
    expect(evidence.textContent).toContain('已观察到项目绑定候选')
    expect(evidence.textContent).toContain('未核实')
    expect(evidence.textContent).toContain('使用证据未接入')
    expect(evidence.textContent).not.toContain('已安装')
    expect(evidence.textContent).not.toContain('已可用')
    expect(evidence.textContent).not.toContain('从未使用')
    const projectDrawer = document.querySelector('[role="dialog"][aria-modal="true"]')
    expect(projectDrawer).toBeTruthy()
    expect(projectDrawer.textContent).toContain('三阶段内容生产编排')
    expect(projectDrawer.textContent).toContain('中文覆盖 · 已复核')
    expect(projectDrawer.textContent).toContain('frontmatter 原文')
    expect(projectDrawer.textContent).toContain('三阶段内容制作流程。')
    expect(projectDrawer.textContent).not.toContain('AI 生成')
    expect(projectDrawer.textContent).toContain('启动并审核三阶段内容生产流程')
    expect(projectDrawer.textContent).toContain('跳过阶段间人工确认')
    expect(projectDrawer.textContent).toContain('从这版剧本启动流程')
    expect(projectDrawer.textContent).toContain('能力简介')
    expect(projectDrawer.textContent).toContain('适用场景')
    expect(projectDrawer.textContent).toContain('不适用场景')
    expect(projectDrawer.textContent).toContain('使用示意')
    expect(projectDrawer.textContent).not.toContain('当前不作推断')
    expect(projectDrawer.textContent).toContain('项目观察关系（不代表宿主可用）')
    expect(projectDrawer.textContent).toContain('来源与身份')
    expect(projectDrawer.textContent).toContain('frontmatter-only')
    expect(projectDrawer.textContent).toContain('投影指纹')
    expect(document.querySelector('.project-skill-library-card .detail-drawer')).toBeNull()
    expect(document.querySelector('body > .detail-drawer')).toBeTruthy()
    expect(document.querySelector('body > .drawer-scrim')).toBeTruthy()
    expect(document.activeElement?.getAttribute('aria-label')).toBe('关闭详情')
    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flush()
    })
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(document.activeElement).toBe(skillSummary)

    await act(async () => root.unmount())
  })

  it('ai_draft 中文覆盖标注 AI 生成并保留英文原文', async () => {
    const draftSnapshot = projectSkillSnapshot()
    draftSnapshot.projects[0].logical_skills[0].localization = {
      ...draftSnapshot.projects[0].logical_skills[0].localization,
      status: 'ai_draft',
    }
    const { root } = await renderProjectWorkbench(projectWorkbenchFetch({
      projectGet: response(projectSkillView(draftSnapshot)),
    }))
    await openProjectSkills()

    const projectSelect = document.querySelector('select[aria-label="选择项目文件夹"]')
    await act(async () => {
      projectSelect.value = 'project-alpha'
      projectSelect.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })

    const card = document.querySelector('.project-skill-library-card')
    expect(card.textContent).toContain('中文 · AI 生成')

    await act(async () => {
      card.querySelector('.asset-card-main').dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })
    const projectDrawer = document.querySelector('[role="dialog"][aria-modal="true"]')
    expect(projectDrawer.textContent).toContain('中文覆盖 · AI 生成')
    expect(projectDrawer.textContent).not.toContain('待人工确认')
    expect(projectDrawer.textContent).toContain('frontmatter 原文')
    expect(projectDrawer.textContent).toContain('三阶段内容制作流程。')
    expect(projectDrawer.querySelectorAll('.ai-generated-tag')).toHaveLength(4)

    await act(async () => root.unmount())
  })

  it('从项目下拉菜单选择其他文件夹并只读展示该项目 Skill', async () => {
    const temporary = projectSkillSnapshot({
      generation_id: 'b1b2c3d4e5f60001',
      candidates: [],
      projects: [{
        ...projectSkillSnapshot().projects[0],
        project_id: 'session-project:temporary1234',
        relative_path: 'temporary-example-project',
        display_name: '临时示例项目',
      }],
      human_associations: {
        registry_id: 'project-skill-session-associations-v1',
        confirmed_on: '2026-08-11',
        items: [],
      },
    })
    const fetchMock = projectWorkbenchFetch()
    fetchMock.mockImplementation(async (url, options = {}) => {
      if (url === '/api/health') return response({
        ...health,
        project_skill_snapshot: { available: true, refresh_state: 'idle', integrity: { degraded: false, errors: [] } },
      })
      if (url === '/api/snapshot') return response(snapshot(3, 'abcdefabcdef0001'))
      if (url === '/api/collections/check') return response({ status: 'unchanged', reason: 'input_signature_match', stable: true, snapshot: collectionSnapshot() })
      if (url === '/candidates/data.json') return response(candidateCatalog())
      if (url === '/api/project-skills') return response(projectSkillView())
      if (url === '/api/folder-picker') return response({
        selected: true,
        selection_token: 'project-skill-folder-token',
        display_path: '~/Example/TemporaryProject',
        expires_in: 120,
      })
      if (url === '/api/project-skills/folder-selection/confirm') return response({
        snapshot: temporary,
        selection: { mode: 'temporary', display_path: '~/Example/TemporaryProject' },
      })
      throw new Error(`unexpected ${url}`)
    })
    const { root } = await renderProjectWorkbench(fetchMock)
    await openProjectSkills()

    const select = document.querySelector('select[aria-label="选择项目文件夹"]')
    await act(async () => {
      select.value = '__choose_other_project_folder__'
      select.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })
    let dialog = document.querySelector('[role="dialog"]')
    expect(dialog.textContent).toContain('选择其他项目文件夹')
    expect(dialog.textContent).toContain('不加入项目登记')
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-picker')).toBe(false)

    const choose = [...dialog.querySelectorAll('button')].find((button) => button.textContent.includes('了解并选择'))
    await act(async () => {
      choose.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/folder-picker', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ target: 'project-skill-source' }),
    }))
    dialog = document.querySelector('[role="dialog"]')
    expect(dialog.textContent).toContain('~/Example/TemporaryProject')

    const confirm = [...dialog.querySelectorAll('button')].find((button) => button.textContent.includes('确认并读取项目 Skill'))
    await act(async () => {
      confirm.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush(); await flush()
    })
    expect(fetchMock).toHaveBeenCalledWith('/api/project-skills/folder-selection/confirm', expect.objectContaining({
      method: 'POST',
      body: JSON.stringify({ selection_token: 'project-skill-folder-token' }),
    }))
    expect(document.querySelector('[role="dialog"]')).toBeNull()
    expect(select.value).toBe('session-project:temporary1234')
    expect([...select.options].some((option) => option.textContent === '临时：临时示例项目')).toBe(true)
    expect(document.querySelector('.project-selected-library')?.textContent).toContain('临时示例项目')
    expect(document.querySelectorAll('.project-selected-library')).toHaveLength(1)
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/project-skills/refresh')).toBe(false)
    expect(fetchMock.mock.calls.some(([url]) => url === '/api/folder-selection/confirm')).toBe(false)

    await act(async () => root.unmount())
  })

  it('部分刷新保留上一份完整快照，不把未确认对象显示为已删除', async () => {
    const partial = response({
      error: 'project_skill_scan_incomplete',
      message: '本次项目 Skill 观察不完整，上一份有效快照未被覆盖',
      scan_status: 'partial',
      generation_id: 'deadbeefdead0001',
    }, 422)
    let projectReadCount = 0
    const failedAttemptView = projectSkillView(projectSkillSnapshot(), {
      last_attempt: {
        schema_version: 1,
        observation_boundary_ref: 'project-skill-observation-boundary-v1',
        attempted_at: '2026-08-11T15:00:00Z',
        attempt_generation_id: 'deadbeefdead0001',
        scan_status: 'partial',
        promoted: false,
        matches_complete_snapshot: false,
        complete_snapshot_generation_id: 'a1b2c3d4e5f60001',
        issues: [
          { status: 'partial', code: 'budget_exceeded', project_id: 'project-alpha' },
          { status: 'security_reject', code: 'path_race', relative_path: '.agents/skills/example' },
        ],
      },
    })
    const { root } = await renderProjectWorkbench(projectWorkbenchFetch({
      projectGet: () => {
        projectReadCount += 1
        return response(projectReadCount === 1 ? projectSkillView() : failedAttemptView)
      },
      projectRefresh: partial,
    }))
    await openProjectSkills()

    const refreshButton = document.querySelector('button[aria-label="刷新项目 Skill"]')
    await act(async () => {
      refreshButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
      await flush()
    })

    expect(document.body.textContent).toContain('示例项目 Alpha')
    const alert = document.querySelector('.project-skills-retained-note[role="alert"]')
    expect(alert).toBeTruthy()
    expect(alert.textContent).toContain('本次未能确认')
    expect(alert.textContent).toContain('保留上一份完整结果')
    expect(alert.textContent).toContain('不把未观察到的对象解释为已删除')
    expect(alert.textContent).toContain('budget_exceeded')
    expect(alert.textContent).toContain('path_race')

    await act(async () => root.unmount())
  })

  it('辅助回执或 Markdown 降级时仍显示有效机器快照并明确降级', async () => {
    const degraded = projectSkillView(projectSkillSnapshot(), {
      last_attempt: null,
      report: { available: false, generation_id: null },
      integrity: {
        degraded: true,
        errors: ['last_attempt_missing', 'report_invalid'],
      },
    })
    const { root } = await renderProjectWorkbench(projectWorkbenchFetch({
      projectGet: response(degraded),
    }))
    await openProjectSkills()

    expect(document.body.textContent).toContain('示例项目 Alpha')
    const degradedNotice = document.querySelector('.project-skills-integrity-note')
    expect(degradedNotice).toBeTruthy()
    expect(degradedNotice.textContent).toContain('机器快照仍可读')
    expect(degradedNotice.textContent).toContain('Markdown 总览未通过同代校验')
    expect(document.body.textContent).not.toContain('/' + 'Users/')

    await act(async () => root.unmount())
  })

  it('只读打开同代校验的 Markdown 总览且不请求 generated 静态路径', async () => {
    const { root, fetchMock } = await renderProjectWorkbench(projectWorkbenchFetch())
    await openProjectSkills()

    const openButton = [...document.querySelectorAll('button')].find((button) =>
      button.classList.contains('project-skills-report-open'),
    )
    expect(openButton).toBeTruthy()
    await act(async () => {
      openButton.dispatchEvent(new MouseEvent('click', { bubbles: true }))
      await flush()
    })

    const dialog = document.querySelector('[role="dialog"][aria-labelledby="project-skills-report-title"]')
    expect(dialog).toBeTruthy()
    expect(dialog.textContent).toContain('项目 Skill Markdown 总览')
    expect(dialog.textContent).toContain('a1b2c3d4e5f60001')
    expect(document.activeElement?.getAttribute('aria-label')).toBe('关闭 Markdown 总览')
    expect(fetchMock.mock.calls.some(([url]) => String(url).includes('/generated/project-skills/'))).toBe(false)

    await act(async () => {
      document.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
      await flush()
    })
    expect(document.querySelector('#project-skills-report-title')).toBeNull()
    expect(document.activeElement).toBe(openButton)
    await act(async () => root.unmount())
  })

  it('人工关联与机器观察分区显示，不改写机器 Skill 计数', async () => {
    const { root } = await renderProjectWorkbench()
    await openProjectSkills()

    const projectSelect = document.querySelector('select[aria-label="选择项目文件夹"]')
    expect(projectSelect).toBeTruthy()
    await act(async () => {
      projectSelect.value = 'project-beta'
      projectSelect.dispatchEvent(new Event('change', { bubbles: true }))
      await flush()
    })

    const selected = document.querySelector('.project-selected-library')
    const human = document.querySelector('[aria-label="人工关联"]')
    expect(selected).toBeTruthy()
    expect(human).toBeTruthy()
    expect(selected.textContent).toContain('0 个 Skill')
    expect(selected.textContent).toContain('当前项目没有可展示的 Skill')
    expect(selected.querySelectorAll('.project-skill-library-card')).toHaveLength(0)
    expect(human.textContent).toContain('apple-design')
    expect(human.textContent).toContain('人工关联')
    expect(human.textContent).toContain('不代表项目内已安装或当前宿主可用')

    await act(async () => root.unmount())
  })

  it('将项目和 Skill 的危险元数据当作纯文本渲染', async () => {
    const attack = '<img id="project-skill-xss" src=x onerror="window.__projectSkillXss=1">'
    const unsafe = projectSkillSnapshot()
    unsafe.projects = [
      {
        ...unsafe.projects[0],
        display_name: attack,
        logical_skills: [
          {
            ...unsafe.projects[0].logical_skills[0],
            display_name: attack,
          },
        ],
      },
    ]
    unsafe.human_associations = {
      ...unsafe.human_associations,
      items: [],
    }
    const { root } = await renderProjectWorkbench(projectWorkbenchFetch({
      projectGet: response(projectSkillView(unsafe)),
    }))
    await openProjectSkills()

    expect(document.querySelector('#project-skill-xss')).toBeNull()
    expect(window.__projectSkillXss).toBeUndefined()
    expect(document.querySelector('.project-skills-view')?.textContent).toContain(attack)

    await act(async () => root.unmount())
  })

  it('项目 Skill 可从移动端导航直接到达', async () => {
    const { root, fetchMock } = await renderProjectWorkbench()

    const mobileEntry = projectNavButton('.bottom-nav')
    expect(mobileEntry).toBeTruthy()
    await openProjectSkills('.bottom-nav')

    expect(fetchMock.mock.calls.filter(([url]) => url === '/api/project-skills')).toHaveLength(1)
    expect(document.querySelector('.topbar')?.textContent).toContain('项目 Skill')
    expect(document.querySelector('.project-skills-view')).toBeTruthy()

    await act(async () => root.unmount())
  })
})
