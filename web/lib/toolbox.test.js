import { describe, expect, it } from 'vitest'
import {
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

  it('describes recent and older snapshot ages', () => {
    const now = new Date('2026-08-06T12:00:00Z').valueOf()
    expect(snapshotAge('2026-08-06T11:40:00Z', now)).toBe('20 分钟前')
    expect(snapshotAge('2026-08-04T12:00:00Z', now)).toBe('2 天前')
  })
})
