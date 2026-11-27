import json
from pathlib import Path

BASE = Path(__file__).resolve().parent
OUT = BASE.parent / 'tmp' / 'e2e'
TARGET = BASE / '验收.md'
def read_report(name):
    path = OUT / name if (OUT / name).is_file() else BASE / name
    return json.loads(path.read_text(encoding='utf-8'))
samples = read_report('metrics.json')
flows = read_report('flows.json')
shots = read_report('shots.json')
rows = [
    ('新皮肤', 1, 59.7, 60, 16.0, 2076, 5.35, 5.23),
    ('新皮肤', 4, 3.0, 2.0, 909.7, 780, 27.49, 25.62),
    ('旧皮肤', 1, 59.7, 59.7, 15.5, 2112, .22, .01),
    ('旧皮肤', 4, 38.0, 7.3, 32.8, 1704, .93, .03),
]
for sample in samples:
    canvas, slider = sample['canvas'], sample['slider']
    rows.append(('改后 · '+sample['model'], sample['rate'],canvas['fps'],slider['fps'],
                 canvas['nextFrame']['meanMs'],canvas['nodes']['added']+canvas['nodes']['removed'],
                 sample['taskIdle']['cpuPercent'],sample['homeIdle']['cpuPercent']))

table = ('| 版本／模型 | CPU 降速 | 拖画布 FPS | 拖滑杆 FPS | 跟手延迟均值 ms | 清单节点变动 | 任务页空闲 CPU | 首页空闲 CPU |\n'
         '| --- | ---: | ---: | ---: | ---: | ---: | ---: | ---: |\n')
table += '\n'.join(f'| {name} | {rate}× | {canvas:.1f} | {slider:.1f} | {delay:.1f} | {nodes} | {task:.2f}% | {home:.2f}% |'
                   for name, rate, canvas, slider, delay, nodes, task, home in rows)
checks = []
for name, rate, canvas, slider, delay, nodes, task, home in rows[4:]:
    passed = (rate != 4 or (canvas >= 30 and slider >= 30 and delay <= 60)) and nodes <= 50 and task <= (3 if rate == 4 else 2) and home <= 1
    checks.append(f'- {name}、{rate}×：{"达到适用阈值" if passed else "存在未达标项"}。')
flow_list = '、'.join(f"`{flow['name']}_flow.cjs` {'通过' if flow['code']==0 else '失败'}" for flow in flows)
errors = sum(len(shots.get(key, [])) for key in ('console', 'pageErrors', 'httpErrors', 'external'))
body = f'''## 性能与流程

{table}

{chr(10).join(checks)}

旧数据取自 [wb-09b 报告](../wb-09b-out/report.md)。改后数据用同一套 Playwright/CDP 探针采集。拖动采 3 秒，空闲采 10 秒，使用 1600×1000 的独立 Edge 会话和 1／4 倍 CPU 配置。两组均启用「左前光」，左栏出小样后再开始拖动。跟手延迟是输入事件时间戳至下一次帧回调执行的差值。CPU 是 CDP 记录的网页主线程单核占用。

最大预览 FBX 来自 `exchange/tasks/20261119-191403-563c/preview/preview.fbx`，为 7,152,140 字节，按文件大小降序选择，并列时按路径选第一份。复制到隔离演示产品后重新运行测量。它与演示 FBX 的 SHA-256 都是 `d1bdd32e4249228804e26c9b21661725ebf918b303606968b021cb00848ee19f`，因此这组数据是同一几何负载的独立复测。原始 [测量记录](metrics.json) 包含帧时间、长任务、节点变动和输入延迟。

- 网页流程结果。{flow_list}。[逐项日志目录](flows.json)
- 主截图流程检查。记录到 {errors} 个控制台、页面、HTTP 或外部请求错误。[截图流程记录](shots.json)
- 旧接口检查。相同签名时零 DOM 变动、零画布绘制。用故意模拟的 HTTP 404 和 400 验证 Max 状态与校准入口降级。[兼容性记录](compat.json)
- 后端任务签名不包含执行器心跳。执行器状态片独立比较可见状态，心跳改动只更新状态片。
- 光向示意图改在提交出图、启动校准或确认入库之前生成。进入空闲后不会因视角拖动补生成整套 PNG。
'''
original = TARGET.read_text(encoding='utf-8')
marker = '## 性能与流程'
TARGET.write_text(original.split(marker)[0].rstrip()+'\n\n'+body,encoding='utf-8')
