import { describe, expect, it } from 'vitest'
import {
  groupAssetsByScenario,
  observedProviderLinks,
  relationDefinitionFor,
  scenarioContextFor,
  scenarioGuideFor,
} from './scenarios'

const videoSkill = {
  asset_id: 'skill:video',
  type: 'skill',
  source_name: 'video-maker',
  localization: { zh_name: '视频制作方法' },
  curation: { category: '创意生产', subcategory: '视频、动画与分镜' },
}

const videoPlugin = {
  asset_id: 'plugin:storyboard',
  type: 'plugin',
  source_name: 'storyboard',
  localization: { zh_name: '分镜插件' },
  curation: { category: '插件', subcategory: '视频、动画与分镜' },
}

const videoMcp = {
  asset_id: 'mcp:storyboard',
  type: 'mcp',
  source_name: 'storyboard-mcp',
  localization: { zh_name: '分镜连接' },
  curation: { category: '连接', subcategory: '视频、动画与分镜' },
  source: { observation: { plugin_name: 'storyboard', basis: 'cache_manifest_projection' } },
}

describe('scenario grouping and explicit relationships', () => {
  it('groups each asset once, uses stable taxonomy order, and keeps unclassified items last', () => {
    const unclassified = {
      asset_id: 'skill:unknown',
      type: 'skill',
      source_name: 'unknown',
      curation: { category: '其他', subcategory: null },
    }
    const memory = {
      asset_id: 'skill:memory',
      type: 'skill',
      source_name: 'memory',
      curation: { category: '协作与管理', subcategory: 'AgentMemory 与长期记忆' },
    }
    const input = [unclassified, videoSkill, memory, videoSkill]
    const groups = groupAssetsByScenario(input)

    expect(groups.map((group) => group.subcategory)).toEqual([
      'AgentMemory 与长期记忆',
      '视频、动画与分镜',
      '未细分',
    ])
    expect(groups[1].items).toEqual([videoSkill])
    expect(groups[1].guide.relationKind).toBe('single')
    expect(groups[1].guide.relationLabel).toBe('单项')
    expect(input).toHaveLength(4)
  })

  it('keeps exact relation types separate from group states', () => {
    expect(relationDefinitionFor('alternative').label).toBe('备选')
    expect(relationDefinitionFor('conflict').label).toBe('冲突')
    expect(relationDefinitionFor('complementary').label).toBe('互补')
    expect(relationDefinitionFor('dependency').label).toBe('依赖')
    expect(relationDefinitionFor('duplicate').label).toBe('重复')
    expect(relationDefinitionFor('independent').label).toBe('独立')
    expect(relationDefinitionFor('workflow').kind).toBe('complementary')

    const single = scenarioGuideFor('Seedance 提示词 · 通用型', 1)
    expect(single.relationKind).toBe('single')
    expect(single.relationLabel).toBe('单项')
    expect(single.relationNote).toBe('当前只有一个对象，不建立对象间关系。')

    const alternatives = scenarioGuideFor('Seedance 提示词 · 通用型', 2)
    expect(alternatives.relationKind).toBe('alternative')
    expect(alternatives.relationLabel).toBe('备选')
    expect(alternatives.relationLabel).not.toContain('为主')

    const unknown = scenarioGuideFor('未配置场景', 2)
    expect(unknown.relationKind).toBe('unknown')
    expect(unknown.relationLabel).toBe('关系未判定')
  })

  it('does not turn a shared scene into an observed relationship', () => {
    expect(observedProviderLinks([videoSkill, videoPlugin])).toEqual([])
    expect(scenarioGuideFor('视频、动画与分镜', 2).relationLabel).toBe('混合关系')
  })

  it('resolves only an exact plugin-name projection as an observed provider link', () => {
    const wrongMcp = {
      ...videoMcp,
      asset_id: 'mcp:other',
      source: { observation: { plugin_name: 'other-plugin' } },
    }
    const links = observedProviderLinks([videoSkill, videoPlugin, videoMcp, wrongMcp])

    expect(links).toHaveLength(1)
    expect(links[0].from.asset_id).toBe(videoPlugin.asset_id)
    expect(links[0].to.asset_id).toBe(videoMcp.asset_id)
  })

  it('keeps related tool counts separate from the current Skill result', () => {
    const context = scenarioContextFor(
      [videoSkill],
      [videoSkill, videoPlugin, videoMcp],
      '视频、动画与分镜',
    )

    expect(context.currentCounts.skill).toBe(1)
    expect(context.relatedCounts.plugin).toBe(1)
    expect(context.relatedCounts.mcp).toBe(1)
    expect(context.related.map(({ relation }) => relation)).toEqual(['observed', 'observed'])
    expect(context.providerLinks).toHaveLength(1)
  })
})
