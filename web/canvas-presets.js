// 画布的提示词预设：几条内置的常用写法 + 自己存的（存在 canvas/prefs.json）。点一下追加到提示词里。
export const BUILTIN_PRESETS=[
  '白底产品图：纯白背景，产品完整居中，柔和自然的接地阴影，电商主图',
  '换材质：保持产品造型、尺寸和视角不变，只把柜体换成参考图里的材质和颜色',
  '场景图：把产品放进温馨明亮的客厅，真实的室内光线，产品是画面主体',
  '细节特写：靠近拍摄产品的拉手、木纹和边角，清晰展示做工',
  '去掉背景：只保留产品，背景换成纯白',
  '高清修复：内容和构图都不变，提高清晰度、修正噪点和模糊',
];

// 追加到提示词：空的直接放，已有内容用逗号接上，重复的不加
export function appendPrompt(prompt,text){
  const current=String(prompt||'').trim();
  if(!current)return text;
  if(current.includes(text))return current;
  return current.replace(/[，,。.\s]+$/,'')+'，'+text;
}
