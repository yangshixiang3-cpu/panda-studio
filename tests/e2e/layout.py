from pathlib import Path
import re

web = Path(__file__).resolve().parents[2] / 'web'
p = web / 'index.html'
s = p.read_text(encoding='utf-8')
def replace(start, end, replacement=''):
    global s
    a = s.index(start)
    b = s.index(end, a)
    s = s[:a] + replacement + s[b:]

replace('  <!-- 动效开关', '  <script type="importmap">')
replace('  <div class="scene-backdrop"', '  <section id="start-page">')
replace('<span class="brand-copy">', '    <main class="start-main">', '''<span>白底工作台</span></a><div class="header-tools"><div data-runner></div><a href="#refs" class="refs-entry">参考库</a></div></header>
''')
replace('      <div class="hero">', '      <section class="folder-panel">')
s = re.sub(r'选产品文件夹</h2><p class="hint">.*?</p>', '产品文件夹</h2>', s)
s = re.sub(r'<svg class="drop-border".*?</svg>', '', s)
s = s.replace('拖入文件夹需要用桌面窗口打开（双击 白底工作台）','浏览器里不能拖文件夹，请用上面的按钮')
s = s.replace('<div><span class="eyebrow">当前产品</span><h2 id="product-name"></h2></div>','<h2 id="product-name"></h2>')
s = s.replace('模型文件 <span id="model-count"></span></h3><p class="hint">选择出图用的 Max 模型；FBX 用于网页预览。</p>','模型</h3>')
s = s.replace('参考图 <span id="ref-count"></span>','参考图').replace('sku 文件夹</h3>','sku</h3>')
s = s.replace('<a href="#refs" class="refs-entry">参考库 →</a><span class="hint">画面自动保存，随时回来继续</span>','')
replace('      <footer class="start-footer">', '    </main>')
replace('    <header class="task-header">', '          <svg id="lighting-disk"', '''    <header class="task-header">
      <div class="task-identity"><button id="go-home" class="plain">← 产品</button><strong id="task-product"></strong><span id="save-state" data-state="saved" role="status" title="已保存"></span></div>
      <div class="task-steps" aria-label="出图步骤"><button id="smallshot">出小样</button><span aria-hidden="true">→</span><button id="final">渲染</button><span aria-hidden="true">→</span><button id="delivery">入库</button></div>
      <div class="header-actions"><button id="calibration-history">AI 校准</button><span id="accepted-dot" hidden></span><button id="overview">总览</button><div data-runner></div></div>
    </header>
    <main class="workspace">
      <aside class="checklist"><div class="sidebar-heading"><h2>白底清单</h2><span id="item-count"></span></div><div id="items"></div><button id="add-item" class="add-item">＋ 加一张</button></aside>
      <section class="work-area">
        <div class="canvas-heading"><strong id="selected-label"></strong><span id="output-size"></span></div>
        <div id="model-message" class="notice" hidden></div>
        <div class="comparison">
          <div id="frame-zone"><div id="frame"><canvas id="canvas" aria-label="产品画面"></canvas><img id="overlay" alt="叠加参考图" hidden><div class="margin-guide"></div><span id="outside-note" hidden>产品超出画面</span><span id="loading-model" class="is-busy">正在准备预览模型</span></div></div>
          <div id="side-reference" hidden><img alt="并排参考图"></div>
          <div class="canvas-tools">
            <details class="canvas-help"><summary aria-label="画面操作提示">?</summary><div class="popover gesture-hint"><div>左键拖动：转动与俯视</div><div>滚轮：远近</div><div>右键或 Shift＋左键：微移</div><div>双击：自动取景</div></div></details>
            <details id="preview-menu"><summary aria-label="预览模型菜单">⋯</summary><div class="popover"><button id="choose-fbx">改用模型文件夹里的 FBX</button><button id="rotate-model" hidden>预览模型方向不对：旋转 90°</button><small id="model-dimensions"></small></div></details>
          </div>
        </div>
      </section>
      <aside class="adjustments">
        <div class="tabs" role="tablist" aria-label="画面设置">
          <button role="tab" id="tab-pose" aria-controls="pose-panel" data-tab="pose">画面</button>
          <button role="tab" id="tab-lighting" aria-controls="lighting-panel" data-tab="lighting">打光</button>
          <button role="tab" id="tab-references" aria-controls="references-panel" data-tab="references">参考图</button>
        </div>
        <section id="pose-panel" role="tabpanel" aria-labelledby="tab-pose">
          <div id="pose-controls"></div><div class="preset-grid" id="presets"></div>
          <div class="format-switch" aria-label="输出画幅"><button data-orientation="portrait">竖版</button><button data-orientation="landscape">横版</button></div>
          <details id="advanced"><summary>高级</summary><div id="advanced-controls"></div></details>
          <section id="parts-panel" hidden><h2>开合（仅预览）</h2><div id="parts-controls"></div><button id="close-parts">全部合上</button></section>
        </section>
        <section id="lighting-panel" role="tabpanel" aria-labelledby="tab-lighting" hidden>
          <label class="lighting-switch"><input type="checkbox" id="lighting-single">单张单独打光</label>
''')
s = s.replace('          <div id="lighting-angle"></div>\n','')
s = s.replace('<small class="lighting-ends"><span>柔和</span><span>强烈</span></small>','')
s = s.replace('<small id="lighting-save" role="status"></small>','')
replace('        <section id="parts-panel" hidden><h2><span', '  <dialog id="dialog">', '''        <section id="references-panel" role="tabpanel" aria-labelledby="tab-references" hidden>
          <button id="add-reference">添加参考图…</button><input id="reference-input" type="file" accept=".jpg,.jpeg,.png,.webp" multiple hidden>
          <div id="reference-list"></div>
          <div class="reference-modes"><label><input type="checkbox" id="overlay-on">叠在画面上</label><label><input type="checkbox" id="side-on">并排对照</label></div>
          <label class="control"><span>透明度 <output id="opacity-value">35%</output></span><input id="opacity" type="range" min="0" max="80" value="35"></label>
        </section>
      </aside>
    </main>
  </section>
  <section id="refs-page" hidden>
    <header class="start-header"><a href="#">← 返回首页</a></header>
    <main class="start-main"><div class="library-heading"><h1>参考库</h1><p class="hint">认可的图片会作为 AI 校准的颜色依据</p></div><div id="refs-groups"></div></main>
  </section>
''')
s = s.replace('  <script type="module" src="/fx.js"></script>\n','')
p.write_text(s, encoding='utf-8')
