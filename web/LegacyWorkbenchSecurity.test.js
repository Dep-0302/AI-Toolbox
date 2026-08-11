import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { JSDOM, VirtualConsole } from 'jsdom'
import { afterEach, describe, expect, it } from 'vitest'

const HERE = path.dirname(fileURLToPath(import.meta.url))
const ROOT = path.resolve(HERE, '..')
const WORKBENCH_HTML = fs.readFileSync(path.join(ROOT, 'collection-workbench', 'index.html'), 'utf8')
  .replace(/<script src="(?:data|decisions)\.js"><\/script>/g, '')

function workbenchItem(uid, name, src) {
  return {
    uid,
    inner: `${name}/SKILL.md`,
    copy_srcs: [src],
    zh_name: `${name} 中文名`,
    zh_sum: `${name} 的测试摘要`,
    zh_state: 'reviewed',
    name,
    group: '测试分组',
    group_by: 'name_any',
    desc: `${name} description`,
    desc_full: `${name} description`,
    desc_flags: [],
    src,
    copies: 1,
    platform: ['通用大模型'],
    inputs: ['一句话想法'],
    outputs: ['分析报告'],
    chars: 128,
    shape: {
      '训练与测试': 0,
      '知识库': 0,
      '范例库': 0,
      '模板': 0,
      '参考资料': 0,
      '素材': 0,
      '脚本': 0,
      total: 1,
    },
    version: '1.0.0',
    installed: { hosts: [], as_name: '' },
    zh: true,
    multi: false,
    tags: [],
  }
}

const WORKBENCH_DATA = {
  schema_version: 3,
  generated_at: '2026-08-11T12:00:00Z',
  source_dir: '/home/example/AI-Agent-Skills',
  stats: {
    skills: 2,
    groups: 1,
    ungrouped: 0,
    installed: 0,
    competing: 2,
    loose_docs: 0,
    repos: 0,
  },
  hosts: ['codex', 'claude', 'hermes', 'workbuddy'],
  guessed_fields: ['platform', 'inputs', 'outputs'],
  groups: [{ name: '测试分组', desc: '测试夹具', competing: true, count: 2, order: 0 }],
  facets: { platform: [], inputs: [], outputs: [], zh_state: [], has: [] },
  items: [
    workbenchItem('11111111111111111111111111111111', 'fixture-one', 'skills/fixture-one.zip'),
    workbenchItem('22222222222222222222222222222222', 'fixture-two', 'skills/fixture-two.zip'),
  ],
  loose_docs: [],
  repos: [],
}

const openPages = []

function openWorkbench(url = 'http://127.0.0.1:4791/collection-workbench/index.html') {
  const virtualConsole = new VirtualConsole()
  const dom = new JSDOM(WORKBENCH_HTML, {
    runScripts: 'dangerously',
    url,
    virtualConsole,
    beforeParse(window) {
      window.__WORKBENCH_DATA__ = JSON.parse(JSON.stringify(WORKBENCH_DATA))
    },
  })
  openPages.push(dom)
  return dom
}

function emptyPayload(overrides = {}) {
  return {
    schema_version: 1,
    updated_at: '2026-08-11T12:00:00Z',
    decisions: {},
    overrides: {},
    ...overrides,
  }
}

function decision(item, overrides = {}) {
  return {
    verdict: 'keep',
    merge_into: null,
    note: '',
    decided_at: '2026-08-11T12:00:00Z',
    anchor: { name: item.name, src: item.src },
    ...overrides,
  }
}

function inPageRealm(window, value) {
  return window.JSON.parse(JSON.stringify(value))
}

afterEach(() => {
  while (openPages.length) openPages.pop().window.close()
})

describe('legacy collection workbench security boundary', () => {
  it('keeps the file:// manual entry functional', () => {
    const { window } = openWorkbench('file:///tmp/AI-Toolbox/collection-workbench/index.html')

    expect(window.document.querySelector('#top')).not.toBeNull()
    expect(window.document.querySelectorAll('#groups .c').length).toBe(WORKBENCH_DATA.items.length)
    expect(window.document.querySelector('#hint').textContent).toContain('本地手动入口')
  })

  it('renders imported override text without creating elements or attributes', () => {
    const { window } = openWorkbench()
    const item = WORKBENCH_DATA.items[0]
    const attack = '"><img id="stored-xss" src=x onerror="window.__storedXss=1">'
    const payload = emptyPayload({
      overrides: { [item.uid]: { zh_name: attack } },
    })

    expect(window.mergeFile(inPageRealm(window, payload))).toBe(0)
    window.render()

    const card = window.document.querySelector(`#groups .c[data-uid="${item.uid}"]`)
    expect(window.document.querySelector('#stored-xss')).toBeNull()
    expect(window.__storedXss).toBeUndefined()
    expect(card.querySelector('.nm').textContent).toBe(attack)
    expect(card.getAttribute('aria-label')).toContain(attack)
  })

  it('rejects prototype keys, malformed UIDs, unknown fields, and invalid verdicts', () => {
    const { window } = openWorkbench()
    const item = WORKBENCH_DATA.items[0]
    const proto = window.JSON.parse(`{
      "schema_version":1,
      "updated_at":"2026-08-11T12:00:00Z",
      "decisions":{"__proto__":{}},
      "overrides":{}
    }`)
    const badUid = emptyPayload({ decisions: { 'not-a-uid': decision(item) } })
    const badVerdict = emptyPayload({
      decisions: { [item.uid]: decision(item, { verdict: 'keep\" onmouseover=alert(1)' }) },
    })
    const extraField = emptyPayload({ extra: true })

    expect(() => window.normalizeDecisionPayload(proto)).toThrow(/禁止键/)
    expect(() => window.normalizeDecisionPayload(inPageRealm(window, badUid))).toThrow(/合法 UID/)
    expect(() => window.normalizeDecisionPayload(inPageRealm(window, badVerdict))).toThrow(/verdict/)
    expect(() => window.normalizeDecisionPayload(inPageRealm(window, extraField))).toThrow(/未知字段/)
    expect(window.eval('Object.getPrototypeOf(decisions)')).toBeNull()
    expect(window.eval('Object.getPrototypeOf(overrides)')).toBeNull()
  })

  it('rejects invalid merge targets and cycles before mutating current decisions', () => {
    const { window } = openWorkbench()
    const item = WORKBENCH_DATA.items[0]
    const sameGroup = WORKBENCH_DATA.items.find(
      (candidate) => candidate.uid !== item.uid && candidate.group === item.group,
    )
    expect(sameGroup).toBeTruthy()

    const good = emptyPayload({ decisions: { [item.uid]: decision(item) } })
    window.mergeFile(inPageRealm(window, good))
    expect(window.verdictOf(item.uid)).toBe('keep')

    const unknownTarget = '0'.repeat(32)
    const badTarget = emptyPayload({
      decisions: {
        [item.uid]: decision(item, { verdict: 'merge', merge_into: unknownTarget }),
      },
    })
    expect(() => window.mergeFile(inPageRealm(window, badTarget))).toThrow(/merge_into/)
    expect(window.verdictOf(item.uid)).toBe('keep')

    const cycle = emptyPayload({
      decisions: {
        [item.uid]: decision(item, { verdict: 'merge', merge_into: sameGroup.uid }),
        [sameGroup.uid]: decision(sameGroup, { verdict: 'merge', merge_into: item.uid }),
      },
    })
    expect(() => window.mergeFile(inPageRealm(window, cycle))).toThrow(/成环/)
    expect(window.verdictOf(item.uid)).toBe('keep')
  })

  it('enforces field and file-size budgets', () => {
    const { window } = openWorkbench()
    const item = WORKBENCH_DATA.items[0]
    const longNote = emptyPayload({
      decisions: {
        [item.uid]: decision(item, { note: 'x'.repeat(4001) }),
      },
    })
    expect(() => window.normalizeDecisionPayload(inPageRealm(window, longNote))).toThrow(/4000/)

    const status = window.document.querySelector('#matchinfo')
    window.doImport({ size: 2 * 1024 * 1024 + 1 })
    expect(status.textContent).toContain('2 MB')
  })
})
