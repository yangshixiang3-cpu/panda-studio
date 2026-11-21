// 饰面近似色表与材质类型判断：不依赖 three.js，网页各处和 Node 单测都能直接导入。
// 预览材质（model/loaders.js）、柜子识别取名与色块（variants.js）、参考库选图窗口的色块（calibration.js）共用。
export const finishes=[
  ['黑胡桃',0x4a3426,.6],['焦糖胡桃',0x6e4a30,.6],['胡桃',0x5e4130,.6],
  ['摩卡红橡',0x7a5644,.6],['红橡',0x9a6a4c,.6],['樱桃',0x8b4a35,.6],['柚木',0x9b6b3f,.6],
  ['北欧橡木',0xc4a47c,.6],['橡木',0xc4a47c,.6],['原木',0xc4a47c,.6],
  ['白蜡',0xc9b08c,.6],['枫木',0xd8c19a,.6],
  ['纯黑',0x1c1c1c,.5],['哑黑',0x1c1c1c,.5],['黑色',0x1c1c1c,.5],['黑',0x1c1c1c,.5],
  ['奶油白',0xece4d6,.55],['白色',0xeeeae3,.55],['象牙',0xeeeae3,.55],['白',0xeeeae3,.55],
  ['幻影灰',0x8d8f8e,.55],['浅灰',0xb7b8b6,.55],['深灰',0x6e6f70,.55],['灰',0x6e6f70,.55],
].sort((a,b)=>b[0].length-a[0].length);

// 材质类型：玻璃 / 镜子 / 五金金属 / 饰面（板材、台面等）。柜子识别取名和色块只看饰面。
export function materialKind(name=''){
  if(/镜|mirror/i.test(name))return 'mirror';
  if(/玻|glass/i.test(name))return 'glass';
  if(/五金|金属|不锈钢|银|铝|拉手|插座|开关|金|铜|香槟|steel|metal/i.test(name))return 'metal';
  return 'finish';
}
// 按材质名取近似饰面色（sRGB 十六进制数），认不出返回 null。
export function finishColorOf(name=''){
  return finishes.find(([word])=>name.includes(word))?.[1]??null;
}
