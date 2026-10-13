/* md-app.js — mdkit 的业务装配层(非库,属应用)。
   职责:用领域插件创建配置好的渲染器,并挂出兼容垫片
   window.renderMarkdown(md, baseDir, source)——保持旧签名,reader.js/chat.js/app.js 无感切换。
   插件定义见 ./md-plugins.js(业务知识全部住那里,mdkit/ 保持干净)。 */
import { createRenderer } from './mdkit/index.js';
import { appPlugins } from './md-plugins.js';

const renderer = createRenderer({ plugins: appPlugins });

// 对外:库本体 + 配置好的应用渲染器 + 旧签名兼容垫片(供 reader/chat/app 全局调用)
window.MDKit = { createRenderer, renderer };
window.renderMarkdown = function (md, baseDir, source) {
  return renderer.render(md, { baseDir, source });
};
