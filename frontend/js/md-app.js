/* md-app.js — mdkit 的业务装配层(非库,属应用)。
   职责:用领域插件创建配置好的渲染器,并挂出兼容垫片
   window.renderMarkdown(md, baseDir, source)——保持旧签名,reader.js/chat.js/app.js 无感切换。
   同时装配编辑器 createEditor,把预览回调接到同一渲染管线(编辑预览与阅读态一致)。
   插件定义见 ./md-plugins.js(业务知识全部住那里,mdkit/ 保持干净)。 */
import { createRenderer, createEditor } from './mdkit/index.js';
import { appPlugins } from './md-plugins.js';

const renderer = createRenderer({ plugins: appPlugins });

// 编辑器工厂:预览用应用渲染器(带 baseDir/source,图片重写/公式与阅读态同源)。
// 调用方传 { element, initialValue, onSave, autosaveId, baseDir, source, minHeight }。
function createAppEditor(opts = {}) {
  const { baseDir = null, source, ...rest } = opts;
  return createEditor({
    ...rest,
    previewRender: (text) => renderer.render(text, { baseDir, source }),
  });
}

// 对外:库本体 + 配置好的应用渲染器/编辑器 + 旧签名兼容垫片(供 reader/chat/app 全局调用)
window.MDKit = { createRenderer, createEditor: createAppEditor, renderer };
window.renderMarkdown = function (md, baseDir, source) {
  return renderer.render(md, { baseDir, source });
};
