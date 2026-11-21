export function syncRange(input,value=input.value){
  if(Number(input.value)!==Number(value))input.value=value;
  const fill=(Number(input.value)-Number(input.min))/(Number(input.max)-Number(input.min))*100+'%';
  if(input.style.getPropertyValue('--fill')!==fill)input.style.setProperty('--fill',fill);
}
