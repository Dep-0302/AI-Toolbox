import { describe, expect, it } from 'vitest'
import {
  candidateDescriptionState,
  candidateDisplayItems,
  candidateHealthFindings,
  categoriesFor,
  coverageNotice,
  filterAssets,
  hostStats,
  issueGroups,
  itemLabel,
  matchesSearch,
  observationFacts,
  reconcileSelectedItem,
  snapshotAge,
  subcategoriesFor,
} from './toolbox'

const assets = [
  {
    asset_id: 'skill:frontend',
    type: 'skill',
    source_name: 'frontend-builder',
    description: 'Build responsive interfaces',
    localization: {
      zh_name: '前端应用构建',
      summary: '构建响应式 Web 应用',
      use_cases: ['内部工具'],
      synonyms: ['网页开发'],
      status: 'reviewed',
    },
    curation: { category: '开发与工程', subcategory: 'Web 应用与界面构建' },
    host_bindings: [
      { host_id: 'codex' },
      { host_id: 'claude' },
    ],
  },
  {
    asset_id: 'plugin:airtable',
    type: 'plugin',
    source_name: 'airtable',
    localization: { zh_name: 'Airtable 数据协作', status: 'stale' },
    curation: { category: '插件', subcategory: '数据、表格与业务分析' },
    host_bindings: [{ host_id: 'codex' }],
  },
]

const coverageHosts = ['codex', 'claude', 'hermes', 'workbuddy', 'antigravity']

function coverageRows(assetType, overrides = {}) {
  return coverageHosts.map((hostId) => ({
    host_id: hostId,
    asset_type: assetType,
    adapter_id: null,
    status: hostId === 'antigravity' ? 'placeholder' : 'not_connected',
    source_refs: [],
    configured_source_count: 0,
    observed_source_count: 0,
    item_count: 0,
    ...(overrides[hostId] || {}),
  }))
}

function snapshotWithCoverage(rows) {
  return { scan_scope: { coverage: rows } }
}

describe('toolbox data helpers', () => {
  it('searches Chinese names, synonyms, and use cases', () => {
    expect(matchesSearch(assets[0], '网页 内部')).toBe(true)
    expect(matchesSearch(assets[0], '界面构建')).toBe(true)
    expect(matchesSearch(assets[0], '视频')).toBe(false)
  })

  it('keeps broad categories scoped to the current asset collection and derives child categories', () => {
    const skills = assets.filter((item) => item.type === 'skill')
    expect(categoriesFor(skills)).toEqual(['开发与工程'])
    expect(subcategoriesFor(skills, '开发与工程')).toEqual(['Web 应用与界面构建'])
    expect(categoriesFor(skills)).not.toContain('插件')
  })

  it('filters without changing the source collection', () => {
    const filtered = filterAssets(assets, {
      host: 'claude',
      types: ['skill'],
      query: '前端',
    })
    expect(filtered.map(itemLabel)).toEqual(['前端应用构建'])
    expect(assets).toHaveLength(2)
  })

  it('combines broad and child category filters', () => {
    expect(
      filterAssets(assets, {
        types: ['skill'],
        category: '开发与工程',
        subcategory: 'Web 应用与界面构建',
      }),
    ).toEqual([assets[0]])
    expect(filterAssets(assets, { types: ['skill'], subcategory: '数据、表格与业务分析' })).toEqual([])
  })

  it('combines favorite and multi-host filters', () => {
    const filtered = filterAssets(assets, {
      favorites: new Set(['skill:frontend']),
      favoriteOnly: true,
      multiHost: true,
    })
    expect(filtered).toEqual([assets[0]])
  })

  it('derives host counts from bindings, not activation claims', () => {
    const stats = hostStats({
      hosts: [
        { id: 'codex', label: 'Codex', status: 'observed' },
        { id: 'claude', label: 'Claude', status: 'observed' },
      ],
      items: assets,
      scan_scope: { roots: [] },
    })
    expect(stats.find((host) => host.id === 'codex').assetCount).toBe(2)
    expect(stats.find((host) => host.id === 'claude').assetCount).toBe(1)
  })

  it('derives per-type host counts for tool filters', () => {
    const mcp = {
      asset_id: 'mcp:filesystem',
      type: 'mcp',
      host_bindings: [{ host_id: 'codex' }],
    }
    const stats = hostStats({
      hosts: [{ id: 'codex', label: 'Codex', status: 'observed' }],
      items: [...assets, mcp],
      scan_scope: { roots: [] },
    })
    const codex = stats.find((host) => host.id === 'codex')
    expect(codex.countsByType.skill).toBe(1)
    expect(codex.countsByType.plugin).toBe(1)
    expect(codex.countsByType.mcp).toBe(1)
  })

  it('distinguishes an observed empty source from an unconnected source', () => {
    const snapshot = snapshotWithCoverage(
      coverageRows('mcp', {
        codex: {
          adapter_id: 'codex_plugin_mcp',
          status: 'observed',
          source_refs: ['~/.codex/plugins/cache/*/.mcp.json'],
          configured_source_count: 1,
          observed_source_count: 1,
        },
      }),
    )
    const codex = coverageNotice(snapshot, 'mcp', 'codex')
    const claude = coverageNotice(snapshot, 'mcp', 'claude')
    const allHosts = coverageNotice(snapshot, 'mcp')
    expect(codex.status).toBe('observed')
    expect(codex.message).toContain('本次未观察到声明')
    expect(codex.message).toContain('不证明本机其他位置不存在')
    expect(claude.status).toBe('not_connected')
    expect(claude.message).toContain('尚未接入观察源')
    expect(claude.message).toContain('不表示本机不存在')
    expect(allHosts.message).toContain('已完成的限定来源本次未观察到声明')
  })

  it('describes Codex-only tool observation as limited host coverage', () => {
    const snapshot = snapshotWithCoverage(
      coverageRows('cli', {
        codex: {
          adapter_id: 'codex_cli_metadata_v1',
          status: 'observed',
          source_refs: ['~/.local/bin/codex'],
          configured_source_count: 1,
          observed_source_count: 1,
          item_count: 1,
        },
      }),
    )
    const notice = coverageNotice(snapshot, 'cli')
    expect(notice.status).toBe('limited')
    expect(notice.tone).toBe('info')
    expect(notice.message).toContain('CLI 当前为局部覆盖')
    expect(notice.message).toContain('已完成：Codex')
    expect(notice.message).toContain('未接入：Claude、Hermes、WorkBuddy')
    expect(notice.message).toContain('不是全机或全部宿主总量')
    expect(notice.message).toContain('~/.local/bin/codex')
    expect(notice.message).toContain('不读取或执行二进制体')
  })

  it('projects exact safe observation facts without operation fields', () => {
    const mcpFacts = observationFacts({
      type: 'mcp',
      source: {
        observation: {
          adapter_id: 'codex_plugin_mcp_v1',
          server_id: 'fixture',
          transport_kind: 'stdio',
          sensitive_config_present: true,
          command: 'must-not-render-command',
          args: ['must-not-render-argument'],
          env: { TOKEN: 'must-not-render-token' },
          headers: { Authorization: 'must-not-render-header' },
        },
      },
    })
    const renderedFacts = mcpFacts.map((fact) => fact.value).join(' ')
    expect(renderedFacts).toContain('.mcp.json')
    expect(renderedFacts).toContain('具体字段与值已省略')
    expect(renderedFacts).not.toContain('must-not-render')
    expect(
      observationFacts({
        type: 'cli',
        source: { observation: { adapter_id: 'codex_cli_metadata_v1' } },
      }).map((fact) => fact.value).join(' '),
    ).toContain('未读取或执行二进制体')
    expect(
      observationFacts({
        type: 'sdk',
        source: { observation: { adapter_id: 'codex_plugin_sdk_v1' } },
      }).map((fact) => fact.value).join(' '),
    ).toContain('直接依赖白名单')
  })

  it('marks an incomplete adapter result as unusable for a complete count', () => {
    const snapshot = snapshotWithCoverage(
      coverageRows('sdk', {
        codex: {
          adapter_id: 'codex_sdk_manifest',
          status: 'partial',
          source_refs: ['~/.codex/plugins/cache/*/package.json'],
          configured_source_count: 2,
          observed_source_count: 1,
          item_count: 1,
        },
      }),
    )
    const notice = coverageNotice(snapshot, 'sdk', 'codex')
    expect(notice.status).toBe('partial')
    expect(notice.tone).toBe('error')
    expect(notice.message).toContain('本次观察不完整')
    expect(notice.message).toContain('不能作为完整盘点数量')
  })

  it('keeps an aggregate scan failure distinct from limited host coverage', () => {
    const snapshot = snapshotWithCoverage(
      coverageRows('sdk', {
        codex: {
          adapter_id: 'codex_plugin_sdk_v1',
          status: 'partial',
          source_refs: ['~/.codex/plugins/cache'],
          configured_source_count: 1,
          observed_source_count: 0,
          item_count: 0,
        },
      }),
    )
    const notice = coverageNotice(snapshot, 'sdk')
    expect(notice.status).toBe('partial')
    expect(notice.tone).toBe('error')
    expect(notice.message).toContain('SDK 观察不完整')
  })

  it('rebinds an open detail to refreshed data and closes removed assets', () => {
    const current = { asset_id: 'mcp:filesystem', description: 'old' }
    const refreshed = { asset_id: 'mcp:filesystem', description: 'new' }
    expect(reconcileSelectedItem(current, [refreshed])).toBe(refreshed)
    expect(reconcileSelectedItem(current, [])).toBeNull()
    expect(reconcileSelectedItem(null, [refreshed])).toBeNull()
  })

  it('groups findings and promotes the highest severity', () => {
    const groups = issueGroups([
      { code: 'localization_stale', severity: 'info' },
      { code: 'localization_stale', severity: 'warning' },
      { code: 'name_collision', severity: 'warning' },
    ])
    expect(groups[0].severity).toBe('warning')
    expect(groups.find((group) => group.code === 'localization_stale').findings).toHaveLength(2)
  })

  it('separates truncated candidate summaries from real parsing errors', () => {
    expect(candidateDescriptionState({ desc_flags: ['truncated'] })).toEqual({
      parsingError: false,
      truncated: true,
    })
    expect(candidateDescriptionState({ desc_flags: ['truncated', 'frontmatter_bleed'] })).toEqual({
      parsingError: true,
      truncated: false,
    })
    expect(candidateDescriptionState({ desc_flags: 'truncated' })).toEqual({
      parsingError: false,
      truncated: false,
    })
  })

  it('projects candidate quality facts without inventing host asset identities', () => {
    const findings = candidateHealthFindings({
      items: [
        {
          uid: 'candidate-stale',
          name: 'stale-skill',
          zh_name: '待复核候选',
          zh_state: 'stale',
          desc_flags: ['truncated'],
          src: '收藏/stale.skill',
        },
        {
          uid: 'candidate-missing',
          name: 'missing-skill',
          zh_state: 'missing',
          desc_flags: [],
          installed: { hosts: ['codex', 'hermes'] },
        },
        {
          uid: 'candidate-broken',
          name: 'broken-skill',
          desc_flags: ['desc_broken'],
        },
        {
          uid: 'candidate-frontmatter',
          name: 'frontmatter-skill',
          desc_flags: ['truncated', 'frontmatter_bleed'],
        },
        {
          uid: 'candidate-duplicate',
          name: 'duplicate-stale-skill',
          zh_state: 'stale',
          desc_flags: ['truncated'],
        },
        {
          uid: 'candidate-duplicate',
          name: 'duplicate-stale-skill-second',
          zh_state: 'stale',
          desc_flags: ['truncated'],
        },
        {
          uid: 'candidate-malformed-title',
          zh_name: { bad: true },
          name: 'safe-fallback-name',
          zh_state: 'stale',
        },
        { uid: '__proto__', name: 'unsafe', zh_state: 'stale' },
      ],
    })

    expect(findings).toHaveLength(6)
    expect(findings.map((finding) => finding.code)).toEqual([
      'candidate_localization_stale',
      'candidate_description_truncated',
      'candidate_localization_missing',
      'candidate_description_broken',
      'candidate_description_broken',
      'candidate_localization_stale',
    ])
    expect(findings.every((finding) => finding.scope === 'candidate')).toBe(true)
    expect(findings.every((finding) => !Object.hasOwn(finding, 'asset_id'))).toBe(true)
    expect(findings.every((finding) => !Object.hasOwn(finding, 'installed'))).toBe(true)
    expect(findings.every((finding) => !Object.hasOwn(finding, 'hosts'))).toBe(true)
    expect(findings[0]).toMatchObject({
      candidate_uid: 'candidate-stale',
      title: '待复核候选',
      severity: 'warning',
      path: '收藏/stale.skill',
    })
    expect(findings.at(-1).title).toBe('safe-fallback-name')
    expect(findings.some((finding) => finding.candidate_uid === 'candidate-duplicate')).toBe(false)
    expect(candidateHealthFindings(null)).toEqual([])
    expect(candidateHealthFindings({ items: 'bad' })).toEqual([])
  })

  it('builds a crash-safe candidate view and excludes every duplicate-UID row', () => {
    const items = candidateDisplayItems({
      items: [
        null,
        { uid: 'duplicate', name: 'first', chars: 10 },
        { uid: 'duplicate', name: 'second', chars: 20 },
        {
          uid: 'safe',
          name: 'safe-name',
          zh_name: { bad: true },
          zh_sum: ['bad'],
          platform: 'bad',
          inputs: [null, '参考图'],
          tags: { bad: true },
          chars: 'many',
          shape: { total: 'bad', 参考资料: 2 },
          installed: { hosts: ['codex', null] },
        },
      ],
    })

    expect(items).toHaveLength(1)
    expect(items[0]).toMatchObject({
      uid: 'safe',
      name: 'safe-name',
      zh_name: '',
      zh_sum: '',
      platform: [],
      inputs: ['参考图'],
      tags: [],
      chars: 0,
      shape: { 参考资料: 2 },
      installed: { hosts: ['codex'] },
    })
  })

  it('maps the frozen 4 stale, 5 missing, 19 truncated, and 1 broken fixture to 29 records', () => {
    const items = []
    for (let index = 0; index < 4; index += 1) {
      items.push({ uid: `stale-${index}`, name: `stale-${index}`, zh_state: 'stale' })
    }
    for (let index = 0; index < 5; index += 1) {
      items.push({ uid: `missing-${index}`, name: `missing-${index}`, zh_state: 'missing' })
    }
    for (let index = 0; index < 19; index += 1) {
      items.push({ uid: `truncated-${index}`, name: `truncated-${index}`, desc_flags: ['truncated'] })
    }
    items.push({ uid: 'broken-0', name: 'broken-0', desc_flags: ['desc_broken'] })

    const findings = candidateHealthFindings({ items })
    const count = (code) => findings.filter((finding) => finding.code === code).length
    expect(findings).toHaveLength(29)
    expect(count('candidate_localization_stale')).toBe(4)
    expect(count('candidate_localization_missing')).toBe(5)
    expect(count('candidate_description_truncated')).toBe(19)
    expect(count('candidate_description_broken')).toBe(1)
  })

  it('describes recent and older snapshot ages', () => {
    const now = new Date('2026-08-06T12:00:00Z').valueOf()
    expect(snapshotAge('2026-08-06T11:40:00Z', now)).toBe('20 分钟前')
    expect(snapshotAge('2026-08-04T12:00:00Z', now)).toBe('2 天前')
  })
})
