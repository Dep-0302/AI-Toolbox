import scenarioTaxonomy from '../../registry/scenario_taxonomy.json'
import { itemLabel } from './toolbox'

const TYPE_ORDER = ['skill', 'plugin', 'mcp', 'cli', 'sdk']
const RELATION_ORDER = { observed: 0, same_scene: 1, adjacent: 2 }
const FALLBACK_SCENE = scenarioTaxonomy.default || {}
const RELATION_DEFINITIONS = scenarioTaxonomy.relation_definitions || {}
const LEGACY_RELATION_KIND_ALIASES = {
  workflow: 'complementary',
  collection: 'unknown',
}

const FALLBACK_RELATION_DEFINITIONS = {
  single: {
    label: '单项',
    definition: '当前只有一个对象，不建立对象间关系。',
  },
  alternative: {
    label: '备选',
    definition: '面向同一任务，任意一个都能独立完成，选择一个即可。',
  },
  conflict: {
    label: '冲突',
    definition: '在同一范围同时启用或使用会造成碰撞、覆盖、矛盾或不兼容。',
  },
  complementary: {
    label: '互补',
    definition: '能力不同且不可互相替代，组合使用会增加明确价值。',
  },
  dependency: {
    label: '依赖',
    definition: '存在方向性，一个对象必须依赖另一个对象才能运行或完成。',
  },
  duplicate: {
    label: '重复',
    definition: '实际是同一资产的副本、版本或重复收录。',
  },
  independent: {
    label: '独立',
    definition: '同属一个场景，但彼此没有替代、冲突、互补或依赖关系。',
  },
  mixed: {
    label: '混合关系',
    definition: '组内存在两种及以上已经确认的关系。',
  },
  unknown: {
    label: '关系未判定',
    definition: '当前只完成场景归组，没有足够证据判断对象间关系。',
  },
}

function uniqueAssets(items) {
  const seen = new Set()
  return (Array.isArray(items) ? items : []).filter((item) => {
    const key = item?.asset_id
    if (!key || seen.has(key)) return false
    seen.add(key)
    return true
  })
}

function normalize(value) {
  return String(value || '').trim().toLocaleLowerCase('en-US')
}

function countTypes(items) {
  const counts = Object.fromEntries(TYPE_ORDER.map((type) => [type, 0]))
  for (const item of uniqueAssets(items)) {
    if (Object.hasOwn(counts, item.type)) counts[item.type] += 1
  }
  return counts
}

export function relationDefinitionFor(relationKind) {
  const canonicalKind = LEGACY_RELATION_KIND_ALIASES[relationKind] || relationKind
  const fallback = FALLBACK_RELATION_DEFINITIONS[canonicalKind]
  const configured = RELATION_DEFINITIONS[canonicalKind]
  if (!fallback && !configured) {
    return {
      kind: 'unknown',
      ...FALLBACK_RELATION_DEFINITIONS.unknown,
      ...RELATION_DEFINITIONS.unknown,
    }
  }
  return { kind: canonicalKind, ...fallback, ...configured }
}

export function scenarioGuideFor(subcategory, itemCount) {
  const label = subcategory || '未细分'
  const configured = scenarioTaxonomy.scenes?.[label] || {}
  const configuredKind =
    configured.relation_kind || FALLBACK_SCENE.relation_kind || 'unknown'
  const relation = relationDefinitionFor(itemCount === 1 ? 'single' : configuredKind)
  return {
    label,
    order: configured.order ?? Number.MAX_SAFE_INTEGER,
    description:
      configured.description ||
      (label === '未细分' ? '这些能力尚未进入具体场景分组。' : FALLBACK_SCENE.description),
    relationKind: relation.kind,
    relationLabel: relation.label,
    relationNote:
      itemCount === 1
        ? relation.definition
        : configured.relation_note || FALLBACK_SCENE.relation_note || relation.definition,
    relatedSubcategories: [...new Set(configured.related_subcategories || [])],
    configured: Boolean(scenarioTaxonomy.scenes?.[label]),
  }
}

export function groupAssetsByScenario(items) {
  const groups = new Map()
  for (const item of uniqueAssets(items)) {
    const subcategory = item.curation?.subcategory || '未细分'
    if (!groups.has(subcategory)) {
      groups.set(subcategory, {
        subcategory,
        items: [],
        categories: new Set(),
      })
    }
    const group = groups.get(subcategory)
    group.items.push(item)
    if (item.curation?.category) group.categories.add(item.curation.category)
  }

  return [...groups.values()]
    .map((group) => ({
      ...group,
      categories: [...group.categories].sort((left, right) => left.localeCompare(right, 'zh-CN')),
      guide: scenarioGuideFor(group.subcategory, group.items.length),
    }))
    .sort((left, right) => {
      if (left.subcategory === '未细分') return 1
      if (right.subcategory === '未细分') return -1
      return (
        left.guide.order - right.guide.order ||
        left.subcategory.localeCompare(right.subcategory, 'zh-CN')
      )
    })
}

export function observedProviderLinks(items) {
  const inventory = uniqueAssets(items)
  const pluginsByName = new Map()
  for (const item of inventory) {
    if (item.type !== 'plugin') continue
    const key = normalize(item.source_name)
    if (!pluginsByName.has(key)) pluginsByName.set(key, [])
    pluginsByName.get(key).push(item)
  }

  const links = []
  const seen = new Set()
  for (const target of inventory) {
    if (!['mcp', 'sdk', 'cli'].includes(target.type)) continue
    const pluginName = target.source?.observation?.plugin_name
    if (!pluginName) continue
    for (const plugin of pluginsByName.get(normalize(pluginName)) || []) {
      const key = `${plugin.asset_id}->${target.asset_id}`
      if (seen.has(key)) continue
      seen.add(key)
      links.push({
        from: plugin,
        to: target,
        kind: 'provides',
        evidence: 'observed',
        basis: target.source?.observation?.basis || 'plugin_name_projection',
      })
    }
  }
  return links
}

export function scenarioContextFor(primaryItems, allItems, subcategory) {
  const current = uniqueAssets(primaryItems)
  const inventory = uniqueAssets(allItems)
  const guide = scenarioGuideFor(subcategory, current.length)
  const sceneNames = new Set([subcategory, ...guide.relatedSubcategories])
  const sceneItems = inventory.filter((item) => sceneNames.has(item.curation?.subcategory))
  const sceneIds = new Set(sceneItems.map((item) => item.asset_id))
  const providerLinks = observedProviderLinks(inventory).filter(
    (link) => sceneIds.has(link.from.asset_id) || sceneIds.has(link.to.asset_id),
  )

  const contextItems = uniqueAssets([
    ...sceneItems,
    ...providerLinks.flatMap((link) => [link.from, link.to]),
  ])
  const currentIds = new Set(current.map((item) => item.asset_id))
  const currentTypes = new Set(current.map((item) => item.type))
  const providerIds = new Set(providerLinks.flatMap((link) => [link.from.asset_id, link.to.asset_id]))

  const related = contextItems
    .filter((item) => {
      if (currentIds.has(item.asset_id)) return false
      if (!currentTypes.has(item.type)) return true
      return item.curation?.subcategory !== subcategory
    })
    .map((item) => {
      let relation = 'adjacent'
      if (providerIds.has(item.asset_id)) relation = 'observed'
      else if (item.curation?.subcategory === subcategory) relation = 'same_scene'
      return { item, relation }
    })
    .sort((left, right) => {
      const relationOrder = RELATION_ORDER[left.relation] - RELATION_ORDER[right.relation]
      const typeOrder = TYPE_ORDER.indexOf(left.item.type) - TYPE_ORDER.indexOf(right.item.type)
      return relationOrder || typeOrder || itemLabel(left.item).localeCompare(itemLabel(right.item), 'zh-CN')
    })

  return {
    guide,
    currentCounts: countTypes(current),
    relatedCounts: countTypes(related.map((entry) => entry.item)),
    related,
    providerLinks,
  }
}

export const SCENARIO_RELATION_LABELS = {
  observed: '来源关联',
  same_scene: '同场景入口',
  adjacent: '相邻场景',
}
