# 白底工作台 v0.1：Max 任务执行器

目标环境是 **3ds Max 2025、内置 Python 3 / PySide6 / pymxs、Corona 12 Update 2**，接口以 `../docs/SPEC.md` 为准。本次开发环境没有 Max，不能把语法检查及纯 Python 测试当作 Max 实测结果。

## 启动、停止与调试

1. 保持目录结构，把上层服务提供的配置放在 `whitebg-studio/config.json`。相对路径相对于该文件所在目录，任务文件路径应全部为绝对路径。
2. 在 Max 中拖入 `启动白底执行器.ms`，或通过“脚本 → 运行脚本”运行它。脚本用 `python.ExecuteFile` 加载本目录的 `wb_runner.py`，再调用 `start`，每 3 秒检查一个任务。
3. 再次运行启动脚本会复用已有定时器和配置；会话状态独立保存在 `sys.modules`，重复加载脚本也不会增加定时器。
4. 运行 `停止白底执行器.ms` 停止后续轮询。同步渲染需要先完成任务清理和释放锁，停止操作不强行中断渲染。

工作台也可通过 `3dsmax.exe -U MAXScript <脚本路径>` 启动 Max 和执行器。执行器开启后约每 3 秒更新 `runner.heartbeat.json`，空闲、等待锁和渲染期间均更新。停止时写入 `stopped: true`。

在 Max 的 Python 控制台中也可调用：

```python
import wb_runner
wb_runner.start(r"D:\路径\whitebg-studio\config.json")
wb_runner.stop()
wb_runner.run_once()  # 同步处理当前配置中的下一个任务
wb_runner.run_once(r"D:\路径\whitebg-studio\config.json")  # 停止轮询后指定配置调试
```

所有 Max 操作都在 Qt 主线程执行；`busy` 防止定时器在渲染消息循环中重入。运行定时器时不能通过 `run_once` 更换配置，应先 `stop`。已有 `done`、`failed` 的任务不重做；等待锁和中断留下的 `running` 可重试。中断任务按整个任务重做，旧输出先备份。重试已失败任务应由服务创建新的 `job_id`。

本目录不会创建或修改 `../config.json`。以下字段仅供上层服务配置：

```json
{
  "exchange_dir": "exchange",
  "template_max": "D:\\模板\\白底模板.max",
  "template_anchor": { "x_mm": 444.0, "back_y_mm": 461.0, "floor_z_mm": 0.0 },
  "render_preset_rps": null,
  "smallshot_time_limit_sec": 25,
  "max_lock_owner_prefix": "WhiteBG"
}
```

`template_anchor` 必须是上述三字段对象；任务未提供时使用配置值。缺失、null、旧三元组或废弃键均不自动迁移，不再从模板包围盒猜测锚点。仅导出预览时不需要锚点。

## 状态、日志与备份

- 查看 `<exchange_dir>/jobs/<job_id>.status.json` 和 `<exchange_dir>/runner.log`。任务明细各有状态和 `message`；遮罩失败且成图成功时，该项仍为 `done` 并写明原因。任一成图失败则任务整体为 `failed`，其余明细继续处理。
- 状态中的 `log` 保留最近 200 行。`runner.log` 追加写入，在文件超过 1 MB 前轮换为 `runner.log.1`，最多保留三代。
- 状态、bbox 等 JSON 写入同目录临时文件后原子替换。重渲染前将旧图片和 FBX 移到同名 `.prev`，避免误认旧输出。已有 `.bak` 保留。
- 损坏的任务或状态记录为 `failed`，原文件/备份保留，避免静默重渲染；其他合法任务继续扫描。排序使用带时区的 `created_at` 的真实时间，同一时刻按文件名排序。
- 配置不可读时只能尝试写默认 `../exchange/runner.log`；磁盘不可写或 Python 尚未启动时，错误只能写到标准错误或 Max Listener，无法保证状态落盘。执行器不调用消息框或文件选择框。

测试产物保留在项目的 `tests/tmp/`。

## Max 锁与场景安全

每个任务通过 `runtime.execute` 执行以下接口，锁脚本只读：

```maxscript
(fileIn @"C:\Users\<用户名>\.codex\skills\panel-furniture-modeling\scripts\pf_max_lock.ms";
 ::PFMaxLock.acquire "WhiteBG:<job_id>" @"<exchange_dir>\tasks\<task_id>")
```

owner 前缀来自 `max_lock_owner_prefix`，默认 `WhiteBG`。返回不以 `OK` 开头就写 `waiting_lock`，不碰场景，下轮再试。任何退出路径均按同一 owner 尝试释放，绝不强制释放他人的锁。

获得锁后，当前场景有对象且 `getSaveRequired()` 为真时，状态为 `waiting_lock`，message 为“当前 Max 场景有未保存的修改”，随后释放锁，不清场。可处理的任务在完成或异常后调用 `resetMaxFile #noPrompt` 清场。模板和产品仅加载/合并，绝不保存任何 `.max`。

原锁脚本把超过 **15 分钟**未活动的占用视为失效，reset/load 不清除内存锁。执行器在导出、每张渲染前以及每项处理后调用 `touch`；渲染失败时也在 finally 刷新，刷新失败则结束任务。同步渲染期间没有能保证执行的独立心跳，单张超过 15 分钟仍可能被其他调用者按原规则接管；本稿未修改原锁协议。

## 坐标与渲染约定

- `wb_core.py` 负责配置、队列、文件协议、单位、PF1、视场角、包围盒、锚点和材质名称识别，不依赖 Max；`wb_runner.py` 负责 Max 操作。
- PF1 毫米偏移 `(x,y,z)` 对应 Max 系统单位偏移 `(x,-z,y)/mm_per_unit`，再加产品包围盒中心。正面是 Max 的 −Y。`units.decodeValue("1mm")` 的倒数作为每系统单位毫米数，不修改系统单位。
- 预览和渲染统一只收集可见且有网格面的产品几何体，排除相机、灯光、辅助物体、组头、TargetObject 和视口隐藏对象。PF1 原点、贴墙位移及 bbox 均按同一可见网格口径计算，隐藏备用网格不参与。世界 bbox 使用 `nodeGetBoundingBox(node, Matrix3(1))`。FBX 是 Y 向上、毫米、二进制，不导出相机、灯光、动画或嵌入贴图，导出器参数用后尝试恢复。
- 贴墙锚点为 Max 世界毫米：可见产品包围盒的 x 中心对齐 `x_mm`、y 最大面（产品背面）对齐 `back_y_mm`、z 最小面（底面）对齐 `floor_z_mm`。位移在 `wb_core.placement_from_bbox` 计算，毫米/厘米/米系统单位均有测试。
- 背面视角从 PF1 的权威 `position_mm/target_mm` 推导方位角，忽略可选的界面 `azimuth_deg` 提示值。只有绝对值 **大于 100°** 时，才隐藏模板中世界 bbox 最小 y（换算毫米）**大于等于 `back_y_mm`** 的网格，跨越平面的几何体不隐藏。模板节点在合并前收集，产品节点不会被误选。
- 每项保存原来的 `isNodeHidden` 标志，临时隐藏后在 finally 恢复，原本隐藏的节点保持隐藏；不写 `isHidden=False`，避免顺带清除图层隐藏状态。隐藏恢复失败时终止任务并清场，防止继续生成受到污染的下一项。
- 只移动没有合并父节点的顶层合并对象，包括组头/辅助父节点，使用世界矩阵平移，避免父子重复移动；移动后复测包围盒，误差超过 0.01 mm 则报错。
- 产品源 `.max` **第 0 帧应当就是合上状态**。第 0 帧求值和渲染，不执行 `parts`，不改开合控制器。
- 相机位置和目标以 PF1 数据为准；水平 FOV 用 `2*atan(tan(vfov/2)*W/H)`，Max 角度属性以度赋值，像素宽高比为 1。新建标准 Target Camera 默认世界 Z 向上、零横滚；相机与目标重合或完全竖直时该项失败，不猜测地平线。
- 非空 `render_preset_rps` 调用 `renderPresets.LoadAll(0,path)`；为空不调用。沿用模板/预设参数；AI 校准只套用 §11.4 白名单，不修改渲染预设。未提供调整时，玻璃/金属仅记录。
- 小样只使用 Claude 实测确认的 `progressive_timelimit`，设置前先 `hasProperty` 检查，**暂按毫秒**换算：25 秒写入 25000，非整数毫秒向上取整。**时间属性单位仍待 Claude 在 Corona 12.2 实测确认**；代码不再尝试其他属性别名。找不到属性仅记日志、沿用模板。结束或异常时恢复原值；正式模式不写时间上限。
- `progressive_passlimit`（模板实测为 7）、`adaptivity_targeterror`、`bg_overrideReflect`（模板实测为 true）只读取并记录，绝不修改。因此小样可能先达到模板的 pass/noise 停止条件，时间上限不是保证渲染时长。原反射覆盖保持生效。
- 正式渲染优先按类名或元素名**精确匹配「遮罩_ID」**，该通道输出到 `out_mask`；没有精确 ID 才退回 Mask / ID / 遮罩模糊匹配，已知「遮罩_线框颜色」绝不作为 ID 兜底。
- 「遮罩_线框颜色」按精确名字单独识别，输出到 `<exchange_dir>/tasks/<task_id>/final/_extra/<no>_遮罩_线框颜色.jpg`，不作为 `out_mask`，不加入交付清单。两个通道的输出都先备份旧文件，再在每项 finally 恢复原输出路径；不改变元素启用状态、`What=2` 或其他属性。恢复输出路径失败会写入日志。
- 正式渲染用 `renderElementBitmaps=pymxs.byref(None)`，同次渲染返回成图与元素位图；自动输出缺失后才保存对应位图，不重复渲染。仅在总元素数或启用元素数可明确对应索引时另存，否则写原因。此处直接用 pymxs 引用参数接口，无需额外的大段 MAXScript。

## 未实测的 pymxs 与启动调用清单

以下逐项都需在 **Max 2025 + Corona 12 Update 2** 核对，普通 Python 测试不能证明其可用。

| 调用 | 本稿假设与实机核对内容 |
|---|---|
| `python.ExecuteFile`、`python.Execute`、`getSourceFileName`、`pathConfig.appendPath` | 拖入及菜单运行均能取得自身路径；Python 有 `__file__`；中文、空格和单引号路径可正确传递；再次启动不增加定时器。 |
| PySide6 `QApplication.instance/QTimer/QThread.currentThread` | Max 主线程已有事件循环；每 3 秒触发；渲染重入被拦截；stop 后不接新任务。 |
| `runtime.execute` 的 `fileIn`、`::PFMaxLock.acquire/touch/release` | 返回字符串前缀为 `OK/BUSY`；跨 reset/load 保留锁；其他 owner 不会被错误释放。 |
| `rt.objects`、`getSaveRequired`、`SetQuietMode` | 所有场景对象都被安全检查覆盖；未保存场景不被清空；安静模式可恢复旧值。 |
| `loadMaxFile(...,quiet=True,missing*Action=...)` | 成功返回 Python `True`；缺 DLL/XRef 时 abort，缺贴图时 logmsg；参与 Quiet Mode 的对话框被抑制。第三方独立弹窗仍需核对。 |
| `resetMaxFile(Name("noPrompt"))` | 不询问保存，成功返回值不为 `False`；异常后的部分场景仍可清空。 |
| `pymxs.animate(False)`、`pymxs.attime(0)` | 禁止自动打关键帧，在第 0 帧求值，退出可恢复；load/merge 不意外改变求值帧。 |
| `superClassOf/classOf/GeometryClass/TargetObject/isGroupHead/isValidNode`、`node.isHiddenInVpt` | 预览和渲染都正确排除目标点、组头、非网格和隐藏节点。 |
| `node.isNodeHidden` | 背面项的临时隐藏能影响 Corona 输出；原隐藏标志在成功/异常后恢复且不改变图层隐藏状态，跨平面几何体与产品不会被隐藏。 |
| `snapshotAsMesh/getNumFaces/nodeGetBoundingBox(node,Matrix3(1))` | 修改器栈后的网格可读取；bbox 包含旋转、缩放和对象偏移。代理/程序几何不能读取时报错，不静默省略。 |
| `units.decodeValue("1mm")` | 返回每毫米对应系统单位数；毫米、厘米、米场景物理尺寸正确；merge 后应维持模板系统单位，需核对 Max 的 load/merge 单位策略。 |
| `pluginManager.loadClass(FBXEXP)`、`FBXExporterGetParam/SetParam`、`select(Array(...))`、`exportFile` | 安装的 FBX 插件支持所列参数；SetParam 返回 `OK`；UpAxis/ConvertUnit/ScaleFactor/AxisConversionMethod 产生正确朝向和毫米尺寸，selectedOnly 不夹带其他节点。 |
| `rt.cameras`、`node.target`、`delete` | 集合可能含目标点，通过 superclass 过滤后仅删除模板相机和目标。 |
| `mergeMAXFile` 的四个旗标、`getHandleByAnim`、`node.parent/transform`、`Matrix3.row4` | 仅识别新合并节点；组层级保留；世界矩阵位移不重复；重名材质采用输入材质；控制器接受位移。 |
| `getNumSubMtls/getSubMtl`、材质 `name/classOf/getHandleByAnim` | 可递归检查多维材质，handle 去重可靠；检测异常仅记日志。 |
| `renderPresets.LoadAll(0,path)` | 成功返回 Python `True`，所有预设类别被加载；每项之后重新设置输出尺寸。 |
| `TargetObject/TargetCamera/fovType/fov`、可选 `rollAngle` | 明确创建目标，水平角以度赋值，默认 LookAt 世界 Z 向上且零横滚；网页和成图构图一致。 |
| `renderers.current`、`hasProperty/getProperty/setProperty` | 已按用户提供的实测名字实现 `progressive_timelimit`；**毫秒单位仍待实测**。需核对 25000 对应约 25 秒、异常后恢复原值、pass/noise/反射覆盖开关均保持原值；反射覆盖贴图仅按 AI 校准白名单临时包裹。 |
| `renderWidth/renderHeight/renderPixelAspect`、`render(camera=...,frame=0,outputFile=...,vfb=False,quiet=True)` | 使用指定相机、帧、尺寸并返回 Bitmap，无输出选择对话框，沿用模板 Corona 色彩和后期。 |
| `maxOps.GetCurRenderElementMgr`、`NumRenderElements/GetRenderElement/GetElementsActive/GetRenderElementFilename/SetRenderElementFilename` | 管理器为 0 基索引；已使用实测的「遮罩_ID」「遮罩_线框颜色」精确名字，但路由调用仍未实测；需核对输出路径与异常后恢复，元素 enabled 和 What 均不变。 |
| `pymxs.byref(None)` 配合 `renderElementBitmaps` | 返回 `(beauty,bitmaps)`，Corona 元素位图顺序对应全列表或启用列表；无法映射时不标记遮罩成功。 |
| `bitmap.filename`、`save(bitmap)`、`close(bitmap)` | `.jpg` 无对话框保存，元素位图可另存并关闭；模板色彩管理及 JPEG 遮罩结果需检查。 |

## 本地验收

在 `whitebg-studio` 的上一级运行：

```powershell
python -m unittest discover -s whitebg-studio/max/tests -p "test_*.py"
python -m py_compile whitebg-studio/max/wb_core.py whitebg-studio/max/wb_runner.py
rg -n '^\s*(import pymxs|from pymxs\b)' whitebg-studio/max/wb_core.py
rg -n -i 'template_anchor_mm|saveMaxFile|saveNodes' whitebg-studio/max/wb_runner.py
```

当前普通 Python 验证：**143 个用例通过**，包括原有 112 项和打光新增 31 项。覆盖参数截断、HSV、属性候选顺序、粗糙度反向调整、旋转矩阵、材质分类、分区颜色区分度，以及模拟的共享材质包裹、玻璃限制、灯光强度、逐项旋转恢复、PNG 路由、清单和图例备份；打光覆盖见下节。模拟调用不能代替真实 Max/Corona 测试。后两条 `rg` 应无输出、返回 1，表示没有匹配。`py_compile` 只检查语法，不执行 runner。

## AI 校准

按 `../docs/SPEC.md` §11.2、§11.4 接收 render 任务的 `overrides`（允许 null）、`want_manifest`、`calibration_round`（缺省 0）。合并产品、贴墙并读取预设之后、建相机之前套用材质和环境调整；灯光调整在每项渲染前套用、项末恢复。所有调整数值先经过 `wb_core.clamp_overrides`。未知字段丢弃，非法类型及非有限数值报错。未提供的字段保持中性，未提供调整时不额外改材质或灯光。

产品材质按 handle 递归去重，同一物体对子材质的重复引用只计一次。贴图外包一层 `CoronaColorCorrect`，同一任务重复套用时替换本执行器自己的层；产品原有的校正层仍保留。纯色用 0…255 RGB 转 HSV 改色，再以 RGB 0.5 为中心调整对比度。反射、折射、透明度结果限制在 0…1，光泽同样限制在 0…1；IOR 限制在 1.3…1.8。折射、IOR、透明度只调整识别为 glass 的材质。属性不存在时写“<材质>：没有可调的 <项目>”，不创建猜测属性。

模板灯在合并产品前收集。未打光项的 AI 全局与单灯强度系数相乘；绕摆放后产品底面中心的世界 Z 轴旋转，正值从上往下看逆时针；灯光及有效目标点的世界位置与朝向一起旋转，父节点先赋值，目标点按 handle 去重。每项角度覆盖全局角度，例如全局 60°、本项 −30°，该项直接从模板原始矩阵旋转 −30°；该项结束或失败后恢复模板原始强度和矩阵。已设打光项使用下节规则，忽略 AI 旋转与单灯系数。恢复失败终止任务并清场。环境只包裹已有反射覆盖贴图，保持 `bg_overrideReflect` 开关、pass/noise、色彩管理及其他渲染参数不变。所有改动只在内存，任务结束走原有清场逻辑，不写 `.max`。

`want_manifest: true` 时，在 `<exchange_dir>/tasks/<task_id>/calibration/round-<n>/` 写入：

- `scene_manifest.json`：调整后的产品材质（名称、类、引用物体数、kind、RGB、递归查到的首个位图文件名、实际可读的标量属性）、模板灯、环境反射贴图名称、产品世界 bbox 中心毫米坐标和 `unsupported`。`lights` 记录模板基准灯；逐项角度由任务中的 overrides 表达；打光实际结果逐项追加到 `lighting_applied`。
- `regions/legend.json`：`{"#RRGGBB": "材质名"}`。配色用固定 RGB 网格最远点选择，远离黑白；颜色间最大通道差至少 32，避免 ±6 匹配窗口重叠。当前最多 300 种主材质；超过容量明确报错，不复用颜色。
- `regions/<no>.png`：产品节点的 `wirecolor` 按主材质赋值，「遮罩_线框颜色」路由为 PNG。多维或复合材质递归取第一个子材质，空槽按“未分配材质”，局限写入 `unsupported`。无此元素只记日志，小样继续。沿用模板元素的启用状态和 `What`；模板未启用通道时记录原因。小样及正式渲染都只渲一次，自动输出缺失时尝试保存同一次返回的元素位图，逐项恢复元素原路径。

分析 JSON 采用同目录临时文件原子替换。`wirecolor`、全局材质和环境调整保留到该任务清场，逐项灯光在项末恢复。

### 新增的未实测调用及属性名

下列全部是 **Max 2025 + Corona 12 Update 2 待实测项**，候选属性均先 `hasProperty` 再 `getProperty/setProperty`，按表内顺序使用第一个存在的属性。属性存在只能证明名称可读，不能证明语义和单位正确。

| 对象/项目 | 候选属性名（按优先顺序）或调用 | 假设与实测重点 |
|---|---|---|
| Legacy 类识别 | `CoronaMtl`、`CoronaLegacyMtl` | 两个类名使用相同映射；大小写无关。 |
| Legacy 漫反射色、贴图 | `colorDiffuse / diffuseColor`；`texmapDiffuse / diffuseTexmap` | 色值为 Max Color 0…255；赋回不改变贴图启用开关和权重。 |
| Legacy 反射 | `levelReflect / reflectionLevel / reflectLevel` | 0…1 强度。 |
| Legacy 光泽 | `reflectGlossiness / reflectionGlossiness / reflGlossiness / roughness` | roughness 按 −delta，其余按 +delta。 |
| Legacy 折射、IOR、透明度 | `levelRefract / refractionLevel / refractLevel`；`refractIor / refractionIOR / ior`；`opacity / opacityLevel / levelOpacity` | 折射及透明度为 0…1 标量，IOR 为绝对值；不改吸收、薄壳或贴图权重。 |
| Physical 类识别 | `CoronaPhysicalMtl` | 仅此类使用 Physical 映射。 |
| Physical 基础色、贴图 | `baseColor / base_color`；`baseTexmap / texmapBase / baseColorTexmap` | 基础色和贴图槽名称待核对。 |
| Physical 反射 | `baseReflectionLevel / reflectionLevel / reflectLevel / baseReflectivity` | 可能没有独立反射强度，此时跳过并记日志；不拿 `baseLevel` 或 IOR 代替反射强度。 |
| Physical 光泽及模式 | `baseRoughness / roughness / baseGlossiness / glossiness`；`roughnessMode / baseRoughnessMode` | 模式值 1 假设表示 glossiness，0 表示 roughness；模式缺失时按属性名判断；不更改模式。 |
| Physical 折射、IOR、透明度 | `refractionAmount / refractionLevel / refractLevel`；`baseIor / baseIOR / ior`；`opacityLevel / opacity / levelOpacity` | 0…1 强度和绝对 IOR；若材质启用了其他 IOR 表示模式，实际含义仍需核对。 |
| 校正贴图创建和输入 | `CoronaColorCorrect()`；`inputMap / texmap / input / map` | 可直接外包原贴图；不修改原图、不改文件。 |
| 校正色相、饱和度、亮度、对比度 | `hueShift / hue_shift / hue`；`saturation / saturationMultiplier`；`brightness / brightnessMultiplier`；`contrast / contrastMultiplier` | 色相假设为角度；另外三项读取新建贴图的中性值，中性值为 0 时写倍率−1，为 1 时直接写倍率；其他尺度跳过并记日志。环境亮度允许 0.5…2，不套用材质的 0.6…1.4 范围。需实测与纯色改色的视觉一致性。 |
| 模板灯强度 | `intensity / multiplier / intensityMultiplier` | 当前强度乘全局和单灯系数，物理单位不变。 |
| 灯光及目标旋转 | `node.target/parent/transform`、`copy(Matrix3)`、`Matrix3(Point3,Point3,Point3,Point3)`、`row1…row4` | Matrix3 行向量约定；旋转保持原比例，目标控制器和层级接受世界矩阵；程序/约束灯可能拒绝赋值并使当前任务失败。 |
| 环境反射覆盖贴图 | `bg_texmapReflect / bg_texmapReflection / reflectOverrideTexmap` | 仅读写贴图引用，绝不切换反射覆盖开关；没有原贴图时跳过。 |
| 材质与位图清单 | `getHandleByAnim/getNumSubMtls/getSubMtl/getNumSubTexmaps/getSubTexmap`、`filename / fileName`、`Color.r/g/b` | handle 去重可靠；递归找到首个位图文件名；程序贴图无文件名时 bitmap 为 null，不当成纯色。 |
| 分区线框色 | `node.wirecolor = Color(r,g,b)` | CMasking 线框颜色元素实际输出该颜色；透明、反射、多维材质和抗锯齿边缘仍需核对。 |
| 分区 PNG 与位图返回 | `SetRenderElementFilename`、`render(..., renderElementBitmaps=pymxs.byref(None))`、`bitmap.filename`、`save(bitmap)`、`close(bitmap)` | 小样也返回 `(beauty, elements)`；PNG 无对话框；模板通道已启用且输出不受色调映射干扰。未修改全局 PNG 插件设置，位深/色彩空间/边缘混色需实测。 |

[Chaos 官方 MAXScript 文档](https://docs-chaos.atlassian.net/wiki/spaces/CRMAX/pages/124394405)建议用 `show renderers.current` 检查实际属性；该文档没有提供本版本全部材质和校正贴图的参数表。上述候选和尺度仍是离线实现假设，不能当作 Corona 12.2 实测结果。

场景保存调用检查：

```powershell
rg -n -i '\b(saveMaxFile|saveNodes|savePlus|holdMaxFile|saveScene|saveAs)\s*\(' whitebg-studio/max/wb_runner.py
```

应无匹配（退出码 1）。`rt.save(...)` 只接收渲染位图，不是保存场景。

## 打光

按 `../docs/SPEC.md` §12 全部、§11.8 和 §11.7 实现。render 任务每项的 `lighting` 已由服务端合并；执行器不再次从产品级字段继承。`lighting: null`、缺少设置或 `azimuth_deg: null` 不增加主光、不降低补光；原有 AI 校准仍按该项 overrides 执行。未设置 AI 调整时，完全保留模板灯光。

`wb_core.normalize_lighting` 截断方位 −180…180°、仰角 5…85°、整体亮度 0.6…1.5、对比 0…1；非法方位置 null，其他非法字段采用 35°、1.0、0.5 默认值。`bool`、字符串和非有限数值不当作数字。函数不修改原始任务。

每项打光按移动后的产品**可见网格世界包围盒中心**定位。主光距中心为对角线的 2.5 倍，宽高均为对角线的 0.5 倍；PF1 +X 对应 Max +X、PF1 +Y 对应 Max +Z、PF1 +Z 对应 Max −Y。几何计算使用 Max 系统单位，清单位置换算为 Max 世界毫米。

主光强度为 `config.key_light_intensity`（默认 20，待 Claude 实测标定）× `lighting.intensity` × AI `lights.intensity_mul`；模板内所有灯的原强度乘 `(1 − 0.75 × contrast) × intensity × lights.intensity_mul`。执行器对已打光项再次将 AI 整体系数限制为 0.85…1.15，并忽略 AI 全局/逐项旋转和 `per_light`，保持使用者指定光向。产品合并带入的灯不作为模板补光调整。

灯类只从 `rt.light.classes` 按名称寻找，精确优先 `CoronaLight`、`Corona灯光`、`CR灯光`，再匹配 Corona/CR 与灯/Light 标记。代码不读取 `rt.CoronaLight` 英文全局符号。主光从第一盏 Corona 模板灯复制可用的 `colorMode`、`blackbodyTemp`、`color`；不存在的颜色属性写日志，没有 Corona 模板灯则记录采用插件默认颜色、待核对中性白。

每项结束或渲染抛异常时恢复模板灯原始强度，并删除临时主光和目标点，下一项独立处理。找不到类、退化包围盒、无效主光配置或必要属性设置失败时，清理已创建节点、恢复已改强度，记录“跳过打光，继续渲染”。模板灯强度属性无法读取时记录该灯未调整。恢复或删除失败抛 `SceneRestoreError`，停止任务，交给原有 `resetMaxFile` 清场。全部在内存修改，不保存任何 `.max`。

有场景清单时，在每项开始渲染前写入 `lighting_applied`，按字符串编号保存，避免多项互相覆盖：

```json
{
  "lighting_applied": {
    "1": {
      "applied": true,
      "lighting": {"azimuth_deg": -45, "elevation_deg": 35, "intensity": 1, "contrast": 0.5},
      "pos_mm": [-1000, -1000, 1000],
      "intensity": 20,
      "intensity_mul": 1,
      "fill_ratio": 0.625,
      "description": "主光从左前方斜上方照来（水平 -45°，高 35°）；左侧受光较亮、右侧偏暗；明暗对比中等。"
    },
    "2": null,
    "3": {"applied": false, "reason": "明细 3 跳过打光，继续渲染：未找到可用的 Corona 灯类"}
  }
}
```

示例位置仅展示字段，实际值由包围盒计算。`null` 表示该项未设打光，`applied: false` 表示要求打光但未能套用。清单记录渲染尝试时的打光状态。

### 打光未实测清单

本环境没有 3ds Max。下列调用与属性组合均只完成离线模拟，需 Claude 在中文版 Max 2025 + Corona 12.2 实测；用户给出的模板属性名不等于新灯写入已验收。

| 调用或属性 | 实测重点 |
|---|---|
| `rt.light.classes`、`str(cls)`、匹配后的 `cls()` | 中文类能否枚举并直接构造；实际类名 `CR灯光` 与模糊匹配是否可靠。 |
| `hasProperty`、`getProperty`、`setProperty` | 每个设置前检查属性；确认节点包装对象上的读写行为。 |
| `shape=0`、`width`、`height` | **0 表示矩形尚未确认**；宽高是否按系统单位表示完整边长。 |
| `targeted=True`、`target`、`rt.TargetObject()`、`key.pos`、`target.pos`、`rt.Point3` | 是否自动创建有效目标；没有目标时手动赋目标是否可用；targeted 控制器是否使发光面准确朝向产品中心，是否存在翻面。 |
| `visibleDirectly=False`、`visibleRefl=False`、`visibleRefract=False`、`on=True` | 尤其 `visibleRefract` 是否存在，三种可见性开关的实际渲染效果。缺少必要属性会跳过整项打光。 |
| 主光 `intensity`；模板灯 `intensity / multiplier / intensityMultiplier` | 强度单位和模板是否一致；默认 20 的实际曝光、阴影和整体亮度需标定。 |
| `colorMode`、`blackbodyTemp`、`color` | 新旧 Corona 灯颜色模式枚举一致；温度和颜色复制后是否得到相同色温、中性白；缺属性时的默认色需核对。 |
| `rt.superClassOf(node) == rt.Light` | 合并前收集是否包含全部模板灯（含隐藏、关闭的灯），不包含产品自带灯。 |
| `rt.isValidNode`、`rt.delete(key)`、`rt.delete(target)` | 自动与手工目标都回收；主光删除自动删目标时能否安全跳过重复删除。 |
| 逐项异常恢复、清场 `resetMaxFile` | 渲染失败后模板强度精确恢复；主光不残留；连续不同 lighting 不串项；恢复失败确实终止任务。 |

离线测试覆盖 PF1 坐标和单位、参数截断与 null、补光比例、中文类查找、几何尺寸、颜色复制、AI 系数、混合项、缺属性降级、异常恢复、逐项清单及无场景保存调用。普通 Python 测试通过不代表上述调用已在 Max 中验证。

## 官方接口参考

- [Autodesk：pymxs 引用参数](https://help.autodesk.com/cloudhelp/2025/ENU/MAXDEV-Python/files/using_pymxs/pymxs_differences/MAXDEV_Python_using_pymxs_pymxs_differences_pymxs_by_ref_parameters_html.html)：byref 返回函数结果与输出参数的元组。
- [Autodesk：场景加载与合并](https://help.autodesk.com/cloudhelp/2024/ENU/MAXScript-Help/files/MAXScript-Tools-and-Interaction/File-Access/3ds-Max-Scene-Files-Access/GUID-624D3D05-B15D-4A97-9F15-DA35CDB0DDD2.html)：merge 旗标和 quiet 接口。
- [Autodesk：FBX 导出参数](https://help.autodesk.com/cloudhelp/2027/ENU/MAXScript-Help/files/MAXScript-Tools-and-Interaction/Interacting-with-the-3ds-Max/FBX-Import-and-Export-Dialog/GUID-54B1F140-B304-48A2-9829-C68B345E2044.html)：插件参数；安装的 2025 版本仍须逐项核对。
- [Autodesk：渲染调用和元素位图](https://help.autodesk.com/cloudhelp/2026/ENU/MAXScript-Help/files/MAXScript-Tools-and-Interaction/Interacting-with-the-3ds-Max/Render-Scene-Dialog/GUID-9175301C-13E6-488B-ABA6-D27CD804B205.html)：outputFile 与 renderElementBitmaps。
- [Autodesk：TargetObject](https://help.autodesk.com/cloudhelp/2024/ENU/MAXScript-Help/files/3ds-Max-Objects-and-Interfaces/Node-MAXWrapper/Node-Subclasses/GeometryClass-Node/Geometry-Standard-Objects/GUID-9FFE83EB-72F2-40C4-BD14-356DFCA88638.html)：目标点属于 GeometryClass，但没有实际网格。

文档用于核对接口写法，不代表本轮代码已在目标 Max/Corona 中运行。模板坐标、属性名及两个通道名字采用用户提供的 Claude 实测结果；时间单位按用户指示暂用毫秒，待实测确认。
