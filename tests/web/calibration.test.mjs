import test from 'node:test';
import assert from 'node:assert/strict';
import {describeOverrides,describeUsage} from '../../web/calibration.js';

test('轮次用量显示模型、耗时和已有 token 数，兼容旧轮次',()=>{
  assert.equal(describeUsage(), '');
  assert.equal(describeUsage({model:'gpt-6.2-sol',latency_sec:18,total_tokens:12300}), 'gpt-6.2-sol · 18 秒 · 12.3k tokens');
  assert.equal(describeUsage({model:'gpt-6.1-sol',latency_sec:0.12}), 'gpt-6.1-sol · 0.12 秒');
  assert.equal(describeUsage({model:'demo',latency_sec:1,prompt_tokens:30,completion_tokens:20}), 'demo · 1 秒 · 50 tokens');
  assert.equal(describeUsage({model:'demo',latency_sec:1,prompt_tokens:30}), 'demo · 1 秒 · 输入 30 tokens');
});

test('已套用调整显示中文说明，隐藏参数名称和默认值',()=>{
  const lines=describeOverrides({materials:{'胡桃木':{hue_shift_deg:2,brightness_mul:1.03,reflection_mul:.9,glossiness_delta:-.01,contrast_mul:1}},lights:{intensity_mul:1.1,per_light:{}},environment:{reflection_strength_mul:1},items:{'1':{lights:{rotate_deg:15}}}});
  assert.equal(lines.length,3);
  assert.match(lines[0].change,/明亮程度增加 3%/);
  assert.match(lines[0].change,/反光强度减少 10%/);
  assert.match(lines[2].change,/15°/);
  assert.doesNotMatch(JSON.stringify(lines),/brightness_mul|reflection_mul|glossiness_delta/);
  assert.deepEqual(describeOverrides(null),[]);
  assert.deepEqual(describeOverrides({materials:{'木':{brightness_mul:1}},lights:{},environment:{},items:{}}),[]);
});
