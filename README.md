# Panda Studio（原名白底工作台）v1.3

> **公开版说明**
>
> 这是 Panda Studio 的公开源码。为保护公司素材，公开版**不包含**：内置材质参考图、白底模板（`.max`）、
> 测试用的产品模型和产品图。用到这些素材的测试会自动跳过。
>
> - **直接使用**：到 [Releases](../../releases) 下载安装程序。装好后把你自己的白底模板放到安装目录的
>   `template\白底模板.max`，再在「本机设置」里指定 3ds Max，就可以出图；材质参考图在「参考库」里自己添加。
> - **从源码运行**：Windows 10/11、Python 3.13，`python -m pip install -r requirements.txt pywebview Pillow`
>   （中转站 SDK 也可装到 `pylibs`：`python -m pip install --target pylibs -r requirements.txt`），
>   然后 `pythonw desktop.pyw`。打包见 `packaging\build.ps1`（需要 PyInstaller 和 Inno Setup 6）。
> - 下文是开发过程中的完整说明，个别本机路径已换成通用写法。

1.3.0 起软件改名为 Panda Studio，程序文件为 `PandaStudio.exe`；下文仍沿用「白底工作台」「工作台」的叫法，指的是同一个软件。

给不懂 3D 的使用者：选产品文件夹，查看自动起草清单，逐张调整画面，先出小样，再提交渲染，最后确认入库。

这是本地网页与 Python 服务。实际 Max 执行器由另一份工作提供，本次没有修改 `max/` 或 `docs/`。协议以只读的 [docs/SPEC.md](docs/SPEC.md) 为准。

## 安装版（推荐给其他电脑）

- 安装程序：`packaging\output\PandaStudio-安装程序-<版本>.exe`，双击安装。可以选择安装目录，默认装到第一个不是 C 盘的本地硬盘（如 `D:\Panda Studio`；从旧版本升级时沿用原来的目录），按当前用户安装，不需要管理员权限；会建开始菜单快捷方式，桌面快捷方式可选。
- 程序自带 Python 运行环境、桌面窗口组件和中转站 SDK，目标电脑**不需要另装 Python**。需要 Windows 10/11 64 位和 Microsoft Edge WebView2 运行库（Windows 11 自带，安装时没检测到会提示）。
- 数据都在安装目录里：`exchange\`（任务和出图）、`refs\`（参考库）、`config.json`、`ai_settings.json`。升级重装不会覆盖 `config.json` 和参考库；卸载只删程序文件，数据保留。
- 第一次打开：自动从注册表找 3ds Max；在左侧「设置」里确认 3ds Max 程序、白底模板、历史白底目录。顶部出现「先完成本机设置」时点它去设置。
- AI 中转站：在左侧「AI」里重新填地址和 Key（Key 按本机 Windows 账号加密，换电脑要重填）。
- Max 执行器：在工作台里点「启动 Max」，或在已打开的 Max 里运行**安装目录**下的 `max\启动白底执行器.ms`。开发版和安装版的数据目录不同，同一时间只用一个。

### 重新打包

```powershell
& "<工作台目录>\packaging\build.ps1"
```

需要本机 Python 3.13（已装 pywebview、Pillow、`pyinstaller==6.22.3`）和 Inno Setup 6（`D:\Tools\InnoSetup6`）。配置：`packaging\whitebg.spec`（PyInstaller）、`packaging\installer.iss`（安装程序）、`packaging\icon\app.ico`（图标）、`packaging\ChineseSimplified.isl`（安装界面中文，来自 Inno Setup 官方仓库的社区翻译）。版本号改 `installer.iss` 的 `AppVersion` 和 `web\index.html` 的 `app-version`。

## 输出尺寸

正式图分三档，在任务页画布右上角切换：**4K = 3000×4000，6K = 4500×6000，8K = 6000×8000**（竖版；横版宽高对调）。切换后已出的成品要重新渲染。档位越高越吃内存和时间，8K 约是 4K 的 4 倍。

## 启动

本机需要 Python 3（本次使用 3.13）和支持 ES Module、importmap、WebGL 的浏览器。HTTP 服务使用 Python 标准库；AI 校准的颜色统计和参考库缩图使用本机已有 Pillow，不需要 pip 或 npm 安装。three.js 及 FBX 读取依赖已复制到 `web/vendor/three/`，不请求 CDN、字体或其他网络资源。

双击 **启动白底工具.bat**。服务成功监听后自动打开：

```
http://127.0.0.1:8770/
```

命令行也可以在 `whitebg-studio/` 中运行：

```powershell
python -m server.app --open
```

保留命令行窗口。关闭窗口或按 Ctrl+C 停止服务。端口固定为 8770，仅绑定 127.0.0.1；禁止地址复用，Windows 还使用端口独占选项。端口被占时打印中文提示并以非零退出码结束，不会另选端口或误打开别的服务。

## 使用

1. 在首页选择或扫描「产品文件夹」，选中出图用的 `.max` 后点「新建任务」。桌面窗口支持拖入产品文件夹或模型。网页提交预览导出，模型就绪后按实际尺寸初始化草稿。也可在画布右上角「⋯」改用产品内的 FBX、查看模型尺寸，或旋转文件预览。正式渲染需要 `.max`。
2. 左侧「白底清单」用于勾选、排序、删除和增加出图项。「画面」页签可调视角、大小、位置和横竖画幅。「高级」中可调透视，有绑定部件时可开合预览。正式渲染使用合上状态。画布右上角「?」可查操作，双击画布或点「自动取景」可恢复取景，虚线是 10% 留白线。
3. 「打光」页签默认使用模板灯光。开启「光方向」后可用圆盘或预设调整方向，开启「这一张单独打光」可单独设置当前条目。「参考图」支持叠加与并排，透明度可调。当前页签、参考图选择、开关及透明度按任务保存在本机浏览器。
4. 顶栏「出小样 → 渲染 → 入库」突出当前建议步骤，三个按钮均可直接点击。小样和成品处理勾选的条目，「总览」可放大查看成果。点「入库」核对新增与归档清单，再点「确认写入 sku」。同名旧文件归入 `sku/旧版/YYYYMMDD-HHMMSS/`。
5. 需要调色时，打开「AI 校准」，可写一句要求，再点「开始 AI 校准」。校准期间可以继续调整画面，打光和清单结构受服务端校验。查看轮次后，可采用某轮、撤销已采用调整或再调一轮。光向示意图会在出图、开始校准或入库前按当前设置生成。
6. 首页和任务页的「Max」状态片显示连接情况。离线可打开面板启动 Max，或复制执行器脚本路径。离线也能提交小样和渲染，作业等待执行器上线。任务活动或执行器离线时约每 1.8 秒查询一次，任务空闲且执行器在线时约每 8 秒查询一次。

图片可点击放大（右栏参考图点「放大」）；滚轮以光标为中心缩放，双击切换适应窗口与 1:1，放大后左键拖动，左右键翻页，Esc 关闭。

画面停止操作约 0.4 秒后自动保存；切换条目时恢复各自的画面。关闭网页时若仍有未保存内容，浏览器会提示。首页列出已有任务，可随时继续。

## 配置与目录

`config.json` 与 SPEC §2 相同。相对路径以 `whitebg-studio/` 为基准。正式使用前确认本机的白底模板和可选 `.rps` 路径。服务端只把配置写入作业，不改模板或产品模型。

```
server/                   标准库 HTTP 服务、任务及入库逻辑
web/                      原生网页、取景、FBX 与 PZ 预览
web/vendor/three/         实际使用的 three.js 文件及许可证
exchange/tasks/<id>/     task.json、preview、smallshots、final
exchange/jobs/           不可改写的作业文件；执行器状态文件
tools/fake_runner.py     开发假执行器
tools/prepare_demo.py    创建隔离的临时测试产品
tests/fixtures/          指定的绑定 FBX 与参考图副本
tests/screens/           实际浏览器截图与验收缩小图
```

取景按 PF1 毫米保存，`position_mm`、`target_mm` 为权威值。网页与 PF1 同轴；Max 换算为 `[X, -Z, Y]`。竖直视场角默认为 27°，界面对应约 50 mm。自动取景直接计算八角点的约束，求四周 10% 留白的最小距离。

加载执行器 FBX 时按其单位与 `bbox.json` 校核尺寸，超过 1% 时按记录校正并提示。其他来源的 FBX 读其单位信息，支持每次旋转 90°，角度写回任务。材质统一为简化预览材质，不加载外部贴图。

`camera.js` 的取景与角度描述，以及 `model/loaders.js` 的 FBX 分支，复制自指定柜体预演台后适配；`model/rig.js` 是本地副本。网页不 import 原项目文件。PZ 动作按原项目 `RIG.md` 及 `INTERFACES.md` 约定处理。

## 用假执行器跑通全流程

只在开发测试时使用。假执行器读取同一套交换作业，复制指定测试 FBX 并写实测包围盒；渲染用本机已有 Pillow 画带编号、角度、尺寸的占位 JPG 和遮罩，不调用 Max。这些占位图不代表正式渲染质量。网页不依赖假执行器。

先关闭真正的 Max 执行器，避免它领取开发占位模型作业。测试产品的 `.max` 是标明用途的占位文本，不能交给真实 Max 使用。

在 `whitebg-studio/` 中：

```powershell
python tools/prepare_demo.py
python -m server.app --open
```

`prepare_demo.py` 输出新建的临时产品路径，并记录到 `tests/demo-product.json`。它只在 `tests/tmp/` 下创建目录、复制素材，不修改真实产品。

另开一个终端，在同一目录运行：

```powershell
python tools/fake_runner.py
```

在首页粘贴临时产品路径，扫描、新建任务。等待预览就绪，出小样、提交渲染、打开总览，再确认入库。再次确认入库，可在临时产品的 `sku/旧版/` 中看到上一版。用 Ctrl+C 关闭假执行器与服务。

只处理当前队列一次也可以：

```powershell
python tools/fake_runner.py --once
```

## 测试与截图复现

在包含 `whitebg-studio/` 的上一级目录运行：

```powershell
python -m unittest discover -s whitebg-studio/tests -p "test_*.py"
node --test whitebg-studio/tests/web
```

Python 测试使用 `tests/tmp/` 下隔离的临时目录；入库测试绝不写真实产品。Windows 深目录测试使用长路径前缀。所有测试产物保留，不清理删除。核心测试覆盖扫描、起草、原子写入、备份、不可变作业、状态合并、清单重排、开合预览、入库与路径越界。

服务运行时，可用本机已有的 `playwright-core` 与 Edge 复现浏览器验收，不安装依赖：

```powershell
python -B codex-tasks/wb-10b-tmp/environment.py setup
node codex-tasks/wb-10b-tmp/flows.cjs
```

脚本默认用本机已有的 Edge 与 `playwright-core`。也可用 `PLAYWRIGHT_CORE` 指定库目录。验收使用隔离副本、8796 端口、假执行器与假 Codex。报告、日志和流程截图写入 `codex-tasks/wb-10b-tmp/`。

## 本地接口

所有 JSON 写请求使用 `Content-Type: application/json`。本机 Host 和写请求 Origin 会校验；错误信息为中文。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/folders?path=...` | 空路径列盘符，否则列子目录和父目录 |
| `POST /api/scan` | `{product_dir}`，只读扫描 |
| `GET /api/tasks?product_dir=...` | 已有任务列表，可按产品筛选。每项包含 `variant_name`、`variant_group`、`variant_count`，缺省分别为 `null`、`null`、`0` |
| `POST /api/tasks` | `{product_dir, model_max}`，新建并起草一张主图 |
| `GET /api/tasks/<id>` | 返回任务、预览状态、作业状态及任务目录；合并执行器状态 |
| `PUT /api/tasks/<id>` | 保存条目及模型旋转；客户端不能伪造出图状态或改产品路径 |
| `POST /api/tasks/<id>/draft` | `{size_mm:[x,y,z], parts:[{name,type}], initial}`，按实测尺寸起草；`initial:true` 只更新未修改的初始清单 |
| `POST /api/tasks/<id>/preview` | `{source:"export"}` 或 `{source:"file",path}` |
| `POST /api/tasks/<id>/variants` | `{variants:[{id,name,nodes,bbox_mm:{min,max}}], active}`，保存识别结果并同步同组任务，返回 `{task,siblings,orphans}` |
| `POST /api/tasks/<id>/switch-variant` | `{variant_id}`，返回已有任务或创建复用整场景预览的兄弟任务，响应为 `{task_id}` |
| `POST /api/tasks/<id>/variant-name` | `{variant_id,name}`，同步同组任务中的款式名，返回 `{task}` |
| `POST /api/tasks/<id>/jobs` | `{mode:"smallshot"}` 或 `{mode:"final"}`，提交启用条目 |
| `POST /api/tasks/<id>/delivery-plan` | `{}`，预检并返回明确清单和确认凭据 |
| `POST /api/tasks/<id>/deliver` | `{token}`，仅执行当前明确清单 |

任务可以保存 `variants`（整场景款式列表）、`variant`（当前柜子，单柜为 `null`）和 `variant_group`（兄弟任务组号）。每个款式有编号 `id`、入库用名称 `name`、Max 节点名 `nodes` 和 PF1 毫米包围盒 `bbox_mm`。单柜沿用原有入库文件名，多柜会在入库时给成品、遮罩和光向参考加款式名前缀，并使用 `白底清单_<款式名>.json`。
| `GET /api/file?path=...&task_id=...` | 读取允许范围内文件 |

文件接口仅允许网页目录、交换目录，以及指定任务对应产品本次扫描列出的模型文件、「参考图」「sku」和扫描识别的深层参考图片。模型旁边的其他文件不会因放在「模型」中而获准读取。路径先解析再核对范围，目录穿越、其他产品、系统文件返回 403。产品子目录的符号链接和目录联接不接受；入库旧版目录指向 sku 外部时拒绝。首页未创建任务前只列参考图文件名；已有该产品任务后才通过任务授权显示缩略图。

## 桌面窗口用法

双击 `白底工作台.lnk`，或 `打开白底工作台.bat`，打开标题为「白底工作台」的 1600×1000 桌面窗口，可以缩放和最大化。本机使用已安装的 Python 3.13、pywebview 6.2.1 与 Edge WebView2，不需要安装其他包或联网。快捷方式指向本机的 `pythonw.exe` 和 `desktop.pyw`，起始目录为 `whitebg-studio/`；移动程序或 Python 后需重新生成快捷方式。批处理通过 `pythonw.exe` 启动，不保留命令行窗口。

- 在窗口任意位置拖入产品文件夹，或 `.max` / `.fbx` 模型。拖过窗口时显示「松手即可打开这个产品」。也可点首页的「选择产品文件夹…」「选择模型文件…」使用原生对话框。
- 拖入名为「模型」「参考图」「sku」「视角图」的文件夹时选用上一级。拖入模型时，以路径中最近的「模型」上一级为产品目录；没有「模型」则以模型所在目录为产品目录。扫描后自动选中拖入的模型；选中 FBX 后新建任务会直接使用该 FBX 预览，渲染仍需要 `.max`。
- 一次拖入多项只打开第一个文件夹或模型；所有图片按参考图处理，其余项目会提示忽略。无法获取完整路径时提示使用「选择产品文件夹」按钮。
- 任务页拖入 jpg、jpeg、png、webp 图片会加入当前任务。在首页扫描产品后拖入图片，会暂存并在新建任务时一起加入；切换产品会清空该暂存列表。图片与产品同时拖到任务页时，图片先加入原当前任务，再打开拖入的产品。
- 首页和任务页均可点「添加参考图…」。浏览器模式使用文件内容上传；桌面拖入按完整路径复制。图片存放在 `exchange/tasks/<id>/references/`，同名自动改名，并记录到 `task.json` 的 `reference_images`。单张最多 50 MB，扩展名与图片内容须相符，源文件保持不变。
- 首页「sku 文件夹」会显示是否存在；不存在时只提示入库会自动新建，已有时显示文件数及同名旧文件的归档位置。

桌面入口在后台线程复用 `server` 包，仅监听 `127.0.0.1:8770`。`GET /api/health` 返回白底工作台标识；端口已由本工作台服务使用时直接复用，其他程序占用则弹窗后退出。关闭窗口只停止本次桌面进程启动的服务；复用的服务保持运行。早于本次改动、尚无健康接口的旧服务需要先关闭，再启动新版。

原来的 `启动白底工具.bat` 保留为浏览器模式。浏览器模式保留文件夹浏览器，隐藏原生选择按钮，并提示「拖入文件夹需要用桌面窗口打开（双击 白底工作台）」。

新增接口：`POST /api/tasks/<id>/references` 接收 JSON `{paths:[完整图片路径]}`；`POST /api/tasks/<id>/references/upload?name=文件名` 接收单张图片的原始字节。两者均校验本机 Origin、图片格式与大小，并返回更新后的 `{task}`。`POST /api/tasks` 可用 `model_file` 指定扫描列出的 Max 或 FBX；扫描结果新增 `sku_exists`。

本次验收仅在 `tests/tmp/` 的隔离交换目录中创建任务，不向正式 `exchange/jobs/` 写入测试作业。

新增输入流程的安全验收命令，在包含 `whitebg-studio/` 的上一级目录运行：

```powershell
python whitebg-studio/tests/ui_acceptance.py
python whitebg-studio/tests/desktop_smoke.py
python -m py_compile whitebg-studio/desktop.pyw
```

`ui_acceptance.py` 在临时端口启动隔离服务，使用本机已有 Edge 与 `playwright-core` 检查扫描、文件权限、参考图上传及网页路径接入，报告保存为 `tests/input-acceptance.json`。它核对指定真实产品与正式作业目录在验收前后未变化，不代替原生拖放验证。

`desktop_smoke.py` 使用临时交换目录启动真实桌面入口，成功时截取实际窗口到 `tests/screens/03-desktop.png` 后关闭；失败时只记录 `tests/desktop-report.json`，不生成替代截图。为防止写入其他服务，端口已被占用时该验收脚本不复用服务。原生拖放、原生选择对话框和最大化仍需在实际桌面操作验收。本次沙箱实测 WebView2 报告 `Main window failed to start`，未生成桌面截图。

运行中，服务的一般写入只发生在交换目录。只有确认入库才写产品 `sku/`。JSON 先写同目录 `.tmp`，刷新落盘后原子替换；每次覆盖已有文件先复制为 `.bak`，已有备份保留并使用新的备份名。已发布的作业文件不修改，状态文件由执行器写，服务只读。开发工具创建的文件及测试记录只在 `tests/` 内。

## 待确定的规格细节

- SPEC §8 允许成图完成但遮罩缺失，§9 则要求成图与遮罩一起入库。当前采用预检阻止缺遮罩的条目入库，保留已渲染成图，页面说明缺少的文件。需要确定后续是否允许「只入成图」；当前没有默许跳过遮罩。
- SPEC §10 没有规定判定横竖时使用哪个初始透视距离。当前按视角方向下八角点的正交投影宽高比判断，严格大于 1.3 才用横版，再按实际透视及画幅求最小取景距离。若两端需要同样的透视初值，应由规格明确。

本次联调使用假执行器；尚未与另一对话的真实 Max 执行器联调。真实 Corona 材质、模板与遮罩效果由 Max 端验证。

## AI 校准

任务页「AI 校准」弹层中的「开始 AI 校准」启动服务端后台流程，依次完成小样、颜色检查、AI 分析和后续轮次。关闭网页后服务继续处理。AI 优先对照「材质参考」中本任务提供的图片，其次参考本产品已有白底和参考库。主图参考用于光向和明暗。调整仅应用于本次任务的材质、灯光与环境反射。

点「添加并标注…」或把图片拖入弹层，在图上框选场景材质、参考库材质或本任务的新材质。可勾选「也加入参考库」复制图片和参考库材质的框。运行中添加、编辑或删除任务参考会在下一轮生效，每轮卡片显示实际用了几张。[参考图标注与清理](docs/参考图标注与清理.md)

默认使用本机 Codex。使用 OpenAI 兼容中转站时，从首页顶栏「AI 设置」或校准弹层的「设置」进入。

中转站功能需要 Python 3.13 x64，使用 `pylibs/` 内置的 OpenAI 官方 Python SDK。复制工作台时，请将整个 `pylibs/` 文件夹一起复制。

1. 选择「API 中转站」，填入中转站提供的接口地址和 API Key，点「保存」。地址如包含 `/v1`，请一并填写。
2. 再打开设置并点「拉取模型」。从列表搜索并选择模型，或手动填写模型 ID。`gpt-6.2-sol` 在中转站提供时列在首位。
3. 选择推理强度，点「测试连接」，确认连接、图片识别和输出格式，再保存。校准弹层顶部显示当前引擎和模型，各轮显示实际模型、耗时及可取得的 token 数。

API Key 由 DPAPI 按本机 Windows 账号加密，保存在 `%APPDATA%\whitebg-studio\ai_key.bin`。换电脑后，请在「AI 设置」里重新填写 Key。保存时留空继续使用原密钥，点「清除」后保存可移除密钥。

第 0 轮带 `want_manifest: true` 和当前 `accepted_overrides`；后续轮次携带截断后的完整新值，从原场景重新套用。默认最多调整到第 3 轮（含原始第 0 轮，共最多 4 次小样），判定已准或达到上限就进入「待确认」。同一服务一次只运行一个 AI 校准。

点「AI 校准」可查看各轮小样、结论、调整与置信度。「用这一轮」采用该轮已经渲染验证的调整。「撤销 AI 调整」清空已采用调整。「再调一轮」保留历史记录，可补充要求。采用后，按钮旁的紫点提示已采用的轮次。提交正式渲染时，作业使用任务的 `accepted_overrides`。

校准中可以调整画面。打光控件和排序手柄暂时置灰。AI 调用失败、输出不合 schema，或执行器缺少校准输入时，保留当前小样并显示失败，仍可普通渲染。服务重启后，正在出小样的轮次继续等待原作业，正在思考的轮次标为失败，可手动再调一轮。

每轮保存到 `exchange/tasks/<id>/calibration/round-<n>/`：

| 文件 | 用途 |
|---|---|
| `smallshots/<no>.jpg` | 该轮全部小样，历史轮次保留 |
| `scene_manifest.json`、`regions/<no>.png`、`regions/legend.json` | 执行器提供的场景与材质分区 |
| `stats.json` | 每张、每种材质的面积占比、平均 sRGB、平均 D65 Lab、亮度标准差与高光占比 |
| `input.json`、`prompt.txt`、`command.json` | 分析输入、图片用途与 Codex 命令参数 |
| `result.json`、`validated.json` | 原始输出与校验截断后的建议 |
| `usage.json` | 引擎、模型、耗时和可取得的 token 计数 |
| `stdout.log`、`stderr.log`、`error.log` | 调用日志，错误日志只在失败时存在 |

颜色统计按分区图每个通道 ±6 匹配，平均 Lab 逐像素转换后求平均；亮度采用 sRGB 加权灰度，高光严格大于 245。分区图与小样尺寸不一致时失败。图片预算为最多 12 张；小样与分区较多时分别拼成按编号排列的对照页，所有启用项都保留，随后按预算附同产品白底、材质参考、两张风格参考和一张主图参考。候选参考会标明可信度低，不使用「不用」的图。

配置新增：

| 配置项 | 默认值或行为 |
|---|---|
| `codex_command` | `codex`；支持可执行文件名、完整路径或参数数组，测试用数组指定 Python 和假 Codex |
| `codex_home` | `null` 时优先 `%USERPROFILE%\.codex-exec`，目录不存在则 `%USERPROFILE%\.codex`；相对路径从工作台根目录解析 |
| `codex_model` | `gpt-6.1-sol` |
| `codex_effort` | `medium` |
| `calibration_max_rounds` | `3`，第 0 轮之后自动调整的上限 |
| `calibration_timeout_sec` | `900`，单次分析最长 15 分钟 |
| `history_roots` | 本机 `产品资料/26.05` 至 `26.11` 七个月目录 |

真实调用为以下形式，提示词通过标准输入传入；由服务设置 `CODEX_HOME`，使用参数列表启动，不拼接 shell 命令。本次只执行了 `codex exec --help`，返回码为 0，未执行下面的真实校准。帮助命令提示 Codex 临时目录权限警告，实际账户和额度未验证。

```powershell
Get-Content -Raw -Encoding UTF8 "<round目录>\prompt.txt" | codex exec --skip-git-repo-check --ephemeral -s read-only -m gpt-6.1-sol -c 'model_reasoning_effort="medium"' --output-schema "<工作台目录>\server\calibration_schema.json" -o "<round目录>\result.json" -i "<小样或对照页>" -i "<分区或对照页>" -i "<参考图>" -
```

服务端为校准 render 作业补充 `calibration_round` 和绝对路径 `calibration_dir`，小样路径指向该轮的 `smallshots/`。执行器应把清单与分区写入该 `calibration_dir`。本次只扩展了假执行器，未改动 `max/`、`docs/`，真实执行器接口还需联合验证。

新增接口：

| 方法与路径 | 用途 |
|---|---|
| `PUT /api/tasks/<id>` | `{ai_calibration:{enabled:true或false}}`，只开放开关，不能伪造采用结果 |
| `POST /api/tasks/<id>/calibration` | `{action:"start",user_note:"..."}`、`{action:"accept",round:N}`、`{action:"undo"}` 或 `{action:"retry",user_note:"..."}` |

安全复现校准全流程、生成截图（在上一级目录运行）：

```powershell
python whitebg-studio/tests/calibration_acceptance.py
```

脚本在 `tests/tmp/calibration-browser-*/` 内复制网页、建立临时产品与独立参考库，在临时端口启动服务；`codex_command` 强制为 `[python, tests/fake_codex.py]`。假 Codex 读取真实 schema 与本轮统计，先给固定小调整，第 2 轮返回已准。脚本通过本机已有 Edge 与 `playwright-core` 点击出小样、采用、撤销、重新采用、提交正式渲染，并检查 final 作业的调整。它不会向正式 `exchange/jobs/` 提交作业，不修改真实产品或真实参考库状态。

报告保存到 `tests/calibration-acceptance.json`，截图为 `tests/screens/04-calibration.png`、`05-refs.png`；截图展示隔离任务与隔离参考库。窗口与服务在验收后关闭，临时产物保留。正常运行时会自动调用配置中的真实 Codex；开发演示应使用此隔离脚本，不要用旧演示入口开启 AI 校准。

## 参考库

首页点「参考库」进入按材质分组的缩略图网格，点「认可」或「不用」。可把图片拖到材质分组、点「添加图片…」上传内容，或粘贴图片完整路径后点「添加路径」。桌面窗口拖入的完整路径按鼠标所在分组添加。只写工作台的 `refs/`，原图只读；同一来源重复加入时复用缩图，保留原有认可状态。

`refs/library.json` 保存材质键、关键词、缩图路径、原图路径和 `candidate|approved|rejected` 状态，风格参考单独一组。点「添加并标注…」或把图片拖到分组外，在图上框选对应材质。标注裁图自动视为已认可，优先用于 AI 校准。剩余名额优先使用已认可图，没有已认可图时用候选图。分组标题旁的「关键词」用于匹配 Max 材质名。

首页「已有任务」显示交换目录总大小。已入库任务有可清理的中间文件时，可以点「清理已入库任务」并确认。服务启动后及此后每 24 小时会自动清理入库超过 30 天且没有进行中作业的任务。详情见 [参考图标注与清理](docs/参考图标注与清理.md)。

默认按配置的七个月历史目录建库：

```powershell
python whitebg-studio/tools/build_ref_library.py
```

本次按要求实际扫描整个指定根目录（包含其中可访问的其他月份）：

```powershell
python whitebg-studio/tools/build_ref_library.py --history-root "D:\产品资料"
```

扫描 `sku`、`SKU`、`白底` 下的图片，排除 TD、遮罩、通道、旧版及附加输出，不跟随目录链接；材质候选严格按文件名关键词匹配，不借用上级目录名猜材质。最长边缩至 1600，透明区域铺白底，原始尺寸与文件保持不变。本地历史大图直接读取后缩小，不套用网页上传的 50 MB 限制。风格固定随机抽取最多 12 张，重复建库保留认可状态，覆盖库文件前先备份。

本次实际扫描 429 张合格来源图，建库读取错误 0 个；结果保存在 `refs/build-report.json`：

| 材质键 | 候选数 |
|---|---:|
| 摩卡红橡 | 14 |
| 胡桃木 | 5 |
| 奶油白 | 7 |
| 北欧橡木 | 5 |
| 幻影灰 | 1 |
| 纯黑 | 10 |
| 长虹玻璃 | 1 |
| 白底风格 | 12 |

多个材质关键词可能同时匹配一个文件，同一来源可以出现在多个分组；这些数字是待核对的候选数量。

| 方法与路径 | 用途 |
|---|---|
| `GET /api/refs` | 读取库 |
| `POST /api/refs/status` | `{key,file,status:"approved"或"rejected"}`，更新状态 |
| `POST /api/refs/images` | `{key,paths:[完整图片路径]}`，只读原图并缩小复制 |
| `POST /api/refs/upload?key=材质键&name=文件名` | 上传图片原始字节，单张最多 50 MB |

Python 新增测试覆盖调整截断与权限边界、颜色统计、状态机停止/上限/失败、超时、输出校验、重启恢复、单校准约束、参考库建库与接口、正式任务采用调整、Windows 文件占用重试。网页新增中文改动说明测试；原有用例保留。

## 2026-11-20 AI 校准实测修正

按 SPEC §11.5 实测修正，Codex 输出的 `materials`、`lights.per_light`、`items` 使用数组，每个对象列全属性并全部必填，拒绝额外字段，`ior` 支持数值或 `null`。服务端先校验结果，再转换为 §11.4 字典并沿用范围截断。`result.json` 保留数组原始结果，`validated.json`、任务记录和下发作业使用字典。提示词说明了输入字典与输出数组的区别及中性值。

执行器按属性识别传统、物理、光线切换和多维材质。光线切换以外层名称记录、分区和调整，颜色、类型及参数读取 direct 子材质（缺失时取首个子材质），调整遍历所有不同子材质并去重。物理材质反射通过 F0 反算 `baseIor`（F0 上限 0.2，IOR 下限 1.01）；`roughnessMode=1` 减去光泽增量，为 0 时加上增量。多维材质按实际面 ID 计数，并通过 `materialIDList` 映射到面数最多的子材质。清单 `params` 记录实际颜色和数值属性，包括光泽模式、IOR、反射、折射及透明度等当前值。

离线验证（从工作台根目录运行）：

```powershell
python -m unittest discover -s whitebg-studio/tests -p "test_*.py"
python -m unittest discover -s whitebg-studio/max/tests -p "test_*.py"
python -m py_compile whitebg-studio/max/wb_core.py whitebg-studio/max/wb_runner.py
python whitebg-studio/tests/recompute_calibration_stats.py
```

本次服务端 89 例、执行器 112 例通过，包括递归检查 schema 全部 8 个对象的严格性、数组转换后截断、假 Codex 完整状态流和仅带属性值的假材质。新版数组协议替身位于 `tests/fake_codex.py`；边界外的旧 `tools/fake_codex.py` 保留原状，不作为本次离线验收入口。执行器两文件编译通过，`wb_runner.py` 没有保存场景调用；执行器修复尚未在真实 Max 中复测。

真实统计只读 `exchange/tasks/20261120-103600-056c/calibration/round-0/` 的 `regions/1.png`、`regions/2.png`、`regions/legend.json` 和对应 `smallshots/*.jpg`。每种图例色生成原色及 gamma 编码色两个候选，选最近的欧氏距离，距离 ≤ 30 才归类，黑色背景不计。报告为 `tests/calibration-real-stats.json`，包含输入 SHA-256 和未改动核对。

每张图为 800 × 600，共 480000 像素；面积占比分母为整张图的像素数（包括背景），另一材质沿用原始 `legend.json` 第二项名称：

| 图 | 奶油白像素 | 奶油白面积占比 | 另一材质像素 | 另一材质面积占比 |
|---|---:|---:|---:|---:|
| 1 | 105366 | 21.951250% | 27112 | 5.648333% |
| 2 | 120167 | 25.034792% | 10303 | 2.146458% |
| 两图合计 | 225533 | 23.493021% | 37415 | 3.897396% |

原始实测文件保持不变，本次未调用真实 Codex，未向正式 `exchange/jobs/` 提交作业。覆盖源码前均保留 `*.bak` 备份。
