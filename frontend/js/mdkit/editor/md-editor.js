/* mdkit/editor/md-editor.js — Markdown 编辑器组件(封装 EasyMDE)。
   纯 UI 组件:不认识任何业务(预览渲染 previewRender、保存 onSave 均由调用方注入),
   只依赖 vendor 全局 EasyMDE(globalThis 惰性读取),缺失即降级为裸 textarea。
   对外 API 见 createEditor 返回对象。 */

// EasyMDE 默认工具栏用 Font Awesome 的 `fa fa-X` 图标,但本项目未引入 FA。
// 这里在 DOM 构建后,把每个按钮按其 fa 类替换为内联 SVG(Feather 风格,与 reader.js
// 现有图标一致),并设中文 title。动作与快捷键仍用 EasyMDE 内置,保证可靠。
const _svg = (inner) =>
  `<svg width="15" height="15" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round">${inner}</svg>`;

const TOOLBAR_ICONS = {
  'fa-bold': ['加粗 (Ctrl+B)', _svg('<path d="M7 5h6a3.5 3.5 0 0 1 0 7H7z"/><path d="M7 12h7a3.5 3.5 0 0 1 0 7H7z"/>')],
  'fa-italic': ['斜体 (Ctrl+I)', _svg('<line x1="18" y1="5" x2="10" y2="5"/><line x1="14" y1="19" x2="6" y2="19"/><line x1="14" y1="5" x2="10" y2="19"/>')],
  'fa-strikethrough': ['删除线', _svg('<line x1="5" y1="12" x2="19" y2="12"/><path d="M8 6h8"/><path d="M8 18h8"/>')],
  'fa-header': ['标题', _svg('<path d="M6 5v14"/><path d="M18 5v14"/><path d="M6 12h12"/>')],
  'fa-code': ['代码块', _svg('<polyline points="16 18 21 12 16 6"/><polyline points="8 6 3 12 8 18"/>')],
  'fa-quote-left': ['引用', _svg('<path d="M9 7H5v5h4c0 2-1 3-3 3.5"/><path d="M19 7h-4v5h4c0 2-1 3-3 3.5"/>')],
  'fa-list-ul': ['无序列表', _svg('<line x1="9" y1="7" x2="20" y2="7"/><line x1="9" y1="12" x2="20" y2="12"/><line x1="9" y1="17" x2="20" y2="17"/><circle cx="5" cy="7" r="1"/><circle cx="5" cy="12" r="1"/><circle cx="5" cy="17" r="1"/>')],
  'fa-list-ol': ['有序列表', _svg('<line x1="10" y1="7" x2="20" y2="7"/><line x1="10" y1="12" x2="20" y2="12"/><line x1="10" y1="17" x2="20" y2="17"/><text x="3" y="9.5" font-size="7" fill="currentColor" stroke="none">1</text><text x="3" y="14.5" font-size="7" fill="currentColor" stroke="none">2</text><text x="3" y="19.5" font-size="7" fill="currentColor" stroke="none">3</text>')],
  'fa-table': ['表格', _svg('<rect x="3" y="5" width="18" height="14" rx="1"/><line x1="3" y1="10" x2="21" y2="10"/><line x1="9" y1="5" x2="9" y2="19"/><line x1="15" y1="5" x2="15" y2="19"/>')],
  'fa-link': ['链接 (Ctrl+K)', _svg('<path d="M10 13a5 5 0 0 0 7 0l2-2a5 5 0 0 0-7-7l-1 1"/><path d="M14 11a5 5 0 0 0-7 0l-2 2a5 5 0 0 0 7 7l1-1"/>')],
  'fa-minus': ['分隔线', _svg('<line x1="3" y1="12" x2="21" y2="12"/>')],
  'fa-eye': ['预览', _svg('<path d="M2 12s3.5-7 10-7 10 7 10 7-3.5 7-10 7-10-7-10-7z"/><circle cx="12" cy="12" r="3"/>')],
  'fa-columns': ['分屏', _svg('<rect x="3" y="4" width="18" height="16" rx="1"/><line x1="12" y1="4" x2="12" y2="20"/>')],
  'fa-arrows-alt': ['全屏', _svg('<path d="M4 9V4h5"/><path d="M20 9V4h-5"/><path d="M4 15v5h5"/><path d="M20 15v5h-5"/>')],
  'fa-undo': ['撤销', _svg('<path d="M9 14 4 9l5-5"/><path d="M4 9h11a5 5 0 0 1 0 10H9"/>')],
  'fa-repeat': ['重做', _svg('<path d="m15 14 5-5-5-5"/><path d="M20 9H9a5 5 0 0 0 0 10h6"/>')],
  'fa-eraser': ['清除格式', _svg('<path d="m5 15 6-6 4 4-6 6H5z"/><path d="m11 9 4-4 4 4-4 4"/><line x1="14" y1="20" x2="21" y2="20"/>')],
};

const TOOLBAR = [
  'bold', 'italic', 'strikethrough', 'heading', '|',
  'code', 'quote', 'unordered-list', 'ordered-list', 'table', '|',
  'link', 'horizontal-rule', '|',
  'preview', 'side-by-side', 'fullscreen',
];

function decorateToolbar(root) {
  root.querySelectorAll('.editor-toolbar button').forEach(btn => {
    const icon = btn.querySelector('i');
    if (!icon) return;
    for (const cls of icon.classList) {
      const hit = TOOLBAR_ICONS[cls];
      if (hit) { btn.innerHTML = hit[1]; btn.title = hit[0]; btn.setAttribute('aria-label', hit[0]); break; }
    }
  });
}

export function createEditor(opts = {}) {
  const {
    element, initialValue = '', previewRender, onSave,
    autosaveId = '', minHeight = '240px', placeholder = '',
  } = opts;
  const EasyMDE = globalThis.EasyMDE;

  // 降级:EasyMDE 未加载 → 裸 textarea(保持旧行为,不至于无法编辑)
  if (!EasyMDE) {
    element.classList.add('reader-edit');
    element.value = initialValue;
    if (placeholder) element.placeholder = placeholder;
    return {
      degraded: true,
      getValue: () => element.value,
      setValue: (v) => { element.value = v; },
      focus: () => element.focus(),
      clearAutosave: () => {},
      destroy: () => {},
    };
  }

  const emde = new EasyMDE({
    element,
    initialValue,
    placeholder,
    // 预览走调用方注入的渲染器(与阅读态同一管线:公式/图片重写/callout 一致)
    previewRender: previewRender ? (text) => previewRender(text) : undefined,
    toolbar: TOOLBAR,
    sideBySideFullscreen: false,   // 分屏在面板内进行,不强制全屏
    forceSync: true,                // 每次改动同步回底层 textarea
    spellChecker: false,
    lineWrapping: true,
    minHeight,
    status: ['lines', 'words', 'cursor'],
    // 每篇文档独立草稿(uniqueId),防丢;成功保存后由调用方 clearAutosave() 清除。
    // 注意 EasyMDE 语义:重开时若存在草稿会**静默恢复**(按文档隔离,不会串台)。
    autosave: autosaveId ? { enabled: true, delay: 1000, uniqueId: autosaveId } : { enabled: false },
  });

  // 工具栏图标 SVG 化 + 标题中文化(在 EasyMDE 构建完 DOM 之后)
  const wrap = element.closest('.EasyMDEContainer') || element.parentElement;
  if (wrap) decorateToolbar(wrap);

  // Ctrl+S / Cmd+S 保存(addKeyMap 追加,不覆盖 EasyMDE 内置快捷键)
  if (onSave) {
    const save = () => { onSave(emde.value()); };
    emde.codemirror.addKeyMap({ 'Ctrl-S': save, 'Cmd-S': save });
  }

  return {
    degraded: false,
    easyMDE: emde,
    getValue: () => emde.value(),
    setValue: (v) => emde.value(v),
    focus: () => emde.codemirror.focus(),
    toPreview: () => emde.togglePreview(),
    toggleSideBySide: () => emde.toggleSideBySide(),
    clearAutosave: () => { try { emde.clearAutosavedValue(); } catch (e) {} },
    destroy: () => { try { emde.toTextArea(); } catch (e) {} try { emde.cleanup(); } catch (e) {} },
  };
}
