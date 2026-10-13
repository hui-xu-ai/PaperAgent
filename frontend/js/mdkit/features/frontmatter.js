/* mdkit/features/frontmatter.js — 文件开头 `---` YAML 子集 → Obsidian 风格属性面板。
   设计约束：frontmatter 在 marked.parse 之前从原文抽出，面板 HTML 拼在正文最前面，
   因此不干扰后续公式占位符/图片重写/标题锚点。解析失败即退回旧行为（整块剥离、不渲染）。 */
import { escapeHtml } from '../core/escape.js';

// 切分文件开头的 `---` 块 → { raw: 元数据行[], body: 正文 }；非 frontmatter 返回 null
export function splitFrontmatter(md) {
  const lines = String(md).split(/\r?\n/);
  if (!lines.length || lines[0].trim() !== '---') return null;
  let end = -1;
  for (let i = 1; i < lines.length; i++) {
    const t = lines[i].trim();
    if (t === '---' || t === '...') { end = i; break; }
  }
  if (end < 0) return null;
  return { raw: lines.slice(1, end), body: lines.slice(end + 1).join('\n') };
}

function fmUnquote(v) {
  const s = String(v).trim();
  if (s.length >= 2) {
    const q = s[0];
    if ((q === '"' || q === "'") && s[s.length - 1] === q) {
      let inner = s.slice(1, -1);
      if (q === '"') inner = inner.replace(/\\"/g, '"').replace(/\\n/g, '\n').replace(/\\\\/g, '\\');
      else inner = inner.replace(/''/g, "'");
      return inner;
    }
  }
  return s;
}

// 单字段值解析：标量 / 流式数组 / 块状列表 / 块标量 / 空值
function fmParseValue(rawVal, rawLines, idx) {
  const val = String(rawVal).trim();
  if (!val) {
    const items = [];
    let k = idx + 1;
    while (k < rawLines.length) {
      const li = /^\s*[-*]\s+(.*)$/.exec(rawLines[k]);
      if (!li) break;
      items.push(fmUnquote(li[1].trim()));
      k++;
    }
    if (items.length) return { values: items, isList: true, consumed: k - idx - 1 };
    return { values: [], isList: false, consumed: 0 };
  }
  if (val === '|' || val === '>') {
    const buf = [];
    let k = idx + 1;
    while (k < rawLines.length && (!rawLines[k].trim() || /^\s+\S/.test(rawLines[k]))) {
      buf.push(rawLines[k].replace(/^\s{1,4}/, ''));
      k++;
    }
    while (buf.length && !buf[buf.length - 1].trim()) buf.pop();
    return { values: [buf.join(' ').trim()], isList: false, consumed: k - idx - 1 };
  }
  if (val.startsWith('[') && val.endsWith(']')) {
    const inner = val.slice(1, -1).trim();
    let arr = null;
    try { arr = JSON.parse(val); } catch (e) { /* 非严格 JSON：tags: [paper, xxx] 裸值数组 */ }
    if (!Array.isArray(arr)) arr = inner ? inner.split(',').map(s => fmUnquote(s.trim())) : [];
    return { values: arr.map(x => (x === null || x === undefined) ? '' : String(x)).filter(s => s !== ''),
             isList: true, consumed: 0 };
  }
  return { values: [fmUnquote(val)], isList: false, consumed: 0 };
}

export function parseFrontmatterEntries(rawLines) {
  const out = [];
  for (let i = 0; i < rawLines.length; i++) {
    const line = rawLines[i];
    if (!line.trim() || /^\s*#/.test(line)) continue;
    if (/^\s*[-*]\s+/.test(line)) continue;
    const m = /^([^:\s][^:]{0,63}?)\s*:\s*(.*)$/.exec(line);
    if (!m) continue;
    const { values, isList, consumed } = fmParseValue(m[2], rawLines, i);
    i += consumed;
    const clean = values.map(v => String(v).trim()).filter(v => v !== '');
    if (!clean.length) continue;
    out.push({ key: fmUnquote(m[1]), values: clean, isList });
  }
  return out;
}

const FM_DOI_RE = /^10\.\d{4,9}\/\S+$/;

function fmRenderValue(v) {
  const s = String(v);
  if (FM_DOI_RE.test(s)) {
    return `<a class="prop-link" href="https://doi.org/${escapeHtml(s)}" target="_blank" rel="noopener">${escapeHtml(s)}</a>`;
  }
  if (/^https?:\/\/[^\s]+$/i.test(s)) {
    return `<a class="prop-link" href="${escapeHtml(s)}" target="_blank" rel="noopener">${escapeHtml(s)}</a>`;
  }
  return escapeHtml(s);
}

function fmValueLines(v) {
  // 标量字段原样一行：`通讯作者: "A；B"` 在 YAML 里是一个字符串（分号只是人读分隔）。
  // 曾按分号拆行 ⇒ textContent 拼接粘连成 "AB"（实测），故不拆、保留原分隔符。
  return [String(v)];
}

export function renderPropPanel(entries) {
  if (!entries || !entries.length) return '';
  const rows = entries.map(e => {
    let valHtml;
    if (e.isList) {
      valHtml = '<span class="prop-chips">' + e.values.map(v =>
        `<span class="prop-chip">${fmRenderValue(v)}</span>`).join('') + '</span>';
    } else {
      const lines = e.values.reduce((acc, v) => acc.concat(fmValueLines(v)), []);
      valHtml = lines.map(l => `<span class="prop-line">${fmRenderValue(l)}</span>`).join('');
    }
    return `<div class="prop-row"><div class="prop-key" title="${escapeHtml(e.key)}">${escapeHtml(e.key)}</div>`
         + `<div class="prop-val">${valHtml}</div></div>`;
  }).join('');
  return `<div class="md-props"><div class="props-head">属性</div><div class="props-table">${rows}</div></div>`;
}
