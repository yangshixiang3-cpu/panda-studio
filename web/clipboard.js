// 把图片放进系统剪贴板，可以直接粘贴到微信、PS、PPT 里。
// 浏览器的剪贴板只收 PNG：jpg、webp 等先在页面里画一遍转成 PNG。
// 给 ClipboardItem 的是一个 Promise：读图、转格式要一点时间，这样点按钮的那一下「用户操作」不会过期。
export async function copyImage(url){
  if(!navigator.clipboard?.write||typeof ClipboardItem==='undefined')
    throw new Error('这个窗口不支持复制图片，请用「在文件夹中显示」');
  const png=(async()=>{
    const response=await fetch(url);
    if(!response.ok)throw new Error('图片读不到');
    const source=await response.blob();
    if(source.type==='image/png')return source;
    const bitmap=await createImageBitmap(source);
    const canvas=document.createElement('canvas');
    canvas.width=bitmap.width;canvas.height=bitmap.height;
    canvas.getContext('2d').drawImage(bitmap,0,0);
    bitmap.close?.();
    return new Promise((resolve,reject)=>canvas.toBlob(blob=>blob?resolve(blob):reject(new Error('图片转换失败')),'image/png'));
  })();
  try{
    await navigator.clipboard.write([new ClipboardItem({'image/png':png})]);
  }catch(error){
    throw new Error('复制失败：'+(error?.message||error));
  }
}
