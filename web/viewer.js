import * as THREE from 'three';
import {RoomEnvironment} from 'three/addons/environments/RoomEnvironment.js';
import {parseFbx} from './model/loaders.js';
import {corners,projectPoint,verticalProjection} from './camera.js';
import {DEFAULT_LIGHTING,DEFAULT_LIGHTING_TUNING,lightDirection,lightingActive,lightingResponse,lightingText,
  previewFinishTint,previewKeyIrradiance} from './lighting.js';

// 未开打光时的预览亮度：对照同机位 Corona 成品（111(2022) 主图）调定。
const PREVIEW_HEMI=.6,PREVIEW_ENV=1.15;

export class Viewer {
  constructor(canvas){
    this.canvas=canvas;this.renderer=new THREE.WebGLRenderer({canvas,antialias:true});
    this.renderer.setPixelRatio(Math.min(devicePixelRatio,2));this.renderer.setClearColor(0xf0f0f0);
    const room=new RoomEnvironment(),pmrem=new THREE.PMREMGenerator(this.renderer);
    this.environment=pmrem.fromScene(room);
    room.dispose();pmrem.dispose();
    this.scene=new THREE.Scene();this.camera=new THREE.PerspectiveCamera(27,.75,1,1000000);
    this.scene.add(new THREE.HemisphereLight(0xffffff,0xb7b5af,2.4));
    const key=new THREE.DirectionalLight(0xffffff,2.4);key.position.set(-3000,5000,4000);this.scene.add(key);
    const fill=new THREE.DirectionalLight(0xffffff,.7);fill.position.set(4000,1000,-2000);this.scene.add(fill);
    this.originalLights=this.scene.children.filter(o=>o.isLight).map(light=>({
      light,intensity:light.intensity,previewFill:light===key?.15:light===fill?.50:0,
    }));
    this.keyLight=new THREE.DirectionalLight(0xffffff,3);this.keyLight.castShadow=true;
    this.keyLight.shadow.mapSize.set(2048,2048);this.keyLight.shadow.bias=-.0001;
    this.ambient=new THREE.HemisphereLight(0xffffff,0xb4b0a8,1);
    this.scene.add(this.keyLight,this.keyLight.target,this.ambient);
    this.ground=new THREE.Mesh(new THREE.PlaneGeometry(1,1),new THREE.ShadowMaterial({opacity:.3}));
    this.ground.rotation.x=-Math.PI/2;this.ground.receiveShadow=true;this.scene.add(this.ground);
    this.clayMaterial=new THREE.MeshStandardMaterial({color:0xd1d1d1,roughness:1,metalness:0});
    this.renderer.shadowMap.type=THREE.PCFShadowMap;
    this.renderer.shadowMap.autoUpdate=false;
    this.setLighting(DEFAULT_LIGHTING,false);
  }
  clear(){
    if(!this.model)return;
    this.scene.remove(this.model.root);this.model.dispose();this.model=null;
  }
  load(buffer,options){
    const model=parseFbx(buffer,{...options,envMap:this.environment.texture,
      anisotropy:this.renderer.capabilities.getMaxAnisotropy(),
      onTexturesChanged:()=>{
        if(this.model!==model)return;
        const missing=model.missingTextureCount;
        if(missing&&missing!==shownMissing){
          const notice=document.getElementById('model-message');
          if(notice?.textContent===modelMessage){
            modelMessage=[...model.warnings,missing+' 张贴图没找到，已用近似颜色代替。'].join(' ');
            notice.textContent=modelMessage;notice.hidden=false;
          }
          shownMissing=missing;
        }
        this.render();
      }
    });
    let shownMissing=0,modelMessage=model.warnings.join(' ');
    this.clear();this.model=model;this.scene.add(model.root);
    this.partsKey=undefined;this.box=model.box;this.applyLighting(true);
    return model;
  }
  setLighting(light,clay=false,tuning=this.lightTuning||DEFAULT_LIGHTING_TUNING){
    const previous=this.light;
    if(previous&&this.clay===clay&&Object.keys(light).every(key=>previous[key]===light[key])
      &&Object.keys(tuning).every(key=>this.lightTuning[key]===tuning[key]))return;
    const materialsChanged=this.clay!==clay||!previous||(lightingActive(previous)!==lightingActive(light));
    this.light=light;this.clay=clay;this.lightTuning=tuning;this.applyLighting(materialsChanged);
  }
  applyLighting(materialsChanged){
    const light=this.light||DEFAULT_LIGHTING,on=lightingActive(light);
    const ratios=on?lightingResponse(light,this.lightTuning):null;
    for(const original of this.originalLights){
      original.light.visible=!on||original.light.isDirectionalLight;
      // 预览模板灯和 Corona 光强单位不同，只在网页场景里做强度换算。
      original.light.intensity=on?original.intensity*original.previewFill*ratios.fill_ratio*ratios.fill_scale:
        original.light.isHemisphereLight?PREVIEW_HEMI:original.intensity;
    }
    // 未开打光：用影棚环境（RoomEnvironment）作整个场景的环境光，相当于 Corona 模板里白色环境那个大柔光箱，
    // 木纹的明度和深色台面的光泽才接近小样；开打光时去掉，保持 lighting.js 已校准的漫反射亮度。
    this.scene.environment=on?null:this.environment.texture;
    this.scene.environmentIntensity=PREVIEW_ENV;
    // 未开打光时加中性色调映射（Khronos PBR Neutral）：中低亮度颜色基本不变，只把接近纯白的高光压回来，
    // 白色、奶油白柜子的各个面才分得出明暗；画布底色是清屏色，不受影响。开打光时不用，保持校准。
    // three 会在色调映射变化时自动重编译材质。
    this.renderer.toneMapping=on?THREE.NoToneMapping:THREE.NeutralToneMapping;
    this.renderer.toneMappingExposure=1;
    this.keyLight.visible=this.ambient.visible=this.ground.visible=on;
    this.renderer.shadowMap.enabled=on;
    if(on)this.renderer.shadowMap.needsUpdate=true;
    if(!this.model)return;
    if(materialsChanged)this.model.root.traverse(mesh=>{
      if(!mesh.isMesh)return;
      mesh.castShadow=true;mesh.receiveShadow=true;
      const original=mesh.userData.previewMaterial??=mesh.material;
      const tint=previewFinishTint(mesh.name);
      if(tint!==null)for(const material of Array.isArray(original)?original:[original]){
        if(!material.color||material.transparent||material.map)continue;
        material.userData.basePreviewColor??=material.color.clone();
        if(on&&!this.clay)material.color.setHex(tint);
        else material.color.copy(material.userData.basePreviewColor);
      }
      mesh.material=on&&this.clay?this.clayMaterial:original;
    });
    if(!on)return;
    const box=this.model.box,center=box.min.map((n,i)=>(n+box.max[i])/2),diagonal=Math.hypot(...this.model.size),d=lightDirection(light);
    this.keyLight.position.set(...center.map((n,i)=>n+d[i]*ratios.key_distance*diagonal));this.keyLight.target.position.set(...center);
    this.keyLight.intensity=previewKeyIrradiance(ratios);
    // 预览半球光以 Corona 环境色实测校准，保留模板方向补光。
    this.ambient.intensity=.40*ratios.env_gi;
    this.ground.material.opacity=.3*this.keyLight.intensity/(this.keyLight.intensity+this.ambient.intensity+1);
    // 用光源的尺寸与距离之比近似矩形面光源的半影。
    this.keyLight.shadow.radius=Math.max(1,Math.min(8, 12*ratios.key_size/ratios.key_distance));
    const shadow=this.keyLight.shadow.camera;
    shadow.left=shadow.bottom=-diagonal*1.5;shadow.right=shadow.top=diagonal*1.5;shadow.near=diagonal*.01;shadow.far=diagonal*(ratios.key_distance+4.5);shadow.updateProjectionMatrix();
    this.keyLight.shadow.normalBias=diagonal*.0005;
    this.ground.position.set(center[0],box.min[1]-.3,center[2]);this.ground.scale.set(diagonal*16,diagonal*16,1);
  }
  poseCamera(item,aspect){
    const pose=item.camera,cam=this.camera,correction=verticalProjection(pose);cam.aspect=aspect;cam.fov=pose.fov_y_deg;
    cam.position.fromArray(pose.position_mm);cam.up.set(0,1,0);cam.lookAt(new THREE.Vector3().fromArray(correction?.target??pose.target_mm));
    cam.near=Math.max(.01,pose.distance_mm/10000);cam.far=Math.max(1e6,pose.distance_mm*10);cam.updateProjectionMatrix();
    if(correction){
      cam.projectionMatrix.elements[9]=correction.offsetY;
      cam.projectionMatrixInverse.copy(cam.projectionMatrix).invert();
    }
  }
  lightingImage(item,light,tuning=this.lightTuning){
    const output=document.createElement('canvas');output.width=1200;output.height=item.orientation==='landscape'?900:1600;
    const ctx=output.getContext('2d'),saved={light:this.light,clay:this.clay,tuning:this.lightTuning,ratio:this.renderer.getPixelRatio(),parts:this.item?.parts||{}};
    try{
      this.model.setParts({});this.setLighting(light,true,tuning);this.renderer.shadowMap.needsUpdate=true;
      this.renderer.setPixelRatio(1);this.renderer.setSize(output.width,output.height,false);this.poseCamera(item,output.width/output.height);
      this.renderer.render(this.scene,this.camera);ctx.drawImage(this.canvas,0,0);
      ctx.fillStyle='#fffef4e8';ctx.fillRect(18,18,970,150);ctx.fillStyle='#58402e';ctx.font='22px "Microsoft YaHei", sans-serif';
      const text=lightingText(light);let row='',y=55;
      for(const char of text){if(ctx.measureText(row+char).width>920){ctx.fillText(row,38,y);y+=34;row='';}row+=char;}ctx.fillText(row,38,y);
      // 把 PF1 光源位置投影到该项画面，示意箭头跟随相机方位。
      const center=new THREE.Vector3().fromArray(item.camera.target_mm),direction=new THREE.Vector3().fromArray(lightDirection(light));
      const p=center.clone().addScaledVector(direction,item.camera.distance_mm*.2).project(this.camera),c=center.project(this.camera);
      let dx=p.x-c.x,dy=-(p.y-c.y),len=Math.hypot(dx,dy);if(len<1e-5){dx=0;dy=-1;len=1;}
      const ex=1100,ey=112,sx=ex+dx/len*55,sy=ey+dy/len*55;
      ctx.fillStyle='#f4c65b';ctx.beginPath();ctx.arc(sx,sy,15,0,Math.PI*2);ctx.fill();ctx.strokeStyle='#a57920';ctx.lineWidth=4;
      ctx.beginPath();ctx.moveTo(sx,sy);ctx.lineTo(ex,ey);ctx.stroke();const angle=Math.atan2(ey-sy,ex-sx);
      ctx.beginPath();ctx.moveTo(ex,ey);ctx.lineTo(ex-17*Math.cos(angle-.5),ey-17*Math.sin(angle-.5));ctx.moveTo(ex,ey);ctx.lineTo(ex-17*Math.cos(angle+.5),ey-17*Math.sin(angle+.5));ctx.stroke();
    }finally{
      this.setLighting(saved.light,saved.clay,saved.tuning);this.model.setParts(saved.parts);
      this.renderer.shadowMap.needsUpdate=true;this.renderer.setPixelRatio(saved.ratio);
      const {renderWidth:width,renderHeight:height}=this;
      this.renderWidth=0;this.render(width,height);
    }
    return output;
  }
  update(item){
    this.item=item;
    if(!this.model?.parts.length)return;
    const key=this.model.parts.map(part=>item.parts?.[part.name]||0).join(',');
    if(key===this.partsKey)return;
    this.partsKey=key;this.model.setParts(item.parts||{});
    this.box=key.split(',').some(value=>Number(value)!==0)?this.model.currentBox():this.model.box;
    this.renderer.shadowMap.needsUpdate=true;
  }
  render(width=this.renderWidth,height=this.renderHeight){
    if(!this.item||!width||!height)return;
    if(width!==this.renderWidth||height!==this.renderHeight){
      this.renderer.setSize(width,height,false);
      this.renderWidth=width;this.renderHeight=height;
      this.renderer.shadowMap.needsUpdate=true;
    }
    this.poseCamera(this.item,width/height);const pose=this.item.camera,cam=this.camera;
    this.renderer.render(this.scene,cam);
    const outside=!!this.model&&corners(this.box).some(p=>{const q=projectPoint(p,pose,cam.aspect);return q[2]<=0||Math.abs(q[0])>1||Math.abs(q[1])>1;});
    if(outside!==this.outside){
      this.outside=outside;this.canvas.parentElement.classList.toggle('outside',outside);
      document.getElementById('outside-note').hidden=!outside;
    }
  }
}
