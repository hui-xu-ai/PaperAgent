/* mdkit/__tests__/guard.test.mjs — mdkit 库守卫(零依赖,node:test)。
   运行:  node --test frontend/js/mdkit/__tests__/guard.test.mjs
   重生成 golden:  GOLDEN_UPDATE=1 node --test .../guard.test.mjs
   两道守卫:
     1. 库边界:mdkit/** 禁止引用任何业务符号(强制"通用渲染"与"业务特化"严格分离)
     2. 渲染快照:代表性用例输出与 golden 逐字节一致(防未来改库导致渲染漂移) */
import { test } from 'node:test';
import assert from 'node:assert';
import { createRequire } from 'module';
import { readFileSync, writeFileSync, readdirSync, statSync, existsSync } from 'fs';
import path from 'path';

const require = createRequire(import.meta.url);
const REPO = path.resolve(import.meta.dirname, '../../../..');
const MDKIT = path.join(REPO, 'frontend/js/mdkit');
const GOLDEN = path.join(MDKIT, '__tests__/golden/render-golden.json');

/* ── 守卫一:库边界 ── */
// mdkit 是纯通用渲染库,以下业务符号一旦出现在库内即视为耦合回归。
const FORBIDDEN = [
  'readerState', 'appendEvent', 'loadReaderFile', 'floatWins', 'state.papers',
  '/api/kb', '/api/library', '/api/', 'window.api',
];
function* walkJs(dir) {
  for (const e of readdirSync(dir)) {
    const p = path.join(dir, e);
    if (statSync(p).isDirectory()) { if (e !== '__tests__') yield* walkJs(p); }
    else if (e.endsWith('.js')) yield p;
  }
}
test('库边界:mdkit/** 不引用业务符号', () => {
  const hits = [];
  for (const f of walkJs(MDKIT)) {
    const src = readFileSync(f, 'utf8');
    for (const bad of FORBIDDEN) {
      if (src.includes(bad)) hits.push(`${path.relative(REPO, f)} → ${bad}`);
    }
  }
  assert.deepStrictEqual(hits, [], 'mdkit 库出现业务耦合:\n' + hits.join('\n'));
});

/* ── 守卫二:渲染快照 ── */
globalThis.marked = require(path.join(REPO, 'frontend/vendor/marked.min.js'));
globalThis.katex = require(path.join(REPO, 'frontend/vendor/katex/katex.min.js'));
globalThis.hljs = require(path.join(REPO, 'frontend/vendor/highlight.min.js'));

const { createRenderer } = await import('../index.js');
const { appPlugins } = await import('../../md-plugins.js');
const renderer = createRenderer({ plugins: appPlugins });
const render = (md, baseDir, source) => renderer.render(md, { baseDir, source });

// 覆盖每个 feature / 插件分支的代表性用例
const CASES = {
  'frontmatter+doi|kb': [`---\ntitle: "T"\ndoi: 10.1002/adma.202407106\ntags: [a, b]\n通讯作者: "A；B"\n---\n\n# 标题\n正文`, 'kb/10.1002_adma', 'kb'],
  'callout+math|kb': [`> [!warning] 注意\n> 内容 **粗** 与 $x^2$\n>\n> - 列表\n\n段落`, 'kb/x', 'kb'],
  'math-block-inline|kb': [`块 $$E=mc^2 \\tag{1}$$ 行内 $\\alpha_1$ 与 O$_3$H`, 'kb/x', 'kb'],
  'pseudo-formula|kb': [`伪 $<sup>[1]</sup>$ 和 $<sup>[2–4]</sup>$ 混真 $a^2$`, 'kb/x', 'kb'],
  'citation-superscript|kb': [`裸上标^{[34]} 与 _{1},正文 ^{文字} 不碰,脚注^[注]不碰`, 'kb/x', 'kb'],
  'code-fence|kb': ['```python\ndef f(x):\n    return x**2  # $notmath$\n```\n\n行内 `c $x$`', 'kb/x', 'kb'],
  'escaped-entities|kb': [`转义 &lt;del&gt;删&lt;/del&gt; 与 &lt;br&gt; &lt;sup&gt;上&lt;/sup&gt;`, 'kb/x', 'kb'],
  'mineru-image-comment|lib': [`占位\n<!-- image -->\n![图](images/foo.png)\n<!-- image-2 -->`, 'library/x', 'lib'],
  'image-rewrite-lib|lib': [`![a](images/x.png) ./images/y.png ![b](https://e.com/z.png)`, 'library/x', 'lib'],
  'image-nobase|none': [`![a](images/x.png)`, null, undefined],
  'wikilink|kb': [`[[笔记]] [[笔记|别名]] [[dir/file.md]]`, 'kb/x', 'kb'],
  'table-gfm|kb': [`| A | B |\n|---|---|\n| 1 | $x$ |\n| 2 | **y** |`, 'kb/x', 'kb'],
  'bom|kb': [`\uFEFF---\nk: v\n---\n\n带BOM正文`, 'kb/x', 'kb'],
  'empty|kb': [``, 'kb/x', 'kb'],
};

function computeGoldens() {
  const out = {};
  for (const [name, [md, baseDir, source]] of Object.entries(CASES)) out[name] = render(md, baseDir, source);
  return out;
}

test('渲染快照:与 golden 逐字节一致', () => {
  const actual = computeGoldens();
  if (process.env.GOLDEN_UPDATE === '1' || !existsSync(GOLDEN)) {
    writeFileSync(GOLDEN, JSON.stringify(actual, null, 2), 'utf8');
    console.log('  golden 已写入 ' + path.relative(REPO, GOLDEN));
    return;
  }
  const expected = JSON.parse(readFileSync(GOLDEN, 'utf8'));
  assert.deepStrictEqual(Object.keys(actual), Object.keys(expected), '用例集与 golden 键不一致(改了 CASES 请 GOLDEN_UPDATE=1 重生成)');
  for (const k of Object.keys(expected)) {
    assert.strictEqual(actual[k], expected[k], `渲染漂移: ${k}`);
  }
});
