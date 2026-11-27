# 白底工作台 v0.1 接口规格（本地单机版）

本文件由 Claude 维护，是网页小程序（server + web）和 3ds Max 任务执行器（max/wb_runner.py，在 Max 自带的 Python 3 里用 pymxs 运行）之间的唯一约定。两边都按本文实现；觉得约定有问题，写进 decisions_needed，不要自行改语义。

## 0. 这一稿要做到什么

- 不懂 3D 的人在浏览器里：选产品文件夹 → 看 AI（规则）起草的白底清单 → 逐张拖动调视角和构图 → 出小样 → 正式渲染 → 看总览 → 确认入库。
- 渲染由本机 3ds Max 2025 + Corona 12 完成。Max 里运行 `wb_runner.py`，轮询交换目录里的任务文件。
- 本稿**只渲染合上状态**。开合滑杆只在网页里预览（模型带 PZ 绑定时才出现），渲染开合留到 v0.2。
- 本稿**不做**：打光方向切换、点对点对齐参考图、矩阵批量、安装包、自动更新、多机。

## 1. 目录

```
whitebg-studio/
  启动白底工具.bat        启动本地服务并打开 http://127.0.0.1:8770/
  config.json             见 §2
  server/                 Python 3 标准库实现的本地服务
  web/                    网页（ES Module，three.js 放在 web/vendor/three/）
  max/wb_runner.py        在 3ds Max 里运行的任务执行器（pymxs）
  max/wb_core.py          执行器里不依赖 Max 的纯计算（坐标换算、任务解析、状态文件），可在普通 Python 里测试
  max/启动白底执行器.ms     在 Max 里运行它即启动执行器
  exchange/               交换目录（以后会换成 \\渲染机\白底交换）
    tasks/<task_id>/task.json
    tasks/<task_id>/preview/      网页预览模型（preview.fbx）和 bbox.json
    tasks/<task_id>/smallshots/   小样 <no>.jpg
    tasks/<task_id>/final/        成品 <no>.jpg、<no>_遮罩_ID.jpg
    jobs/<job_id>.json            任务文件（写入后不再修改）
    jobs/<job_id>.status.json     执行器写的状态
    runner.heartbeat.json        执行器心跳
    runner.log                   执行器日志
  docs/SPEC.md
  tests/
```

## 2. config.json

```json
{
  "port": 8770,
  "exchange_dir": "exchange",
  "max_exe": "D:\\3d2025\\3ds Max 2025\\3ds Max 2025\\3dsmax.exe",
  "template_max": "D:\\模板\\白底模板.max",
  "template_anchor": { "x_mm": 444.0, "back_y_mm": 461.0, "floor_z_mm": 0.0 },
  "render_preset_rps": null,
  "final_size": { "portrait": [3000, 4000], "landscape": [4000, 3000] },
  "smallshot_size": { "portrait": [600, 800], "landscape": [800, 600] },
  "smallshot_time_limit_sec": 25,
  "max_lock_owner_prefix": "WhiteBG",
  "calibration_auto": false
}
```

- 相对路径相对于 `whitebg-studio/`。
- `max_exe` 是本机 `3dsmax.exe` 的完整路径。默认由使用者手动开始 AI 校准。`calibration_auto` 为 `false` 时，普通小样忽略旧任务的自动校准开关。
- `template_anchor`：产品在模板场景里的摆放位置（Max 世界坐标，毫米）。模板里柜子后面有一面墙（`Rectangle2139486305`，正面在 y≈471），产品贴墙摆：产品包围盒的 **x 中心** 对齐 `x_mm`，**背面**（Max 里 y 最大的一面）对齐 `back_y_mm`，**底面**对齐 `floor_z_mm`。2026-11-19 Claude 在 Max 里实测模板后定的值。
- `render_preset_rps`：Corona 渲染预设文件路径；`null` 表示沿用模板场景自带的渲染设置。无论哪种，除尺寸外不改任何渲染参数（小样另外只允许设时间上限）。

## 3. 产品坐标系 PF1（两边必须一致）

所有相机都存成 PF1 坐标，单位**毫米**：

- 原点：产品**合上状态**所有**可见**网格的包围盒中心（只算网格；不含隐藏对象、灯光、相机、辅助物体）。预览导出和渲染都用这同一个口径。
- +X：产品的右边（站在产品正面看时观看者的右手边）。
- +Y：向上。
- +Z：从产品正面朝外，指向站在正面的观看者。

换算：

| 坐标 | 网页（three.js，Y 向上） | 3ds Max（Z 向上） |
|---|---|---|
| X | x | x |
| Y（上） | y | z |
| Z（朝正面外） | z | −y |

- Max 的正面视图从 −Y 看向 +Y，所以产品正面朝 Max 的 −Y。
- 网页里预览模型是执行器从 .max 导出的 `preview.fbx`（§5），导出时 Up Axis = Y、单位毫米，所以 three 里 +Z 就是 PF1 的 +Z。网页按 FBX 读入后的真实尺寸换算成毫米（以 `bbox.json` 的尺寸为准，见 §5）。
- 网页加载其他来源的 FBX（例如带 PZ 绑定的 `*_绑定.fbx`）时，提供"旋转模型 90°"按钮，让人把正面转到 +Z；转动量存进 task.json 的 `preview_rotation_deg`。

## 4. 相机

task.json 里每一项的 `camera`：

```json
{
  "frame": "PF1",
  "position_mm": [1520.3, 640.0, 2170.9],
  "target_mm": [0.0, 30.0, 0.0],
  "fov_y_deg": 27.0,
  "vertical_correction": false,
  "azimuth_deg": 35.0,
  "elevation_deg": 10.0,
  "distance_mm": 2700.0
}
```

- `position_mm` 和 `target_mm` 是权威值；方位角、仰角、距离只给界面用，可从前两者算出。
- 方位角：0° = 正前方，向产品右侧（+X）转为正。仰角：相机高于目标为正。
- `fov_y_deg`：**输出画面的竖直视场角**。Max 相机的 fov 是水平视场角，执行器换算：`hfov = 2·atan(tan(fov_y/2) · W/H)`，W、H 是该项输出像素尺寸。
- 不允许横滚（地平线保持水平）。
- `vertical_correction` 是每张图独立保存的布尔开关，旧任务缺省为 `false`，新增图片、重置及恢复建议均设为 `false`。
- 镜头校正在绝对俯仰角 ≤ 45° 时生效，角度由权威位置和目标计算。超过 45° 时使用原投影，保留开关设置。网页显示「俯视超过 45° 不做竖线校正」。
- 校正时相机位置与方位不变，水平朝向 `[target.x, position.y, target.z]`，以 PF1 +Y 为上。设 `d` 为相机至目标的水平距离，`s = (target.y - position.y) / d`。对于相机坐标中向上为正的高度 `y` 和向前为正的深度 `z`，投影的竖直归一化坐标为 `(y/z - s) / tan(fov_y/2)`。平移只发生在竖直方向，`tan(fov_y/2)` 保持原值，原目标仍落在画面中心，世界竖线在画面中竖直平行。网页出框检查和自动取景沿用该投影。

## 5. 预览模型

执行器的 `export_preview` 任务（§7）从产品 .max 导出：

- `preview/preview.fbx`：二进制 FBX，Up Axis = Y，单位毫米，不含相机、灯光、动画，不嵌贴图。
- `preview/bbox.json`：

```json
{ "units": "mm", "max_world_bbox_mm": { "min": [x,y,z], "max": [x,y,z] },
  "size_mm": { "x": 1200.0, "y_up": 850.0, "z_depth": 400.0 }, "mesh_count": 37 }
```

网页把读入的模型平移到 PF1（包围盒中心到原点），并核对尺寸：与 `size_mm` 相差超过 1% 时，按 `size_mm` 缩放并在界面显示一条提示。

## 6. task.json

```json
{
  "id": "20261119-183000-ab12",
  "created_at": "2026-11-19T18:30:00+08:00",
  "created_by": "",
  "product_dir": "D:\\...\\BG18-260810",
  "product_name": "BG18-260810",
  "model_max": "D:\\...\\模型\\xxx.max",
  "preview_source": "export | file",
  "preview_fbx": "exchange/tasks/<id>/preview/preview.fbx 或产品目录里的 fbx 绝对路径",
  "preview_rotation_deg": 0,
  "reference_images": ["D:\\...\\参考图\\1.jpg"],
  "items": [
    {
      "no": 1,
      "label": "主图",
      "view": "main | front | side | top | back | custom",
      "orientation": "portrait | landscape",
      "enabled": true,
      "camera": { "...": "见 §4" },
      "parts": { "抽屉1": 0.0 },
      "render_open_state": false,
      "smallshot": { "state": "none | queued | rendering | done | failed", "file": "smallshots/1.jpg", "job_id": "" },
      "final": { "state": "none | queued | rendering | done | failed", "file": "final/1.jpg", "mask": "final/1_遮罩_ID.jpg", "job_id": "" },
      "note": ""
    }
  ],
  "delivered": null
}
```

- `no` 从 1 连续编号，清单里拖动排序后重新编号。多柜任务的入库文件名见 §13。
- `parts` 只记录网页里的开合值（0–1）。本稿渲染一律合上，`render_open_state` 恒为 false，界面上标"开合仅预览"。
- `item_id` 是服务端生成的条目标识，客户端保存整份清单时随条目传回。服务端记录条目的 `appearance_updated_at`，用于核对可复用小样的新旧。
- `variants`、`variant` 和 `variant_group` 是可选的多柜任务字段，见 §13。

### 6.1 查询任务进度

`GET /api/tasks/{id}` 保留原有 `task`、`preview`、`jobs` 和 `task_dir`，并返回 `runner`（同 §8.1）、`progress` 和 `signature`。

```json
{
  "kind": "smallshot",
  "stage": "rendering",
  "done": 1,
  "total": 4,
  "eta_sec": 15
}
```

- `kind` 为 `preview`、`smallshot`、`final`、`calibration` 或 `null`。`stage` 为 `queued`、`rendering`、`thinking` 或 `null`。没有进行中的工作时，`kind`、`stage` 与 `eta_sec` 为 `null`，`done` 和 `total` 为 0。
- 校准优先展示，`done`、`total` 以分析轮次为单位。其余工作优先显示正在运行的作业，再按队列顺序显示。预览导出计作一次，渲染的 `done` 为已处理的张数。
- 服务启动时汇总成功且非假执行器的历史作业，之后按状态变化增量更新。小样和成品用作业实际处理时间除以张数，再按类型取中位数。预览按每次取中位数。历史不足时使用小样 6 秒/张、成品 100 秒/张、预览 8 秒/次。AI 分析按 20 秒/轮。
- `eta_sec` 按剩余张数或轮次乘估计时长。当前图片或 AI 分析已用的时间从这一张或这一轮的估计时长里扣除，最低为 0。校准正在等当前轮小样时，包含当前轮剩余出图时间和剩余轮次的分析时间。
- `signature` 为稳定的 SHA-256 十六进制字符串。哈希覆盖任务回包中的实质内容，排除 `runner`（包含 `age_sec`）及随时间变化的 `progress.eta_sec`。

## 7. 任务文件（jobs）

服务端写 `exchange/jobs/<job_id>.json`（先写 `.tmp` 再改名，写完不再修改）。`job_id` = `<YYYYMMDD-HHMMSS>-<4位随机>`。

### 7.1 export_preview

```json
{ "type": "export_preview", "job_id": "...", "task_id": "...", "created_at": "...",
  "max_file": "D:\\...\\xxx.max",
  "out_fbx": "<绝对路径>\\preview\\preview.fbx",
  "out_bbox": "<绝对路径>\\preview\\bbox.json" }
```

### 7.2 render

```json
{ "type": "render", "job_id": "...", "task_id": "...", "created_at": "...",
  "mode": "smallshot | final",
  "max_file": "D:\\...\\xxx.max",
  "template_max": "...", "template_anchor": { "x_mm": 444.0, "back_y_mm": 461.0, "floor_z_mm": 0.0 }, "render_preset_rps": null,
  "time_limit_sec": 25,
  "items": [
    { "no": 1, "width": 600, "height": 800,
      "camera": { "frame": "PF1", "position_mm": [..], "target_mm": [..], "fov_y_deg": 27.0 },
      "out_image": "<绝对路径>\\smallshots\\1.jpg",
      "out_mask": null }
  ] }
```

- `mode = final` 时 `out_mask` 为 `<绝对路径>\\final\\<no>_遮罩_ID.jpg`，`time_limit_sec` 为 null（不限时，沿用模板设置）。
- 多柜任务的渲染作业附带 `keep_nodes` 和 `variant_name`，见 §13。
- 所有路径都是绝对路径。

### 7.3 状态文件 `<job_id>.status.json`（执行器写，服务端只读）

```json
{ "job_id": "...", "state": "waiting_lock | running | done | failed",
  "started_at": "...", "updated_at": "...",
  "items": [ { "no": 1, "state": "pending | rendering | done | failed", "started_at": "...", "out_image": "...", "out_mask": "...", "message": "" } ],
  "message": "", "log": ["一行一条，最多 200 条"] }
```

- 执行器按 `created_at` 先后一个一个处理；同一时间只处理一个任务。
- 状态文件也先写 `.tmp` 再改名。
- item 的 `started_at` 在开始处理此张时写入，历史文件可缺省。假执行器在状态中写 `runner_version: "fake"`。
- 服务端发现有状态文件就以它为准更新 task.json 里对应项的 `smallshot` / `final` 状态。

## 8. 执行器在 Max 里的规矩

- 每个任务开始前用建模技能的 Max 锁占用 Max：`(fileIn "<建模技能目录>/scripts/pf_max_lock.ms"; ::PFMaxLock.acquire "WhiteBG:<job_id>" "<任务目录>")`。返回不是 OK 就把状态写成 `waiting_lock`，不碰场景，下一轮再试。任务结束（成功或失败）都要释放。
- 任务开始时当前场景必须是空的或已保存的；执行器只 `resetMaxFile #noPrompt` / `loadMaxFile ... quiet:true`，**绝不保存覆盖**任何 .max 文件（模板和产品文件都只读）。
- 渲染流程：载入模板 → 删除模板里的相机 → 合并产品 .max（自动改名重名对象、用合并进来的材质）→ 用合并进来的可见网格算包围盒 → 平移产品贴墙摆放（§2 `template_anchor`）→ 按 PF1 换算建 Target Camera → 设输出尺寸 → 渲染 → 保存成图和遮罩 → 刷新 Max 锁（`::PFMaxLock.touch`）→ 处理下一项 → 全部完成后 `resetMaxFile #noPrompt` → 释放锁。
- 背面视角：某一项相机的方位角绝对值大于 100° 时，渲染这一项前把模板里整个位于产品背面之后的几何体（包围盒最小 y ≥ `back_y_mm`）临时隐藏，渲染完恢复。
- 遮罩：模板的渲染元素里有「遮罩_ID」和「遮罩_线框颜色」两个。成品只要「遮罩_ID」，存为 `out_mask`；「遮罩_线框颜色」的输出放到任务目录的 `final/_extra/` 里，不入库。
- 遮罩：用模板场景里已有的渲染元素（材质 ID 通道）。保存不到遮罩时，该项记 `message`，成图照常保存。
- 出错不弹窗：所有错误写进状态文件，继续下一项或结束任务。
- `runner.log` 按行追加，达到 1 MB 轮换为 `runner.log.1`，最多保留 `.1` 至 `.3`。原子写入的 JSON 和日志轮换均停止生成 `.bak`，现有旧备份保留。

### 8.1 心跳与一键启动

执行器启动时立刻写入 `exchange/runner.heartbeat.json`，之后约每 3 秒继续写入，包括空闲、等待 Max 锁和同步渲染期间。文件使用同目录临时文件原子替换。停止时写同样的字段并加上 `"stopped": true`。

```json
{"ts":"2026-11-20T08:00:00+00:00","epoch":1795161600,"pid":1234,"busy":false,"job_id":null,"runner_version":"0.2"}
```

处理作业时 `busy` 为 true，`job_id` 为当前作业号。开发假执行器走同一协议，`runner_version` 为 `fake`。

`GET /api/runner` 返回以下字段。

```json
{"online":false,"age_sec":null,"busy":false,"job_id":null,"max_running":false,"can_start":true}
```

- 心跳存在且未标记 `stopped`，并且距当前时间不超过 12 秒时，`online` 为 true。`age_sec` 为心跳距当前的秒数，无法读取时为 `null`。离线时 `busy` 为 false，`job_id` 为 `null`。
- `max_running` 表示本机有 `3dsmax.exe` 进程，探测结果缓存 5 秒。只有执行器离线、Max 未运行且配置的可执行文件存在时，`can_start` 才为 true。

`POST /api/runner/start` 校验来源，按下列优先顺序处理失败。

| HTTP 状态 | 条件 | `error` |
|---|---|---|
| 409 | 执行器在线 | 执行器已经在运行。 |
| 409 | Max 已打开 | Max 已经打开，但白底执行器没在运行。请在 Max 里运行「启动白底执行器.ms」（拖进视口即可）。 |
| 400 | 找不到 Max 可执行文件 | 找不到 3ds Max：请在 config.json 的 max_exe 填写 3dsmax.exe 的完整路径。 |

成功时独立启动 `[max_exe, "-U", "MAXScript", <max/启动白底执行器.ms 绝对路径>]`，立即返回 `{"started":true}`。命令行启动和拖入脚本共用同一个入口。

## 9. 入库（网页里点"确认入库"）

- 目标：`<product_dir>\sku\`（不存在就创建）。
- 单柜任务的每个启用且成品完成的项复制为 `<no>.jpg` 和 `<no>_遮罩_ID.jpg`。多柜任务见 §13。
- 目标已有同名文件：先把旧文件移动到 `sku\旧版\<YYYYMMDD-HHMMSS>\`，再放新文件。**不删除任何文件。**
- 同时写 `sku\白底清单.json`：每项的编号、标签、视角、横竖、相机、模型路径、任务号、入库时间。已有旧的 `白底清单.json` 同样先移进旧版目录。
- 入库结果写回 task.json 的 `delivered`。

## 10. 起草规则（本稿用固定规则代替 AI）

默认焦距相当于 50 mm（`fov_y_deg = 27`），四周留白 10%。

| 编号 | 标签 | view | 方位角 | 仰角 | 条件 |
|---|---|---|---|---|---|
| 1 | 主图 | main | +35° | 12° | 总是 |

- 新任务和 `/draft` 都只起草一项主图。识别出的抽屉和柜门在主图的 `parts` 中初始化为闭合。`initial:true` 只更新尚未修改的默认主图。
- 横竖版：按该项在画面里的投影宽高比自动选，宽/高 > 1.3 用横版，否则竖版；人可以改。
- 距离：按 §4 的视场角和留白自动取景（包围盒 8 个角点全部落在留白线内的最小距离）。

## 11. AI 校准（v0.2，2026-11-20 用户定）

目的：让 AI（Codex）对照小样与参考调整材质颜色、光泽反射、玻璃折射和灯光，确认后正式渲染沿用。

用户已定：
- **对照依据**：历史确认过的白底（同材质、同产品优先）＋主图参考图（只借鉴灯光方向和明暗感觉，不对颜色）。
- **允许改**：材质颜色和贴图色调、光泽和反射强度、玻璃折射和透明度、灯光强度和方向、环境反射贴图强度。**渲染预设参数一律不动。**
- **时机**：默认通过 `POST /api/tasks/{id}/calibration` 发送 `{"action":"start","user_note":"可选说明"}` 手动开始。配置开启 `calibration_auto` 后，任务的 `ai_calibration.enabled` 决定出小样时是否自动开始。
- **改动去向**：只记在 task.json，渲染时临时套用；**不改、不另存任何 .max**。

### 11.1 流程

```
开始校准 → 复用合格的小样，或让执行器重新出第 0 轮小样＋材质分区图＋场景清单
→ 服务端算每种材质的颜色统计 → 调 Codex（看图＋清单＋统计＋参考）→ 返回调整
→ 服务端按 11.4 的范围校验、截断 → 需要调整且轮数 < max_rounds：带新调整再出一轮小样
→ Codex 判定"已准"或到轮数上限 → 状态"待你确认"
→ 使用者选"用这一轮"/"撤销 AI 调整"/"再调一轮（可附一句话要求）"
→ 提交渲染时把已采用的调整（accepted_overrides）带进 final 任务
```

- 所有当前启用条目必须对应同一个已完成的小样作业，完成时间晚于各条目的最后一次画面或打光改动，且有齐全的小样、场景清单、分区图和图例，才直接进入第 0 轮分析。普通小样缺少分析文件时按第 0 轮校准小样排队。
- 第 0 轮之后最多再运行 `calibration_max_rounds` 轮，默认 3。同一时间只跑一个校准。`retry`、`accept`、`undo` 继续沿用原有操作。
- 每轮的输入、输出、日志都存在 `exchange/tasks/<id>/calibration/round-<n>/`。
- 校准期间 `PUT /api/tasks/{id}` 可更新条目的相机、画幅和开合预览值。改打光或清单结构（增删、排序、启用勾选）返回 409 `AI 校准进行中：结束后再改打光或清单。`。提交小样或正式渲染返回 409 `AI 校准进行中，结束后再出图。`。

### 11.2 执行器新增（render 任务）

render 任务新增字段：
- `overrides`：11.4 的对象，可为 null。执行器合并产品、摆好位置后、建相机前套用；不保存场景。
- `want_manifest`：true 时额外输出：
  - `calibration/round-<n>/scene_manifest.json`：
    ```json
    { "materials": [ { "name": "摩卡红橡", "class": "CoronaPhysicalMtl", "objects": 12,
        "kind": "wood|solid|glass|metal|other",
        "color": [r,g,b] 或 null, "bitmap": "文件名或 null",
        "params": { "可调属性名": 当前值 } } ],
      "lights": [ { "name": "Corona Light001", "intensity": 15.0, "pos_mm": [x,y,z] } ],
      "environment": { "reflect_map": "贴图名或 null" },
      "product_center_mm": [x,y,z], "unsupported": ["说明"] }
    ```
  - 每张小样一张**材质分区图** `calibration/round-<n>/regions/<no>.png`：渲染前把每个产品对象的线框色临时设成"它的主材质"对应的唯一颜色，用模板的「遮罩_线框颜色」元素输出为 **PNG**（不能用 JPG，压缩会混色）；同目录 `regions/legend.json` 写 `{ "#RRGGBB": "材质名" }`。多维子材质的对象按第一个子材质记，并写进 manifest 的 `unsupported`。
- 材质名是 Max 里的材质名（建模技能的预设名，如 摩卡红橡、奶油白、胡桃木、幻影灰、纯黑、长虹玻璃、银色五金）。
- `kind` 判断：名字或类含 玻璃/glass/长虹 → glass；含 金属/五金/metal/chrome/金色/银色 → metal；有位图且名字含 木/橡/胡桃/oak/walnut → wood；纯色 → solid。

### 11.3 服务端颜色统计（每轮）

用材质分区图对每张小样算每种材质：面积占比、平均 sRGB、平均 Lab、亮度标准差、高光像素占比（亮度 > 245）。写 `round-<n>/stats.json`，同时给 Codex 和界面用。分区图里颜色与 legend 的差在 ±6 以内算同一材质。

### 11.4 调整对象 overrides（Codex 只能输出这个范围，服务端截断越界值）

```json
{
  "materials": {
    "<材质名>": {
      "hue_shift_deg": 0,        // −15 … 15
      "saturation_mul": 1.0,     // 0.6 … 1.4
      "brightness_mul": 1.0,     // 0.5 … 2.5（2026-11-20 放宽；执行器写到色彩校正的 exposure = log2(倍率) 档）
      "contrast_mul": 1.0,       // 0.7 … 1.3
      "reflection_mul": 1.0,     // 0 … 2
      "glossiness_delta": 0.0,   // −0.3 … 0.3（粗糙度类属性按 −delta 处理）
      "refraction_mul": 1.0,     // 0 … 1.5，只对 glass
      "ior": null,               // 1.3 … 1.8 或 null，只对 glass
      "opacity_mul": 1.0         // 0.5 … 1.0，只对 glass
    }
  },
  "lights": { "intensity_mul": 1.0, "rotate_deg": 0, "per_light": { "<灯名>": { "intensity_mul": 1.0 } } },
  "environment": { "reflection_strength_mul": 1.0 },
  "items": { "<no>": { "lights": { "rotate_deg": 0 } } }
}
```

- `lights.intensity_mul` 0.5 … 1.6；`per_light.*.intensity_mul` 0.5 … 2；`rotate_deg` −90 … 90（绕产品中心竖轴转动全部模板灯，正值为从上往下看逆时针）；`environment.reflection_strength_mul` 0.5 … 2。
- `items.<no>.lights.rotate_deg` 只用于让主图的光向跟参考图，覆盖全局 `rotate_deg`。
- 执行器套用方式：颜色类用 Corona 色彩校正贴图包住原漫反射/基础色贴图（纯色就直接按 HSV 改色）；反射、光泽、折射、IOR、透明度按材质类的实际属性名改（Legacy 与 Physical 分别映射，属性不存在就跳过并记到状态日志）；灯光改强度和位置/朝向；环境反射在模板的反射覆盖贴图外再包一层色彩校正调亮度。都只在内存里改。

### 11.5 AI 引擎调用（服务端）

- 「AI 设置」选择 `codex`（默认）或 `openai_api`。两种引擎共用图片选择、提示词规则、本地 schema 校验和调整套用。校准弹层显示当前引擎和模型。轮次记录的 `usage` 和 `usage.json` 保存实际引擎、模型、调用耗时和可取得的 token 计数。
- 用本机 `codex exec`：`--skip-git-repo-check --ephemeral -s read-only -m <model> -c model_reasoning_effort="<effort>" --output-schema <schema.json> -o <result.json>`，图片用 `-i`，提示词从标准输入传。`CODEX_HOME` 取 config（默认 `%USERPROFILE%\.codex-exec`，不存在就用 `%USERPROFILE%\.codex`）。模型默认 `gpt-6.1-sol`、强度 `medium`，单轮超时 15 分钟。config 里可以把命令换成假 Codex（测试用）。
- 中转站功能需要 Python 3.13 x64，使用 `pylibs/` 内置的 OpenAI 官方 Python SDK。复制工作台时须一并复制整个 `pylibs/` 文件夹。SDK 使用已保存的 Bearer API Key 请求 `GET {base_url}/models` 和 `POST {base_url}/chat/completions`，关闭重定向并使用用户填写的接口路径。`system` 附角色和规则，`user` 附本轮数据及按清单顺序排列的 `image_url` data URL。图片最长边 1600 像素，普通图片使用质量 92 的 JPEG，材质分区保留 PNG 和原色。
- 中转站先试严格的 `json_schema`。若收到与结构化格式有关的 HTTP 400，依次尝试 `json_object` 和在提示词附 schema 的文本 JSON。按接口地址和模型记住通过校验的级别。剥掉 Markdown 代码围栏后解析，格式或 schema 校验失败重试一次。推理强度可选省略、`low`、`medium`（默认）和 `high`，若 HTTP 400 拒绝 `reasoning_effort`，去掉该字段重试并记住。单次请求超时 180 秒，网络错误、429 和 5xx 等待 2 秒、6 秒各重试一次。
- 引擎、地址、模型、推理强度及探明的兼容性保存到 `ai_settings.json`。DPAPI 按本机 Windows 账号加密密钥，文件位于 `%APPDATA%\whitebg-studio\ai_key.bin`。换电脑后，须在「AI 设置」中重新填写 Key。
- `GET /api/ai/settings` 返回 `engine`、`base_url`、`model`、`reasoning_effort`、`key_set`、`key_tail`、`structured_mode` 和用于界面显示的 `codex_model`。`POST /api/ai/settings` 保存设置和非空的 `api_key`，`clear_key: true` 清除已保存密钥。所有写入和测试请求校验来源。
- `POST /api/ai/models` 用已保存密钥获取 `{base_url}/models`，返回排序后的 `models`，列表包含 `gpt-6.2-sol` 时置顶。`POST /api/ai/test` 用 64×64 纯色图和小 schema 检查接口，返回 `connected`、`vision`、`structured_mode`、`latency_sec`、`model`，失败时附 `reason`。拉取模型和测试可使用表单中的地址和模型等非机密字段。
- 两种引擎每轮收图总数 ≤ 12，包含本轮小样、材质分区图及各类参考。超出预算时小样和分区图分别拼图，保留启用项。颜色与质感优先对照本任务参考，其次是本产品 `sku\` 里的白底（≤ 3 张）、参考库标注裁图及已认可或候选图。剩余参考包含风格参考（≤ 2 张）和主图参考图（≤ 1 张，供对照灯光方向和明暗）。
- 文字输入：scene_manifest、stats、上几轮的 overrides 和结论、使用者附加的一句话、11.4 的范围说明。
- 两种引擎使用同一输出 schema。`verdict` 为 `ok` 或 `adjust`，`summary` 为一两句中文，`notes` 含对象名称、观察与改动，`confidence` 介于 0 和 1。`overrides` 提供相对原场景的**完整新值**。
- 失败（超时、额度、登录失效、输出不合 schema）：该轮记失败，停在"AI 校准失败，可直接用当前小样"，不影响正常出图。
- 输出用数组列出材质、单灯和主图条目。对象列全 `properties`，均为必填并设置 `additionalProperties: false`。服务端收到后转换成 §11.4 的字典形式再保存和下发。
  ```json
  { "verdict": "ok|adjust", "summary": "...", "confidence": 0.0,
    "notes": [ { "target": "...", "observation": "...", "change": "..." } ],
    "overrides": {
      "materials": [ { "name": "材质名", "hue_shift_deg": 0, "saturation_mul": 1, "brightness_mul": 1, "contrast_mul": 1,
                       "reflection_mul": 1, "glossiness_delta": 0, "refraction_mul": 1, "ior": null, "opacity_mul": 1 } ],
      "lights": { "intensity_mul": 1, "rotate_deg": 0, "per_light": [ { "name": "灯名", "intensity_mul": 1 } ] },
      "environment": { "reflection_strength_mul": 1 },
      "items": [ { "no": 1, "rotate_deg": 0 } ] } }
  ```
  `ior` 的类型是 number 或 null；其余数字必填。

### 11.8 与 §12 打光的关系

任一启用条目的光方向生效时，AI 校准提示词写明使用者设定的光向和描述，要求按该光向保留明暗关系。服务端将全局与各项的 `lights.rotate_deg` 置为 0，将 `lights.intensity_mul` 限制在 0.85–1.15，并清空 `per_light`。所有启用条目都使用模板光时，AI 可按原有范围调整灯光。启用条目的实际打光改变时清空已采用的调整。材质和环境反射照常可调。

### 11.7 2026-11-20 实测得到的 Max 事实（DSG91 电视柜，中文版 Max 2025 + Corona 12.2）

- **材质类名是中文且不能用类对象比较**：`classOf` 显示为「Corona传统材质」「_Corona物理材质」「Corona光线切换」，`classOf m == CoronaPhysicalMtl` 对「_Corona物理材质」返回 false。**一律按属性是否存在判断类型**：
  - 传统材质（Legacy）：`colorDiffuse`、`texmapDiffuse`、`levelDiffuse`、`levelReflect`（0–1）、`colorReflect`、`reflectGlossiness`（0–1，越大越亮滑）、`fresnelIor`、`levelRefract`、`colorRefract`、`refractGlossiness`、`IOR`、`levelOpacity`、`colorOpacity`。
  - 物理材质（Physical）：`baseColor`、`basetexmap`、`baseLevel`、`baseroughness`（`roughnessMode=1` 时是粗糙度，越大越糙；为 0 时按光泽度处理）、`baseIor`、`refractionamount`、`opacityLevel`、`opacitycolor`、`metalnessMode`。**没有反射强度属性**：`reflection_mul` 按菲涅耳 F0 = ((n−1)/(n+1))² 乘系数后反算 `baseIor`（F0 上限 0.2，n 下限 1.01）。
  - 光线切换（RaySwitch）：有 `giMtlOn`、`reflectMtlOn`、`refractMtlOn`、`directMtlOn` 和对应的子材质属性（属性名以 `Mtl` 结尾、值为材质）。调整套用到它所有不同的子材质上；`kind`、颜色等按 direct 子材质（没有就第一个子材质）判断；清单、分区、调整里都用**外层材质名**（如 摩卡红橡）。
  - 多维材质（Multimaterial）：`materialList`；分区图按该对象**面数最多的材质 ID** 对应的子材质记。
- 例：奶油白 = 传统材质纯色（`colorDiffuse` 149,126,97，`reflectGlossiness` 0.78）；摩卡红橡 = 光线切换，子材质是物理材质，`basetexmap` 是 Max 的色彩校正贴图包木纹，`baseroughness` 0.7，`baseIor` 1.507。
- **分区图颜色会被 gamma 编码**：线框色 (32,128,224) 渲出来是 (99,187,241)，即约 255·(c/255)^(1/2.2)。服务端统计时每个 legend 颜色同时生成"原色"和"gamma 编码色"两个候选，像素归到距离最近的候选，距离 ≤ 30 才算该材质；黑色背景不计。

### 11.6 参考库 `refs/`

- `refs/library.json`：`{ "materials": { "<材质键>": { "keywords": [...], "images": [ { "file": "refs/<键>/<n>.jpg", "source": "原图路径", "status": "candidate|approved|rejected" } ] } }, "style": [ { "file": ..., "source": ..., "status": ... } ] }`。
- 初始材质键和关键词：摩卡红橡［摩卡红橡、红橡］、胡桃木［胡桃］、奶油白［奶油白］、北欧橡木［北欧橡木、橡木］、幻影灰［幻影灰］、纯黑［纯黑、黑色、黑橡］、长虹玻璃［长虹］。产品材质名包含某个关键词就对应这个键。
- 初始化工具 `tools/build_ref_library.py`：扫描 config `history_roots`（默认 `D:\产品资料`）里 `sku`/`SKU`/`白底` 文件夹的白底主图（排除 TD、遮罩、通道），文件名含关键词的作为该材质的候选；另随机取 12 张作风格候选。缩到最长边 1600 复制进 `refs/`，原图只读。
- 网页"参考库"页：按材质分组显示候选，使用者点"认可/不用"；可拖入图片加到某个材质。AI 优先用 approved，没有 approved 时用 candidate 并在提示里说明可信度低。

### 11.9 本任务材质参考

- `task.ai_calibration.task_refs` 保存 `{id, file, name, created_at, in_library, regions}`，1.7.1 起还有 `library_file`（收进参考库的那张标注图）和 `library_synced`（已收进参考库）。`file` 相对于任务目录，原图和裁图分别保存在 `calibration_refs/` 与 `calibration_refs/crops/`。每个框保存 `{material, material_source, box:[x,y,w,h]}`，坐标归一化。任务详情回包包含最新可用场景清单中的 `scene_materials`。
- `POST /api/tasks/{id}/calibration-refs/upload?name=…[&from=refs/annotated/…jpg]` 接收不超过 50 MB 的图片字节并返回 `{entry}`；带 `from`（从参考库点选的标注图）时原样拷那张标注图，不重新压缩。`PUT /api/tasks/{id}/calibration-refs/{ref_id}` 保存 `{regions}` 并返回 `{entry}`（旧的 `in_library` 字段不再需要，传了也忽略）。`DELETE` 删除任务里的图和裁图。三种写入均校验来源，并用服务锁保护任务记录。
- 每轮排队时固定该轮所用的任务参考图片和框。匹配采用场景原名精确匹配、参考库分组关键词匹配、任务新材质名称包含匹配。每个场景材质最多取 2 张任务裁图。无匹配的新材质裁图作为「其他参考」，未画框的整图作为「整体参考」。每张选中的裁图附平均 D65 Lab。该轮的 `task_refs_used` 保存实际使用的 `{id,region_index}`，整图序号为 `null`，从 0 开始计框。
- ~~勾选「也加入参考库」保存时，参考库 `annotated` 保存独立副本，副本只含 `material_source == "library"` 的框。~~ 1.7.1 起改为每张都自动收进参考库，见 §15.14。任务参考在该任务的中间文件清理时释放，参考库里的那张保留。

## 12. 打光（v0.3，2026-11-20 用户定）

默认使用 Max 模板灯光。开启「光方向」后，工作台设定的光向用于白底渲染、AI 校准（§11.8）和入库光向参考。一个产品使用一套设置，单张可以独立设置。

### 12.1 数据（task.json）

```json
"lighting": { "enabled": false, "azimuth_deg": null, "elevation_deg": 35, "intensity": 1.0, "contrast": 0.5 },
"items": [ { "no": 1, "lighting": null } ]
```

- `enabled` 为布尔值，默认 `false`，旧任务和单张设置缺省时同样视为 `false`。只有 `enabled` 为 `true` 且 `azimuth_deg` 有值时，光方向才生效。关闭后保留四项参数。
- `azimuth_deg` 为主光的 PF1 水平方位，范围 −180…180。0 从正前方照来，正值从产品右侧（+X）照来。
- `elevation_deg`：主光高度 5…85（0 水平，90 正上方）。
- `intensity`：整体亮度 0.6…1.5。
- `contrast`：明暗对比 0…1。补光比例 `fill_ratio = 1 − 0.75 × contrast`（contrast 0 → 补光 1.0，很平；1 → 0.25，暗面很暗）。
- 单张 `items[].lighting` 不为 null 时整套覆盖产品统一的光；为 null 用产品统一的光。
- render 任务每项带实际生效的 `lighting`。使用模板灯光时向执行器发送 `azimuth_deg: null`。

### 12.2 一句话描述（网页和服务端用同一规则，各写一份并测试）

- 水平：|az| < 20 → "正前方"；20–70 → "左前方/右前方"（az < 0 为左）；70–110 → "左侧/右侧"；> 110 → "左后方/右后方（逆光）"。
- 高度：el < 20 → "低角度"；20–55 → "斜上方"；> 55 → "高处（接近顶光）"。
- 对比：< 0.3 → "柔和"；0.3–0.7 → "中等"；> 0.7 → "强烈"。
- 亮暗面：主光在左 → "左侧受光较亮、右侧偏暗"；在右反之；正前方 → "正面受光、两侧略暗"；el > 55 再加 "顶面最亮"。
- 例："主光从左前方斜上方约 35° 照来（水平 −45°，高 35°）；左侧受光较亮、右侧偏暗；明暗对比中等。"（"约 N°"按 5° 取整）
- **主光强度标定（2026-11-20 Claude 实测 DSG91）**：`config.key_light_intensity = 12` 时，左前光/右前光、对比 0.8 的整体亮度与模板原光一致（灰度均值 225 / 224 对模板 224）；20 会偏亮发白。右前光 60° 时右侧板明显变亮、正面变暗，正面与侧板亮度差从左前光的 33 降到 13，明暗方向确实跟随。

### 12.3 Max 执行器怎么跟随（每一项渲染前，渲完恢复）

`lighting.azimuth_deg` 为 null：什么都不做（模板原光）。否则：

1. **临时主光**为对准产品中心的 Corona 矩形灯，沿 PF1 方位、仰角放置。名义距离为 `key_distance × 产品包围盒对角线`，默认系数 2.5。边长系数按对比从 `key_size_max=0.50` 线性插值到 `key_size_min=0.25`。主光继承 Corona 模板灯颜色，可见性字段沿用原规则。
2. **布景限位**使用合并产品之前记录的模板几何包围盒。每面内缩 300 毫米加半块灯板的边长，沿产品中心到光源的射线取出盒距离，主光放在它与名义距离中的较小值。产品中心在内缩盒外时，若可求原布景的出盒距离，按其一半进一步限位。111(2022) 实测右前 45° 的名义灯位 X 为 2761 毫米，越过模板右边界 2631 毫米后画面几乎全黑。主光收近时将强度乘以 `(实际距离 / 名义距离)^2`，抵消照度的距离平方反比变化。
3. **补光**按 `fill_ratio = 1 − (1 − fill_min) × 对比^fill_gamma` 计算，默认 `fill_min=0.10`、`fill_gamma=1`。模板灯乘以补光比例及整体亮度。背景灯可额外保留 `(1 − fill_ratio) × bg_fill_keep`，默认 `bg_fill_keep=0`。主光的补光损失补偿为 `1 + key_compensation × (1 − fill_ratio)`，默认 `key_compensation=111`。主光基础强度 `key_intensity=1`，再乘整体亮度、补光损失补偿和距离补偿。省略调参中的主光基础强度时可沿用显式的 `config.key_light_intensity`，两处都缺省时使用代码默认值 1。AI 校准的 `lights.intensity_mul` 再整体乘到主光与补光，范围见 §11.8。
4. **环境光**按 `1 − (1 − env_gi_min) × 对比` 计算，默认 `env_gi_min=0.15`。当前 Corona 没有独立的环境 GI 数值倍率。配置通过 `source_color: BG_COLOR` 调暗白色环境的 RGB，使用 `direct_enabled: BG_OVERRIDEDIRECT` 和 `direct_color: bg_colorDirect` 保留直接可见的原背景色。实测调暗 `BG_COLOR` 使方盒背光面的最暗处从约 60 降到 35，修改 `bg_colorList` 无效。如果存在已绑定的 `gi_multiplier`，仍可沿用数值倍率路径。执行器按字面回读并恢复颜色，缺少所需属性时记录原因并跳过环境调整。
5. 每项渲染结束或失败后恢复模板灯、恢复环境属性并回收主光和临时目标。恢复失败会停止作业。校准作业的 `scene_manifest.json` 记录主光的位置、尺寸、距离、强度、补光比例和实际使用的 GI 倍率。
6. `lighting_tuning.probe=true` 时，每个渲染作业在载入模板及渲染预设后向 `runner.log` 输出探测信息。渲染器属性通过 `getPropNames` 枚举，日志包括环境、背景、GI、曝光等相关属性，以及模板灯的类、强度、尺寸、世界位置与朝向、墙和地面的世界包围盒。定稿配置关闭探测。

### 12.4 网页

- 右栏"画面调节"下面加一块**"打光"**：
  - 顶部是「这一张单独打光」和「光方向」开关。前者切换当前编辑的是产品统一设置还是本张设置。当前范围的「光方向」关闭时只显示这两个开关。
  - 打开「光方向」时恢复保存的参数。此前没有方向时使用左前 −45°、高 35°、亮度 1.0、对比 0.5。
  - **方向圆盘**：俯视示意，中间是产品的矩形轮廓（按产品宽深比例，正面朝下并标"正面"），外圈一个可拖动的小太阳，拖到哪儿光就从哪儿照来；圆盘下方写当前方位数值。
  - 滑杆：**光的高度**（5–85°）、**整体亮度**（0.6–1.5）、**明暗对比**（0–1，两端标"柔和/强烈"）。
  - 预设按钮：**左前光**（−45°, 35°）、**右前光**（45°, 35°）、**正面光**（0°, 30°）、**顶光**（−20°, 70°）。
  - 一句话描述（§12.2）实时显示。
- **画面预览实时显示明暗**。从任务回包的最近提交快照取得 `lighting_tuning` 和基础主光强度。首次提交前缺少快照时使用代码默认值。网页按 §12.3 的曲线计算模板补光、环境光和主光，使用 three.js 光强换算和尺寸与距离之比近似阴影软硬。地面用 `ShadowMaterial` 接影，Corona 墙地背景照度由小样评估。不打光时沿用原预览灯。开关**"素模看明暗"**临时使用浅灰哑光材质。
- 网页方向灯照度与主光强度及面光近似角面积相乘。Max 收近灯板时按距离平方减弱强度，网页用名义距离近似等效照度。环境半球光与两方向模板补光采用实测换算系数。打光时对明确标记为纯黑、哑黑的 FBX 节点使用近似深色底色。素模和不打光模式沿用各自原有底色。
- **光向示意图**在提交小样、正式渲染、AI 校准或入库前，仅为光方向生效的启用条目生成。PNG 宽 1200，显示该项相机下的素模和当前光，包含阴影、方向描述和太阳箭头，存于 `exchange/tasks/<id>/lighting/<no>.png`。

### 12.5 服务端

- `lighting` 与 `items[].lighting` 的读写、合法性校验（范围截断，非法 az 置 null）。
- render 任务按 §12.1 下发实际生效的 `lighting`。task.json 保留配置参数，并记录每项实际生效的 `lighting_text`。
- 每次提交小样、正式渲染或校准小样时重新读取 `config.json`，原样下发 `lighting_tuning`，同时下发当前 `key_light_intensity`。任务回包附带最近提交的两项快照。旧作业与缺项沿用代码默认值。
- AI 校准：按 §11.8 改提示词和截断；给 Codex 的图里加上本项的光向示意图（标明"使用者设定的光向，只看方向和明暗"），替代原来"主图参考图看光向"的作用（主图参考图仍可附带）。
- **入库**只对光方向生效的条目检查示意图并复制到产品目录的 `光向参考\<no>.png`，已有同名图先归档到 `光向参考\旧版\<时间>\`。`白底清单.json` 每项记录 `lighting` 和 `lighting_text`。使用模板灯光的条目记录 `enabled: false` 和「模板默认灯光」。

### 12.6 扫参小样

- 在项目目录运行 `python tools/light_sweep.py --dry-run` 可离线查看默认任务 `20261120-235812-366c` 的组合。用 `--task` 更换仅启用第 1 项的测试任务，用 `--cases` 传入 JSON 数组，每项包含 `name`、`lighting`，可附 `lighting_tuning` 覆盖本组参数。
- 正式运行时脚本逐组保存第 1 项的打光设置、写配置、提交小样并等待对应作业结束。扫参打开 `capture_mask`，允许从同次渲染的位图补存成图与遮罩。脚本复制当轮成图及 `smallshots/_extra/1_遮罩_ID.jpg`，生成 `metrics.csv` 和 `contact-sheet.jpg`，退出时恢复原配置调参。测试任务保留最后提交的第 1 项打光。
- 主指标为右侧三分之一产品像素的平均 8 位灰度除以左侧三分之一产品像素的平均灰度，即 `right_left_ratio`。使用整张画幅分区、当轮 ID 遮罩排除背景。此机位两侧主要对应同一木饰面的门面和右侧大板。旧的最大两个 ID 亮暗比 `face_ratio` 保留为附加列，用于检查材质差。背景均值取上部 20% 的背景像素。
- 111(2022) 主图的 600×800 小样实测产品灰度均值约为 88（不打光）、101／103／87（右前 45° 的 0.25／0.5／1.0）、99（左侧 80° 的 1.0）、131（左前 45° 的 0.5）和 82（右前 45° 的 0）。背景约 240.02，单张约 7 秒。右前光主要照亮右板，左侧光主要照亮门面。
- Corona 输出的白色画面背景来自直接可见环境。模板没有接影地面，成图中的白底不会呈现地面投影。网页为方向提示使用的 `ShadowMaterial` 地面属于预览。

## 13. 多柜子（款式）

网页使用整场景预览 FBX 识别柜子。每个柜子对应一个独立任务，有自己的清单、相机、小样、正式图、AI 校准和入库记录。

- `variants` 是本场景的款式列表，每项为 `{id, name, nodes, bbox_mm:{min:[x,y,z], max:[x,y,z]}}`。`nodes` 使用 Max 节点原名，`bbox_mm` 使用网页计算的 PF1 毫米坐标。
- `variant` 指向当前柜子的款式数据。识别出单柜时，提交识别的任务保存 `variant:null`。
- `variant_group` 是兄弟任务共享的组号。首次保存多个款式时默认为当前任务号。
- 上述字段可选。旧任务沿用现有的预览、渲染和入库流程。`GET /api/tasks` 每项附带 `variant_name`、`variant_group`、`variant_count`，缺省分别为 `null`、`null`、`0`。

| 接口 | 请求和行为 |
|---|---|
| `POST /api/tasks/<id>/variants` | 请求 `{variants,active}`，`active` 必须在列表中。同步更新同组任务的列表，已有兄弟任务按自己的款式编号更新。返回 `{task,siblings,orphans}`，后两个数组的元素为 `{task_id,variant_id}`，分别列出兄弟任务及其中已从识别列表移除的任务。已移除款式的兄弟任务保留原 `variant`，重新识别到同一编号时会恢复同步。同组有排队、渲染或进行中的 AI 校准时返回 409。若所选活动款式已有兄弟任务，先切换到该任务再提交识别结果。 |
| `POST /api/tasks/<id>/switch-variant` | 请求 `{variant_id}`，返回 `{task_id}`。有对应任务时直接返回其任务号。新建时复制产品、模型、参考图、任务打光、输出档位和 AI 校准开关，起草一张主图。源任务的 `preview/preview.fbx` 和 `preview/bbox.json` 复制到兄弟任务目录，任务详情直接返回 `preview.state:"done"` 和整场景包围盒。源预览未就绪时返回 409「请等预览模型准备好再切换柜子」。 |
| `POST /api/tasks/<id>/variant-name` | 请求 `{variant_id,name}`，同步更新本任务和兄弟任务的 `variants` 和 `variant`，返回 `{task}`。 |

识别请求接受 1–20 个款式。`id` 为 1–40 位英文字母、数字或下划线，同列表内唯一。`name` 清除 Windows 文件名非法字符并去掉首尾空白及末尾点号后须有 1–40 字，可用于入库文件名。同组款式名须互异，忽略大小写。每款须有 1–2000 个节点名，每个为 1–255 字，不同款式的节点名不能重叠。包围盒的两端各须有三个有限数值，每个轴的最小值不得大于最大值。

预览只由原任务提交一次 `export_preview`，使用原有整场景导出协议。带 `variant` 的小样、正式图、AI 校准各轮及材质分区统一使用渲染作业的 `keep_nodes` 和 `variant_name`。执行器合并产品后按原始节点名筛选几何体，清理失去全部成员的组头和辅助父节点，再以当前柜体的包围盒中心为 PF1 原点完成贴墙摆放和取景。无匹配可渲染几何体时，作业失败并提示重新识别柜子。

多柜任务在入库目录使用 `<款式名>_<编号>.jpg`、`<款式名>_<编号>_遮罩_ID.jpg` 和 `白底清单_<款式名>.json`。打光生效时将示意图复制到产品目录的 `光向参考\<款式名>_<编号>.png`。多柜清单中的 `image_file`、`mask_file` 和 `lighting_file` 记录对应的入库文件名，未启用光向时 `lighting_file` 为 `null`。单柜任务沿用 §9 的文件名。历史兄弟任务保留其款式名前缀。任务内的 `final/<编号>.jpg` 等文件保持原名。

## 14. 内网渲染机（v1.4，2026-11-22 用户定）

目的：自己电脑不装 3ds Max，选公司内网里另一台电脑的 Max 出图。本机的任务、清单、预览、AI 校准、入库流程都不变，只是作业交给渲染机执行。

### 14.1 两边的角色

- **渲染机**：在「本机设置 → 渲染机」点「设为渲染机」。会装好 Max 插件、在当前用户的 Run 键登记 `PandaStudio.exe --render-node`（源码运行为 `pythonw desktop.pyw --render-node`）、以管理员身份加防火墙规则 `Panda Studio Render Node`（TCP 8775、UDP 8776，只放行 `LocalSubnet` 和三段私有地址），并立即启动后台服务。状态存在安装目录的 `render_node.json`（`enabled`、6 位 `code`、已配对电脑的令牌 SHA-256）。「停止共享」把 `enabled` 改为 false，服务几秒内自己退出，并删掉 Run 键；防火墙规则保留。卸载时安装程序删掉 Run 键。
- **本机**：顶部「渲染位置」选择本机 Max 或某台渲染机。选择存在 `remote_nodes.json`（`target` 为 null 表示本机；`nodes` 按 `主机:端口` 记名字和令牌）。

### 14.2 发现与配对

- 本机向各网段广播地址（按 /24 估算）、`255.255.255.255` 和 `127.0.0.1` 的 UDP 8776 发送 `PANDA-STUDIO-DISCOVER/1`，渲染机回 JSON：`service`=`panda-studio-node`、`protocol`=1、`name`（电脑名）、`port`、`runner{online,busy,max_running}`、`queue`、`template`。连过的渲染机另用 `GET /node/info` 逐个探测（跨网段时手动输入电脑名或 IP）。
- 配对：`POST /node/pair {code, client}` → `{token, name}`。同一来源 10 分钟内错 5 次返回 429。之后的请求带 `Authorization: Bearer <token>`；渲染机只接受私有、本机和链路本地地址的请求。

### 14.3 作业转发

1. 渲染位置是渲染机时，`Studio.write_job` 先写状态文件 `{state: waiting_lock, remote: {host, port, name, phase: pending, files: {}}}`，再写作业文件。本机执行器（`wb_core.pending_jobs`）和假执行器看到状态里有 `remote` 就跳过。
2. 本机后台线程（每 1.5 秒）把 `phase=pending` 的作业发出去：
   - 模型包：产品文件夹（`.max` 不在产品文件夹里时用其所在文件夹），跳过 sku、视角图、参考图、旧版、autoback、预览和出图文件夹、别的模型和压缩包、备份；上限 3 GB、20000 个文件。指纹 = 选中 `.max` 的相对路径 + 每个文件的（相对路径、大小、修改时间）的 SHA-256。先 `HEAD /node/bundles/<指纹>`，没有才 `PUT` zip。渲染机按来访电脑分开存放（`exchange/remote-cache/bundles/<来访编号>/<指纹>/`），解包时逐项校验路径。
   - 作业：`PUT /node/jobs/<作业号> {job, bundle}`。本机交换目录里的路径写成 `@ex:tasks/<任务号>/…`，`max_file` 写成 `@model:<包内路径>`，`template_max`、`render_preset_rps` 为 null。
3. 渲染机把作业写进自己的交换目录：作业号 `<作业号>-<来访编号>`，任务号 `remote-<来访编号>-<任务号>`（执行器把校准清单、分区图、`_extra` 写在 `exchange/tasks/<任务号>/` 下，所以工作区放在这里）。`@ex:` 换成工作区路径，`@model:` 换成解开的模型包，模板和预设用渲染机自己的，另加 `asset_dirs`（模型包目录，执行器临时加进 Max 贴图搜索路径，任务结束移除）。`created_at` 改为收到的时间，按到达先后排队。换算后作业里不允许出现工作区以外的绝对路径。
4. 本机轮询 `GET /node/jobs/<作业号>`：返回 `state`、换回 `@ex:` 写法的 `status`、`queue_position`、渲染机执行器状态和 `outputs`（工作区里在作业 `started_at` 之后改过的文件）。本机先把新的或变了的文件下载到对应的本机路径（`GET …/file?path=`，原子替换），再写本机状态文件，所以本机看到「完成」时文件一定已经在了。结束后在消息末尾注明「在渲染机 X 上完成」。
5. 连不上时作业保持 `waiting_lock`，消息说明「恢复后自动发送」或「连上后继续同步」；令牌失效提示重新输入连接码；渲染机拒绝（缺模板、格式无效）或找不到作业时本机作业记为失败。
6. 换渲染位置：还没发出去的作业跟着换；换回本机就删掉状态文件，交还本机执行器。已经发出去的作业在原渲染机做完后照常同步回来。
7. 渲染机清理：来访作业文件、工作区、模型包 14 天没动过就删除（只动 `remote-*` 命名空间和 `<作业号>-<来访编号>` 作业）。日志写在 `exchange/render_node.log`（1 MB 轮换）。

### 14.4 执行器改动

- Max 锁脚本优先用建模技能里的 `pf_max_lock.ms`（和建模会话共用一把锁），没有时用随程序带的 `max/pf_max_lock.ms`。
- `asset_dirs` 里的文件夹（含子文件夹，最多 200 个）在任务开始时加进 `mapPaths`，任务结束按加入前的条数删回去。加失败只记日志，不中断任务。

### 14.5 界面

- 顶部状态片：本机时同以前；渲染机时显示「渲染机 <名字>」，附「连接中」「连不上」「要连接码」「Max 未打开」「忙」。点开是渲染位置面板：本机 Max、自动发现和连过的渲染机（标签：空闲、忙 · N 个任务、Max 未打开、要连接码、缺白底模板、连不上），手动添加，以及连接码输入。渲染机时不显示「启动 Max」。
- 用渲染机时，首页「先完成本机设置」提示不再要求本机 Max 和模板。
- 本机设置「渲染机」卡片：设为渲染机、连接码（可换）、电脑名和 IP、自检（后台服务、Max 程序、插件、模板、Max 已打开并连上、防火墙、开机自启）、已连接过的电脑、断开所有电脑、停止共享。

## 15. 画布（v1.5，2026-11-22 用户定：只做生图和连接生图 API，交互参考 LibTV，精简）

- 入口：左侧竖栏「画布」（`#canvas`），整页无限画布。
- 开源依赖：Drawflow 0.0.60（MIT，`web/vendor/drawflow/drawflow.js` 原样），负责节点拖动、连线、平移。`web/canvas.js` 用子类改写：
  - 滚轮以鼠标为中心缩放（0.2–2.5）；
  - 按钮缩放以画面中心；
  - 去掉右键删除圆点；
  - Delete 走可撤销删除。
  节点外观和样式全部自己写，不用它的 CSS；容器不能带别的 class（Drawflow 按第一个 class 判断点的是不是空白处）。
- 节点只有两种：
  - `upload`（拖入、上传、粘贴的图，只有输出圆点）；
  - `gen`（生图：提示词、比例、张数，有输入和输出圆点）。
  每个节点记 `results`（多次生成累积）和 `current`（当前显示的那张），下游的参考图 = 连进来的节点各自的 `current`。
- 交互：
  - 双击空白处新建生图节点；
  - 从圆点拉线到别的节点就是参考；拉到空白处松手，就在那里新建一个连好线的生图节点；
  - 「继续生成」在右侧新建并连好；
  - 拖空白处平移，自动保存（0.5 秒防抖）。
- 接口：
  - 画布内容：`GET/PUT /api/canvas`（`board.json`：`view`、`seq`、`nodes`、`edges`）。
  - 图片：`POST /api/canvas/files`（原始字节，最大 50 MB）、`POST /api/canvas/files/paths`（桌面拖入的本机路径）、`GET /api/canvas/files/<32位十六进制>.<扩展名>`。
  - 生成：`POST /api/canvas/generate {node, prompt, ratio, count, refs}` 在后台线程生成，同时最多 4 个，返回任务记录；`GET /api/canvas/runs/<id>` 查状态，`state` 为 running、done 或 failed，完成后 `files` 列出结果。程序重启时，还在 running 的记录改成 failed，可以重试。
  - 其他：`POST /api/canvas/reveal` 在资源管理器里选中图片。
  - 设置：`GET/POST /api/canvas/settings`；`POST /api/canvas/models` 拉模型列表，像生图的模型排在前面。
- 生图接口（`server/image_gen.py`）：
  - 设置存在 `image_api.json`（`base_url`、`model`、`mode`、`reuse_ai_key`）。Key 用 DPAPI 加密存在 `%APPDATA%\whitebg-studio\image_key.bin`；没填时沿用 AI 设置里的中转站地址和 Key。
  - 接口类型：
    - `images`：没有参考图走 `/images/generations`，有参考图走 `/images/edits`（多张参考一起传）；比例换成 size（1:1→1024x1024，3:4、9:16→1024x1536，4:3、16:9→1536x1024）。
    - `chat`：`/chat/completions`，参考图用 data URL；从 `message.images`、内容里的 data URL 或图片链接取图。
    - `auto`：模型名含 gemini、banana、nano、4o-image、image-preview、flash-image、chat 时走 chat，否则走 images。
  - 张数按并发的单张请求实现。错误信息里不出现 Key。

### 15.1 1.5.1 深色版（2026-11-22 用户定）

- **外观**：画布改成深色。节点是图片卡片，宽 336px，正好 14 个格栅宽；名字和状态标在卡片上方，左右是「+」连接点，位置在卡片垂直中间。
- **输入面板**：不再放在节点里，改成点生图节点时出现在它下方的浮层，用屏幕坐标，不跟着缩放。
  - 内容：风格、配色、构图、相机四个快捷预设；上传、画布参考、预设提示词；带编号的参考图；提示词；模型；「比例 · 画质 · 尺寸 · 张数」。
  - 下面放不下时，画布自动往上平移。
  - 弹层按上下哪边空间够决定往哪边弹。
  - 预设在 `web/canvas-presets.js`。
- **参数**：生成参数新增 `quality`（standard、high）和 `resolution`（1K、2K、4K），比例加了 2:3 和 3:2。
  - gpt-image、dall-e 只用固定尺寸（1024x1024、1024x1536、1536x1024）。高清画质时 gpt-image 传 `quality=high`，dall-e 传 `hd`。
  - 其他模型按「长边 1024、2048、4096 + 比例」算出宽高，取 64 的倍数。
  - 对话接口把比例、分辨率、高清写进提示词。
- **多张结果**：节点空着时，第一张进这个节点；其余每张一个新节点，用 `stackBelow` 在原节点下面同一列纵向排开（间隔 48，碰到别的节点往下跳），复制原节点的生成参数和入边。
  - 1.5.0 存下来的一个节点多张，在载入时也这样拆开。
- **格栅对齐**：格栅 24px。新建、生成、上传、继续生成的位置都对齐；拖动松手时吸附到最近的格栅点，有 160ms 过渡。卡片高度也取格栅整数倍，上限向下取整。
- **连线动效**：用 MutationObserver 给每条 `.main-path` 后面加一条 `.cv-flow` 路径，同步 `d`。这条路径用 `stroke-dasharray` 加 `stroke-dashoffset` 动画做出流动的蓝光，不响应鼠标。

### 15.2 1.5.2 项目、多画布、对齐、打组（2026-11-22 用户定）

- **输入面板精简**：去掉风格、配色、构图、相机那一行。「提示词预设」改成：自己存的预设（`prefs.json` 的 `presets`，最多 60 条，去重）+ 几条内置常用写法（`web/canvas-presets.js` 的 `BUILTIN_PRESETS`）。
- **记住参数**：比例、画质、尺寸、张数改动时写进 `canvas/prefs.json` 的 `gen`（防抖 400ms），新建节点用它做默认值；已有节点保留自己的参数。服务端只接受合法值。
- **存储**：`exchange/canvas/` 下
  - `projects/<12 位 hex>/project.json`（名字、画布列表）、`canvases/<8 位 hex>.json`（节点、连线、视图、`groups`、`seq`）、`images/`（这个项目的全部图片）；
  - `prefs.json`（`gen`、`presets`、`last` = 上次打开的项目和画布）；`runs/` 生成记录；`trash/` 删掉的项目和画布。
  - 旧的 `board.json` + `files/` 第一次启动时搬进「我的项目 / 画布 1」。画布文件夹在软件运行时被删掉，用到时会重建。
  - 接口：`/api/canvas/projects`（GET/POST）、`projects/<pid>`（PUT/DELETE）、`projects/<pid>/restore`、`projects/<pid>/canvases[/<cid>[/board]]`、`projects/<pid>/files[/paths|/<name>]`、`reveal {project?, file?}`、`prefs`；`generate` 要带 `project`，参考图和结果都在这个项目里。
- **顶栏**（`web/canvas-projects.js`）：高 44px，项目名按钮、画布标签（点切换、+ 新建、双击改名、× 删除，最后一张不能删）、空白（拖动窗口）、生图接口状态。深色面板距窗口上沿 8px，桌面窗口里右边留 122px 给窗口按钮，窗口按钮在画布页换成深色。
- **鼠标**：Drawflow 的 `click` 改写——左键空白处框选（不再平移），中键平移，左键节点交给画布自己的拖动；鼠标样式：空白 `default`、节点和组框 `grab`、拖动和平移 `grabbing`。
- **拖动与对齐**（`alignGuides`）：拖动中的外框和其他节点比较左/中/右、上/中/下，阈值 6 屏幕像素（除以缩放）；对上的方向贴齐并画粉色虚线，没对上的方向按格栅取整。Alt 不对齐。多选一起拖，相对位置不变。
- **选择**：框选按屏幕矩形相交；Shift/Ctrl 加减选；Ctrl+A、Esc、Delete（多选删除可撤销）。单选生图节点出输入面板；多选出虚线外框和「打组」按钮。
- **打组**：`groups: [{id, name, nodes}]`，一个节点只属于一个组。组框（`groupFrame`：四周留 24，上面多留 48 放组名）画在画布里最底层，跟着缩放；拖组框整组移动，双击组名改名；成员少于 2 个的组自动去掉。选中整组时组框高亮、按钮变成「解组」（Ctrl+G）。

### 15.3 1.5.3 生图渠道（2026-11-22 用户定）

- **为什么**：用户会用不同站点接生图模型，或者一个站点一个 Key 只能接一种模型，想接好几种。所以把「生图设置」改成可以存多套的「渠道」，可以改名。
- **存储**（`server/image_gen.py` 的 `ImageAPISettings`）：
  - `image_api.json`：`{version: 2, active: 渠道号, channels: [{id(8 位 hex), name, base_url, model, mode, reuse_ai_key, models(拉到的模型列表，最多 300)}]}`，最多 30 个渠道。
  - 每个渠道的 Key 用 DPAPI 加密，存在 `%APPDATA%\whitebg-studio\image_keys\<渠道号>.bin`；删除渠道时一起删。
  - 旧版单套设置（`image_api.json` 平铺字段 + `image_key.bin`）第一次读取时变成「渠道 1」，Key 文件挪过去。
  - 新建渠道默认叫「渠道 N」（N 接着现有「渠道 数字」往下数），新建、复制的渠道成为当前渠道。
- **接口**：`GET/POST /api/canvas/settings`（POST 不带 id 是新建，返回值的 `saved` 是渠道号）；`DELETE /api/canvas/settings/channels/<id>`；`POST …/<id>/activate`；`POST …/<id>/copy`（连 Key）；`POST /api/canvas/models` 带 `id` 时用那个渠道存的 Key，不带 id 时不拿别的渠道的 Key。
- **生成**：`/api/canvas/generate` 带 `channel`，生成记录里也存 `channel`。渠道被删了时这次生成失败，提示重新选择。
- **界面**：
  - 「生图渠道」弹窗（`web/canvas-channels.js`，深色）：左边渠道列表（名字、模型 · 站点、「正在用」），右边编辑（渠道名、接口地址、Key、沿用、模型 + 拉到的生图模型标签、接口类型），下面删除（连点两次）、复制为新渠道、关闭、保存。改了没保存就切换会先提示，再点一次才放弃。
  - 输入面板的模型按钮显示「渠道名 · 模型」，点开列出渠道；选了写进节点的 `gen.channel`，并设为当前渠道（新节点默认用当前渠道）。顶栏右边显示当前渠道。

### 15.4 1.5.4 图片接口返回的解析（2026-11-22）

- **起因**：用户用即梦渠道（seedream-5.5，图片接口，3:4、2K）生成失败，只提示「生图接口没有返回图片」，看不出接口到底回了什么。
- **解析**：图片接口改用 `with_raw_response` 拿原始返回，自己解析（`extract_images_image`）：
  - 认 `data[]` 或 `data.data[]`、`images[]` 里的 `b64_json`、`b64`、`base64`、`url`、`image_url`（字符串或 `{url}`）、`image`，以及 data URL、纯 base64；
  - 某张图失败时（火山方舟写法 `data[].error`）、顶层 `error`：报「生图失败：原因」；
  - 返回任务号（`task_id` 或 `status` 是排队中、处理中）：说明是异步任务，暂不支持；
  - 都认不出：报「生图接口没有返回图片。接口返回：…」，带最多 300 字的原话（长 base64 截掉、去掉 Key）。
  - 对话接口没出图时，也带上模型回复的前 120 字。
- **即梦尺寸**：模型名带 seedream、即梦、jimeng 的，按火山方舟推荐的 2K 尺寸（1:1 2048x2048、4:3 2304x1728、3:4 1728x2304、16:9 2560x1440、9:16 1440x2560、3:2 2496x1664、2:3 1664x2496）；4K 取两倍；4.5 以后总像素最少 2560x1440，所以 1K 也按 2K 出。以前 3:4 2K 算出来是 1536x2048（约 315 万像素），低于这个下限。

### 15.5 1.5.5 异步生图任务（2026-11-22）

- **实测到的**（用户同意后用即梦渠道真实发了一次）：用户的中转站（FluxPort）对较重的请求自动改成异步（`execution_mode_reason: heavy_request_auto_async`）：
  - 提交回 HTTP 202，`object: image.task`，有 `id`/`task_id`、`status: queued`、`poll_url: /v1/images/tasks/<任务号>`、`poll_after_ms: 2000`、`assets: []`；
  - 查询时 `status` 变成 running，结束时是成功（图在 `assets` 里）或失败；这次是 `uncertain`（`error_code: image_result_uncertain`，「生成服务暂不可用」，没扣费），中转站要求先别重复提交、拿任务号找客服。
- **做法**（`ImageGenerator._wait_task`）：
  - 返回是异步任务（`object=image.task`、有 `poll_url`，或有 `task_id` 且状态是排队中/处理中）时，按 `poll_after_ms`（1～10 秒，默认 2 秒）去查 `poll_url`（没有就用 `result_url` 或 `images/tasks/<任务号>`），最多等 10 分钟；
  - 只查和生图接口同一个站点的地址，Key 也只发给这个站点；`assets` 里的图片链接，相对地址按这个站点补全，同站点带 Key 下载，别的站点不带；
  - 失败时报原因并带上任务号；`uncertain` 报「中转站说这次结果还没确认：原因（任务号 …）」，不自动重试。

### 15.6 1.5.6 按 中转站（FluxPort）文档接即梦和 Gemini（2026-11-22）

- **即梦（seedream）**：图片接口不传像素 `size`，传 `resolution`（1K、2K，4K 按 2K）+ 画幅；画幅字段 seedream-5.0 系列叫 `ratio`，其他（5.5 等）叫 `aspect_ratio`（`seedream_params`）。参考图走 `/images/edits` multipart，字段 `image[]`。
- **Gemini 原生**：新接口类型 `gemini`。`POST {根地址}/v1beta/models/{模型}:generateContent`（根地址是接口地址去掉结尾的 `/v1`），`contents[].parts` 里放文字和 `inlineData`（裸 base64），`generationConfig` 是 `responseModalities: [TEXT, IMAGE]` + `imageConfig: {aspectRatio, imageSize}`；结果读 `candidates[].content.parts[].inlineData` 或 `fileData.fileUri`，被拦截时说明原因。
- **生成请求**：带 `Idempotency-Key: panda-<随机>`；SDK 不自动重试（`max_retries=0`），免得重复扣费。
- **异步任务**（`_wait_task`）：查 `status_url`（没有就 `poll_url`、`result_url`），间隔取 `poll_after_ms`（2～10 秒）；queued、dispatching、running 继续等；success 读 assets，没有 assets 再读 `result_url`；failed、uncertain、client_disconnected、paused、canceled、未知状态，或 `auto_poll:false`、`poll_after_ms:0` 时停下并说明原因（带任务号，没扣费时写「没扣费」）；查询遇 429、5xx、断网按 5、10、20、40、60 秒退避，最多 5 次；最多等 20 分钟（不取消任务）。下载 assets 优先 `signed_url`，其次 `download_url`、`url`，同站点带 Key，别的站点不带。
- **图片接口被拒**（`Images API is not supported`）：这次生成自动改走 Gemini 原生（Gemini 类模型）或对话接口（其他）。
- **渠道模板**：生图渠道弹窗顶上「即梦 5.5」（`https://api.ai-media.vip/v1`、seedream-5.5、图片接口）和「香蕉 Gemini 3.1」（同地址、gemini-3.1-flash-image-preview、Gemini 原生），点一下填好，只剩填 Key；新渠道或还叫「渠道 N」的顺便改名。

### 15.7 1.5.7 渠道自己选比例的传法、额外参数，去掉写死的模板（2026-11-22）

- **起因**：用户反馈「比例不生效」：「即梦5.5 副本」（seedream-5.0-pro）选 3:4、1:1 出来都是 2368x1776（4:3），说明这家中转站没认按文档传的 `ratio`；香蕉（Gemini 原生）3:4 出 1792x2400，是对的。用户要的是能接别家中转站、别的图像模型，1.5.6 按模型名写死即梦参数、加两个写死的模板等于圈死了。
- **渠道新增两项**（`image_api.json` 的渠道里）：
  - `size_style`：`auto`（gpt-image / dall-e 只传它们的标准 size；其他模型 size 像素 + aspect_ratio + ratio + resolution 全带上，同一个比例）、`size`、`aspect_ratio`（+ resolution）、`ratio`（+ resolution）、`all`。`size_params()` 按它算。Gemini 原生固定 `imageConfig.aspectRatio / imageSize`，对话接口写进提示词，都不受它影响。
  - `extra`：JSON 对象，每次请求原样并进请求体（Gemini 原生逐层合并，对话接口放 extra_body）；不能有 model、prompt、image、images、n、contents；最长 4000 字。
- **去掉**：按模型名判断即梦的 `seedream_params`、即梦不传 size 的规则、弹窗里写死的两个中转站模板。参考图一律按列表发（字段 `image[]`），dall-e 单张时发 `image`。
- 渠道弹窗的左侧列表在弹窗往下滚时停在原处。

### 15.8 1.5.8 去掉背景格栅点（2026-11-22 用户定）

- 画布背景改成纯色（`--cv-bg`），不再画 24px 的格栅点；节点位置仍按 24px 格子对齐（新建、生成、拖动没对上别的节点时）。

### 15.9 1.5.9 分组连线、@ 引用参考图（2026-11-22 用户定）

- **分组连线**：分组上存 `links: [生图节点号]`（和 `nodes` 一起存在画布文件的 `groups` 里）。
  - 组框右上角（组名那一行，`top:24px`）有「+」，拖出虚线：松手在生图节点上就连上；松手在空白处就在那里新建生图节点再连上；不能连到上传的图、不能连到组里自己的节点。
  - 线画在画布里组框上面、节点下面的 `svg.cv-glinks`：从组框右边 (x+w, y+24) 到节点左边「+」的圆心 (x-23, y+h/2)，和节点连线一样的曲线（`curvePath`）加流动蓝光；节点移动、卡片变高、组变化时重画。
  - 点线选中（红色），Delete 断开；面板里分组那一圈的 × 也能断开。删节点时清掉指向它的连线（撤销能恢复）；多张结果的新节点也连上同一组；解组时改成组里每张图单独连到目标节点。
- **参考图顺序**（`refItems`）：先是连进来的分组里有图的节点（每组按位置从上到下、从左到右），再是单独连的，去重。面板编号、发给接口的顺序都按它。
- **@ 引用**：提示词框里输入 @ 弹出列表（已连的参考图带「图N」编号，后面是画布上别的图，选了自动连上），↑↓ 选、回车或 Tab 确定、Esc 关；插入「@图片名 」。生成时 `resolveMentions` 把 @图片名（空格可有可无）换成按参考图顺序的「图N」，并在开头加一句「参考图按顺序编号：图1～图N。」；不在参考图里的 @ 原样留着。节点里存的还是带 @图片名 的原文。

### 15.10 1.6.0 比例写死、连线可删、生图中的样子（2026-11-22 用户定）

- **比例写死**（不用每个渠道去选）：
  - 查到用户新加的「GPT」渠道（另一家中转站的 gpt-image-2.6-sunburst）要 3:4、发了 size=1024x1536，却出了 1536x1024 横图：这家不认尺寸参数。
  - 图片接口的提示词末尾加「画面比例 3:4（竖版）。」（`ratio_hint`）；尺寸参数照旧（gpt-image 标准尺寸；其他模型 size + aspect_ratio + ratio + resolution）。
  - 出图后 `fit_ratio`：和要的比例差不到 1.2% 不动；横竖方向反了不裁，在生成记录里提示；方向对、裁掉不到四分之一的，居中裁到要的比例（png 存 png，jpg 质量 95）。
  - 生图渠道弹窗去掉「比例怎么传」；额外参数收进「高级」。
- **@ 只列已连上的参考图**，不再列画布上别的图。
- **分组连线**：组的「+」在组框右边正中间、框外 34px（`GROUP_PORT`），一小段虚线连着框；组里的图在面板参考图栏里平铺，每张可点 × 去掉（存在分组的 `skip[目标节点]`，「画布参考」里点一下能加回来）；都去掉了就断开。
- **连线选中删除**：每条连线后面加一条透明的粗线 `.cv-hit`（屏幕上约 16px，按缩放换算 `--cv-hit`），点中就选中；选中的线中间出 × 按钮（`.cv-line-del`），点它或按 Delete 删除；分组连线同样（`.cv-ghit`）。透明粗线必须排在主线后面：Drawflow 移动节点时只更新连线的第一个子元素。
- **节点框按比例**：生图节点的图片区高度按 `gen.ratio`（预览），图片在框里完整显示；上传的图按图片本身。参数按钮和比例选项前面的小框按比例画（`ratioIcon`）。
- **生图中**：不显示计时；图片中间「生图中 进度 xx%」（`progressAt`：到平时用时约 85%，最多 95%，只增不减；平时用时按渠道记在本机 `localStorage` 的 `cv-gen-seconds`，默认 45 秒）；扫光改成两倍宽一层里两道一样的光平移一道的距离（无缝循环）；卡片内容没变就不重画，动画不会被打断；节点一圈慢慢转的淡蓝流光（`@property --cv-angle` + conic-gradient）和轻微呼吸外发光。

### 15.11 1.6.1 边缘流光、连线框选删除、新的开场界面（2026-11-22 用户定）

- **边缘流光**（`.cv-flowlight`，参考用户发的 Apple Intelligence 跑马灯图）：两层（`wide` 柔和宽光、`rim` 贴边细光），每层里一块比卡片大的彩色锥形渐变（紫、粉、橙、黄、蓝）用 `transform: rotate` 慢慢转（6.5 秒一圈），外面的遮罩只露出四周一圈、往中间渐隐；宽光再轻轻呼吸（透明度）。只用 transform 和透明度做动画、不重画，所以不卡。生图中的节点用它替换原来的扫光、外圈转光和呼吸外发光；进度文字在上面。
- **连线选中和删除**：去掉线中间的 × 按钮。选中的线存在 `pickedLines`（普通连线 `e:起点>终点`，分组连线 `g:分组>终点`），选中时变红；点线或线旁的透明粗线选中（Shift/Ctrl 加选），框选时线上每隔几像素取一个点，落进框里的线也选中（和节点一起）。Delete 删除选中的节点和连线，提示里可以撤销（连线也能恢复）。线的点击由画布自己处理，不再用 Drawflow 的连线选中。
- **开场界面**：空画布中间的「开始创作」卡片：黑底、边缘流光、外面一圈反向慢转的彩色光晕（径向遮罩淡出，层级在卡片黑底下面，只在卡片外露出）；渐变图标、标题、一句说明、「新建图片节点」「上传图片」两个按钮，底下三条提示（拖入图片当参考图、中键拖动画布、@ 引用图片）。卡片外的区域不挡鼠标。

### 15.12 1.6.2 点节点居中放大、重新生成、空节点多张成脑图、Windows 分屏（2026-11-22 用户定）

- **单击节点居中放大**（`focusNode`）：单击（没拖动）选中节点时，节点（生图节点连同下面的输入面板）居中，缩放到正好放满画面，最大 150%，320ms 平滑过渡（`animateView`，新的动画顶掉旧的）。拖动、框选、Shift 多选不触发。
- **重新生成**：有图的生图节点，工具条多「重新生成」，面板按钮也叫「重新生成」；生成时一律只要 1 张，结果追加到 `results` 并设为当前（替换显示，旧图文件不删）。用户确认取消了「在原图上改图分出节点」。
- **空节点多张 → 脑图**（`branch` + `branchLayout`）：空节点、张数 ≥ 2 时，这个节点当提示词节点不放图；右边隔 120px 立刻建好 N 个节点，纵向间隔 48、整列上下居中对齐提示词节点、对齐格栅；那一列有节点挡着就整列挪到它们下面。每个子节点连着提示词节点，各自单独请求一次（每次 1 张，参考图和提示词用提示词节点的），各自显示流光和进度；分出的节点看不全时画面平移（必要时缩小）装下。空节点只生成 1 张时，图放进节点自己。
- **「＋ 图片节点」**：放在画面中间；中间有节点挡着就往右挪到空位。
- **Windows 分屏**（`desktop.pyw`）：
  - pywebview 自带的拖动是网页里一点点挪窗口，不走系统「移动窗口」，所以拖到屏幕边上不会分屏；无边框窗体也缺系统窗口样式。
  - 现在：窗口补上 WS_CAPTION、WS_THICKFRAME、WS_SYSMENU、WS_MINIMIZEBOX、WS_MAXIMIZEBOX；用 `SetWindowSubclass` 在 WM_NCCALCSIZE 里让整个窗口都是网页（不画系统边框和标题栏），系统最大化时把多出的一圈边框收回，内容正好铺满工作区；WinForms 把样式冲掉时改大小会补回。
  - 拖动：顶部标题区（`win-drag`）按下时调 `window_start_drag`，在界面线程里 `ReleaseCapture` + `PostMessage(WM_NCLBUTTONDOWN, HTCAPTION)`，交给系统移动窗口 —— 拖到屏幕边缘分屏、Win11 拖到顶部选布局、拖动最大化窗口自动还原、Win+方向键都能用。双击标题区（两次按下间隔 < 450ms、位置相近）切换最大化。
  - 最大化 / 还原改用系统的 `ShowWindow`；窗口大小变化时网页问一下是否最大化，更新图标和圆角描边。最大化时去掉圆角裁剪。
  - 不支持：Win11 鼠标停在最大化按钮上弹出的布局菜单（按钮是网页画的）。

### 15.13 1.7.0 整体审查后的 10 轮优化（2026-11-23，审查和方案见 docs/整体审查与优化方案.md）

- **首页提示**（`web/settings.js` 的 `banner`）：按 `/api/settings` 的 `checks` 说缺什么——缺 3ds Max：「先指定 3ds Max」；白底模板文件没了：「白底模板文件不见了」（重新安装能补回）；都缺：「本机还缺 3ds Max 和白底模板」。选了内网渲染机（`ready` 为真）不提示。设置页 3ds Max 卡片多一行白底模板状态。
- **标题让位**：桌面窗口里，把标题放在正文里的页面（`.page-main > .page-header-inline:first-child`，参考库、本机设置）标题往下让 46px，右上角窗口按钮不压住「添加并标注…」。
- **画布缩略图**：`GET /api/canvas/projects/<项目>/files/<文件>?w=<宽>` 给缩略图：两档（`THUMB_SIZES = (256, 1280)`，宽 > 256 用 1280），长边缩到这个尺寸，WebP 质量 86，缓存在 `projects/<项目>/thumbs/<文件名>-<档>.webp`（原图更新时间比缓存新就重做）；原图本身长边 ≤ 档位 × 1.25、或缩图失败，直接给原图。节点用 1280，参考图栏、@ 列表、画布参考弹层、项目卡片用 256；看大图、复制图片、生图用原图。图片（原图和缩略图）响应 `Cache-Control: private, max-age=604800`（文件名随机，内容不会变）。
- **撤销 / 重做**（`canvas.js`）：每次 `save()` 后记一步（`commit`）：节点（含位置、提示词、设置）、连线、分组，最多 60 步；改提示词（`save(800)`）停 800ms 才记一步。生图节点的 `results`、`current`、`run` 不算改动：撤销时节点还在就用它最新的图和生成状态，之前在过的节点用记下的最新值（`genCache`），所以撤销不会撤掉已经出的图。多步合一步用 `batch`（新建并连线、分出脑图、删除、粘贴、老版本拆分）。撤销后节点号不回退（`highWater`），不会和撤销掉的节点撞号。删除提示条的「撤销」：删完没再改过就撤销这一步，之后又改过就提示用 Ctrl+Z。快捷键 Ctrl+Z、Ctrl+Shift+Z / Ctrl+Y，工具栏有两个按钮（没得撤销 / 重做时变灰）。切换画布清空撤销历史。
- **复制粘贴节点**：Ctrl+C（选中节点、焦点不在输入框、没有选中文字）往系统剪贴板写一行标记「Panda Studio 画布节点 ×N（时间码）」，内存里记下节点（含提示词、设置、图）、选中节点之间的连线、整组都选中的分组、从外面连进来的参考线和分组连线。Ctrl+V 时剪贴板文字等于标记才粘节点，否则照旧把剪贴板里的图片粘成图片节点。同一张画布里粘贴：每粘一次多错开 48；外面连进来的参考线、分组连线也连上。粘到别的画布：放在画面中间；别的项目：先 `POST /api/canvas/projects/<项目>/files/copy {from, files}` 把图片拷过去（文件名不变，最多 200 张，原项目里没有的跳过）。还在生成的节点只复制提示词和设置。Ctrl+D 原地复制一份（错开 48，不动剪贴板）。粘贴算一步撤销。
- **复制图片**（`web/clipboard.js`）：`navigator.clipboard.write` + `ClipboardItem({'image/png': Promise})`，非 PNG 先在页面里画一遍转成 PNG。节点工具条多「复制图片」，看大图（`lightbox.js`，全软件共用）工具栏多「复制图片」，按钮上显示「已复制」或「复制失败」。
- **快捷键说明**：画布工具栏最右「?」（或按 ? 键）弹出两栏：鼠标、键盘；Esc 或点别处关。焦点不在画布上（比如刚点过工具栏按钮）时 Delete、Esc、Ctrl+G、Ctrl+A 也能用（document 上的 keydown，画布自己有焦点时仍由 Drawflow 那边处理）。
- **失败直接重试**：失败的生图节点在图片区域盖一层：「生成失败」+ 原因（最多 140 字、三行，完整原因在提示气泡和面板里）+「重试」。有图的节点（重新生成失败）只在底部压一条「重新生成失败」+「重试」「忽略」，原图还看得见。重试只重做这一个节点（`run(id,{single:true})`，空节点张数 ≥ 2 也不再分出新节点）；「忽略」把 `run` 清掉。只有按钮区不能拖，节点其余地方照常拖动。
- **关窗口不丢改动**：网页右上角关闭按钮先调各页面登记在 `window.__whitebgBeforeClose` 的「立刻保存」（任务页 `flush`、画布 `flush` + 设置偏好），最多等 3 秒，再 `window_close`。Alt+F4、任务栏关闭：`desktop.pyw` 的 `closing` 事件第一次先取消关闭，后台线程 `evaluate_js` 调 `window.__whitebgCloseSoon()`（网页存好后自己关），4 秒内没关上就直接关（closing 事件在界面线程同步执行，不能在里面等网页）。浏览器里关标签页：`pagehide` 用 `fetch keepalive` 补发画布。
- **回到上次的页面**：进页面时记 `localStorage['whitebg.page']`（只记 `#canvas`、`#refs`、`#settings`，其余记首页）；打开软件地址没有 # 时用 `history.replaceState` 换成上次的页面再路由。任务页不记（任务可能已删）。
- **生成记录清理**：`CanvasStore._recover`（启动时）删掉 `canvas/runs` 里 30 天前（`RUN_KEEP_DAYS`，按文件修改时间）的记录，其余还在 running 的标成失败。启动时没有正在生成的任务，不会删到在用的记录；图片在项目文件夹里不受影响。删掉的项目（`canvas/trash`）不自动清。

### 15.14 1.7.1 AI 校准的材质参考自动进参考库（2026-11-23 用户定）

用户要求：每次 AI 校准时选的材质参考图，都能在参考库里对应材质下直接拿到；以后加的也一样。

- **自动收进参考库**（`RefLibrary.absorb`，`Calibration.absorb_ref`）：本任务材质参考每次保存框，都收进参考库的标注参考图（`annotated`），不再有「也加入参考库」的勾。每个框按材质归组：参考库材质直接用；场景材质、新材质先找同名分组，再按分组关键词归（`material_key`，如「柜门 胡桃饰面」→ 胡桃木），都对不上就新建同名分组（关键词就是这个名字，去掉文件名里不能用的字符；整理后不能用的跳过）。没有框的整体参考不收。参考库写失败不影响任务保存。
- **不重复**：同一张图（JPEG 内容一样，按 sha256 比，记在 `annotated[].sha`）在别的任务里又用了，并进已有的那一张；从参考库标注图点选来的（`library_file`）并回那一张。合并规则：这次出现的材质，框换成这次的；别的材质的框保留。任务里删掉材质参考，参考库里的那张保留（要删去参考库页删）。
- **「从参考库选」**：每个材质先列标注过的框（裁图，标「标注」，从 AI 校准收进来的标「校准用过」），再列认可的图。选标注的框：原样拷那张标注图（`from`），框直接画好并先存上，再打开框选让用户确认；同一张图选了几个材质的框合成一张。
- **参考库页**：从 AI 校准收进来的裁图在对应材质下标「校准」。
- **补导入**（`Calibration.sync_library`）：软件启动时后台把以前 AI 校准里加过、还没收进参考库的材质参考（有框、没有 `library_synced`）补进去，只跑一次（收过的有标记）。
- **AI 选图**：本任务自己的材质参考已经作为「本任务参考」给 AI，从参考库挑标注裁图时跳过这几张（`library_file` 相同的），不重复占名额。

### 15.15 1.7.2 安全加固、不占 C 盘、清单保留成果与工程基建（2026-11-26）

- **单一版本源与打包净化**：仓库根目录新增 `VERSION`；`build.ps1` 传 `/DAppVersion` 给 Inno Setup，`whitebg.spec` 生成 exe 版本资源，`/api/health` 返回 `version`，`web/shell.js` 动态写入 `#app-version`。打包改用干净的 `config.default.json` 并剔除 `refs/library.json`、`refs/builtin.json` 中的本机绝对 `source` 路径；排除 `setuptools`/`aiohttp`/`PIL._avif`/`PIL._imagingtk`/多架构冗余库；构建后扫描 dist 敏感词。`installer.iss` 移除管理员覆盖选项、选 C 盘弹提示。
- **不占 C 盘**：
  - 加密密钥（`ai_key.bin`、`image_key.bin`、`image_keys/<cid>.bin`）默认存 `<root>\secrets\`（CurrentUser DPAPI 不变）；运行真实程序根目录时自动执行校验迁移（复制→字节核对→原子替换→再核对→删 `%APPDATA%\whitebg-studio\` 原文件），测试隔离目录不触发迁移。
  - `desktop.pyw` 以 `private_mode=False, storage_path=<root>\webview-data` 启动 WebView2，既保留 `localStorage` 又将浏览器缓存限定在安装目录。
- **清单增删/排序保留成果与删除撤销**：`server/core.py` 的 `_update()` 按 `item_id` 对应原条目，通过 `move_files()` 两阶段临时名原子重命名 `RESULT_FILES`（失败回滚），`renumbered_result()` 保留 `state`/`job_id` 并记录 `job_item` 供 `_merge()` 对齐，同时更新 `ai_calibration.accepted_overrides['items']`。删除条目暂存到 `tasks/<id>/removed/<item_id>/` 与 `task.removed_items`（保留最近 20 条），前端弹 10 秒撤销条，撤销时原样恢复。新增条目不触发 `lighting_changed`。
- **通用缩略图**：`/api/file?path=...&w=<px>`（`w` 钳制 32..1024）经 `allowed_file` 校验后由 `Studio.thumb_file()` 按 `path+mtime_ns+size+w` 生成 WEBP 缓存于 `exchange\.thumbs`（启动清理 30 天前缓存），清单、任务卡、参考库、总览全部接入，灯箱与标注保持原图。
- **前端网络健壮性**：新增 `web/api.js` 封装 `AbortController` 超时、中文网络/超时错误、顶部常驻 `.offline-banner` 断线横幅与指数退避、`readJsonSafe`/`uploadBlob` 统一上传解析；`route()` 限时中止挂起保存并不阻塞轮询。
- **后端安全与稳定性**：
  - 渲染机回传强制 `safe_output_rel()` 白名单校验（仅 `preview/`、`smallshots/`、`final/`、`calibration/round-N/` 下允许扩展名，拒绝 `task.json`）及收紧 `ABSOLUTE_RE`/盘符相对路径拦截；zip 解压按实际写入字节限流。
  - `remote_client.py` 增加 `'sending'` 阶段与 `_sending` 集合消除切回本机竞态。
  - `node_setup.py` 防火墙规则改为 `profile=domain,private` + `program=`；`render_node.py` 连接码 10 分钟 TTL（暴露 `code_expires_at`）、配对成功自动轮换、全局失败限流（20 次/10 分钟）、15 秒 handler 超时、并发信号量（16）、500 错误不回显内部异常。
  - `ai_settings.py` / `image_gen.py` 公网强制 `https://`、换未保存不同源地址不带旧 Key、API POST/JSON 轮询禁止重定向、图片下载拦截内网/回环 SSRF 并在重定向时剥离鉴权头。
  - Max 渲染期间由 `wb_core.LockRenewer` 守护线程每 180 秒刷新同 owner 锁文件（`pf_max_lock.ms` 同步 `PF_MAX_LOCK_FILE`）与心跳，优先加载内置 `max/pf_max_lock.ms`；`runner.py` 活跃渲染任务宽限 4 小时。
  - `calibration.py` `await_round()` 改为退避轮询、缩小 `studio.lock` 持有区间，并按 `calibration_timeout_sec` 或作业消失自动超时解锁。

### 15.16 1.8.0 稳定与流畅、白底纯白保障、可编辑材质参数与参考材质精准对应（2026-11-26）

- **后端稳定与流畅（Phase 2）**：
  - **收窄 `studio.lock` 与请求超时**（`server/app.py`、`server/core.py`、`server/cleanup.py`）：`Handler.timeout = 30`；所有 JSON 请求体与二进制上传流在 `studio.lock` 锁外读取校验；慢速目录浏览 `browse()`、扫描 `scan()`、图片解码与缩图、`deliver()` 大文件拷贝均移出 `studio.lock`，只读接口（`/api/health`、`/api/runner`、`/api/tasks`、`/api/refs`）不被入库或 NAS 扫描阻塞。
  - **状态与任务读取缓存**（`server/runner.py`、`server/node_setup.py`、`server/core.py`）：`tasklist` 进程探测加 3 秒 TTL 缓存与独立锁，`netsh` 防火墙规则探测加 15 秒缓存与后台异步刷新；`Studio.load()` 按 `task.json` 的 `(mtime_ns, size)` 缓存深拷贝快照。
  - **过期作业归档与无缝回查**（`server/cleanup.py`、`server/jobs.py`）：启动后台与每日清理将超过 30 天且任务非活跃的终态作业迁入 `exchange/jobs/archive/` 并维护 `exchange/jobs/archive/index.json`；`JobIndex` 先查活跃目录再回退归档索引，历史轮次与小样查看无缝衔接。
  - **快速窗口启动与原子入库**（`desktop.pyw`、`server/core.py`）：桌面窗口立即打开，3ds Max 注册表探测、密钥迁移、参考库补导入、缩略图清理与过期作业归档延后到后台线程；`deliver()` 先将新成图/遮罩/清单写入 `sku/.staging-<uuid>/` 校验无误，再把同名旧文件移至 `sku/旧版/<timestamp>/` 并原子替换入 `sku/`，异常自动回滚，绝不在 `sku/` 留下 `.prev` 备份文件。
- **前端流畅与低占用（Phase 2）**：
  - **动画按需暂停与去冗余毛玻璃**（`web/style.css`、`web/task-state.js`、`web/app.js`）：进入任务页、画布页或窗口隐藏时在 `body` 挂 `bg-paused` / `page-hidden` 暂停底层光斑动画，去掉深色不透明面板多余 `backdrop-filter`。
  - **FBX 单次解析与警告局部过滤**（`web/model/loaders.js`、`web/viewer.js`、`web/variants.js`、`web/app.js`）：预览 FBX 只解析一次并直接复用 Three.js 对象做多柜款式识别，识别后释放原始 `ArrayBuffer`；`console.warn` 过滤严格限定在 `fbxLoader.parse` 的 `try/finally` 内。
  - **画布高频交互减负与轮询退避**（`web/canvas.js`、`web/canvas-model.js`、`web/lighting.js`、`web/runner.js`）：节点拖动缓存尺寸并用 `requestAnimationFrame` 合并重绘，流光仅在选中或生图中连线播放；移除 `lighting.js` 500ms 与 `runner.js` 5s 无条件 `setInterval`，改为脏状态、路由与可见性触发；画布生图轮询按活跃任务节流，连续失败自动停止并提示离线重试。
- **白底纯白生成保障与深层材质识别**（`max/wb_runner.py`、`max/wb_core.py`、`tools/fake_runner.py`）：
  - **预设加载保护**：调用 `renderPresets.LoadAll` 前快照并在加载后自动补回必需渲染元素（`遮罩_ID`、`遮罩_线框颜色`）及 Corona 环境反射贴图（`bg_overrideReflect` / `bg_texmapReflect`）。
  - **纯白直视背景锁定与恢复**：渲染期间强制开启 `bg_overrideDirect = True`、`bg_colorDirect = RGB(255, 255, 255)` 并临时清空非纯白直视贴图（`bg_texmapDirect`），确保背面视角隐藏模板背景墙（`|azimuth| > 100°`）或自定义打光压暗环境 GI 时背景仍为纯白，渲染结束后完整还原渲染器原始属性。
  - **环境反射关断与嵌套材质遍历**：`environment.reflection_strength_mul` 范围扩展至 `[0.0, 2.0]`（设为 `0` 时临时关闭 `bg_overrideReflect`，消除反射贴图对木纹/烤漆高光的污染）；递归支持 `Multimaterial` 与 `RaySwitch` 任意多层嵌套的清单提取与参数覆盖；导出预览（`export_preview`）与普通出图始终写入 `<task_dir>/preview/scene_manifest.json` 供界面即时读取。
- **可编辑材质参数**（`server/materials.py`、`PUT /api/tasks/<id>/materials`、`web/materials.js`）：
  - 任务页顶栏新增「材质参数」弹层，预览或小样完成后即列出场景全部材质（色块、类型、对象数、贴图名），未导出前也支持手动输入材质名预填；可逐材质调节色调微调（`-15°..+15°`）、颜色浓淡（`60%..140%`）、明亮程度（`50%..250%`）、明暗反差（`70%..130%`）、反光强度（`0%..200%`）、表面光泽（`-0.30..+0.30`），玻璃材质额外支持玻璃折射（`0%..150%`）、折射率（`1.30..1.80` 或留空沿用模型默认）、不透明程度（`50%..100%`），以及全局环境反光强度（`0%..200%`）。
  - `effective_overrides(task)` 以 `ai_calibration.accepted_overrides` 为基线叠加手动 `task.material_overrides`，统一作用于出小样、正式渲染和 AI 校准起始轮；校准记录每轮新增「以此轮微调」载入该轮参数继续手调；切换打光清空 AI 采用轮次时保留用户手动材质参数。
- **AI 校准精准对应参考材质**（`server/ref_library.py`、`server/calibration.py`、`web/materials.js`、`web/calibration.js`）：
  - 升级 `material_key(name, library)` 多级匹配（精确匹配 → 去前后缀编号与饰面词 → 最长关键词优先 → 核心词反向匹配如「胡桃」→「胡桃木」→ 安全同义词表），严禁泛称「玻璃/清玻/茶玻」误配「长虹玻璃」。
  - 支持任务级显式绑定 `task.material_bindings`（自动匹配 / 指定任意参考库分组 / `__none__` 不对照参考库），在「材质参数」下拉框与「AI 校准」参考对应摘要条中直接查看与修改。
  - 未匹配到具体场景材质的任务框选参考不再静默丢弃（归入 `其他参考（<名称>）`）；12 张图片预算调整为小样+分区对照 → 本任务框选参考 → 参考库已标注裁图 → 已认可/候选材质参考 → 风格与主图参考 → 剩余名额才补同产品泛白底；每张参考图 `role` 明确写出对应场景材质名与目标 Lab，并向模型传入结构化 `material_reference_map` 与逐材质配色硬约束。

### 15.17 1.8.1 新机安装启动热修（2026-11-27）

- **修复新电脑首次安装报错 `Cannot find win-arm64`**：`pywebview/platforms/edgechromium.py` 导入时会无条件遍历 `('win-arm64', 'win-x64', 'win-x86')` 调用 `interop_dll_path()`，此前 `1.7.2`/`1.8.0` 在 `packaging/whitebg.spec` 的 `SKIP_BINARIES` 中排除了 `runtimes/win-(x86|arm64)/`，导致未装过旧版的电脑启动时报 `FileNotFoundError: Cannot find win-arm64`。已从 `SKIP_BINARIES` 移除该过滤，并在 `packaging/build.ps1` 中强制校验三个 `WebView2Loader.dll` 均存在。

### 15.18 1.8.2 内网渲染机发现与公用网络防火墙修复（2026-11-27）

- **防火墙规则重建与公用网络兼容**（`server/node_setup.py`）：Windows 对未加域的办公室局域网默认归类为「公用网络（Public）」，`1.7.2` 起使用的 `profile=domain,private` 导致这类局域网上的渲染机被系统防火墙拦截 TCP `8775` / UDP `8776`。现改为先删除同名旧规则再添加，保持 `program="<exe>"` 与 `remoteip=LocalSubnet,10.0.0.0/8,172.16.0.0/12,192.168.0.0/16` 严格内网约束，同时覆盖 `profile=domain,private,public`；`_probe_firewall_rule()` 解析 `netsh ... verbose` 核对端口、配置文件与当前可执行文件路径，自动识别旧版/过期规则并在设置页显示「重新放行防火墙」。
- **多网卡/代理 TUN 下的局域网发现与地址筛选**（`server/remote_client.py`、`server/render_node.py`）：`local_addresses()` 与 `local_lan_ips()` 过滤 `198.18.0.0/15`（Clash/Mihomo TUN 假网段）与 `169.254.0.0/16`，RFC 1918 地址优先；`discover()` 为每张真实局域网 IPv4 网卡单独绑定广播 socket 并分别向 `255.255.255.255` 及子网广播发送两轮探测包，以 `select.select` 并发收集回包，避免虚拟网卡劫持默认广播路由。

### 15.19 1.8.3 3ds Max 锁脚本 MAXScript 语法热修（2026-11-27）

- **修复 `max/pf_max_lock.ms` 在 3ds Max 2025 中的语法错误**：MAXScript 不支持 `().` 链式调用，原第 65 行 `(dotNetClass "System.Guid").NewGuid().ToString "N"` 在 `fileIn` 时报 `语法错误: 位于 .，需要 <因子>`，且随后 `finally` 中调用 `::PFMaxLock.release` 因 `::PFMaxLock` 未定义覆盖原报错。现改为 `local guid = (dotNetClass "System.Guid").NewGuid()` 后再调用 `guid.ToString "N"`，并以 .NET Framework 4.8 兼容的 `Copy(..., true)` + `Delete` 写入锁文件；`wb_runner.py` 释放锁前增加 `::PFMaxLock == undefined` 保护且不覆盖已有失败原因。
