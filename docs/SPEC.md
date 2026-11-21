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

- `task.ai_calibration.task_refs` 保存 `{id, file, name, created_at, in_library, regions}`。`file` 相对于任务目录，原图和裁图分别保存在 `calibration_refs/` 与 `calibration_refs/crops/`。每个框保存 `{material, material_source, box:[x,y,w,h]}`，坐标归一化。任务详情回包包含最新可用场景清单中的 `scene_materials`。
- `POST /api/tasks/{id}/calibration-refs/upload?name=…` 接收不超过 50 MB 的图片字节并返回 `{entry}`。`PUT /api/tasks/{id}/calibration-refs/{ref_id}` 保存 `{regions,in_library}` 并返回 `{entry}`。`DELETE` 删除任务里的图和裁图。三种写入均校验来源，并用服务锁保护任务记录。
- 每轮排队时固定该轮所用的任务参考图片和框。匹配采用场景原名精确匹配、参考库分组关键词匹配、任务新材质名称包含匹配。每个场景材质最多取 2 张任务裁图。无匹配的新材质裁图作为「其他参考」，未画框的整图作为「整体参考」。每张选中的裁图附平均 D65 Lab。该轮的 `task_refs_used` 保存实际使用的 `{id,region_index}`，整图序号为 `null`，从 0 开始计框。
- 勾选「也加入参考库」保存时，参考库 `annotated` 保存独立副本，副本只含 `material_source == "library"` 的框。任务参考在该任务的中间文件清理时释放。

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
