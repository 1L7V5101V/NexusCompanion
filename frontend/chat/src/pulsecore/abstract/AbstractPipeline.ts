// Abstract 黑洞渲染管线：完整移植 Shadertoy fXVGDm（mrange "Abstract black hole"，CC0）
//
// Shadertoy 语义还原：
//   Buffer A 每帧光线步进，末尾与自身上帧输出做 0.9 混合（子像素抖动时间累积/TAA），
//   iChannel0 = Buffer A 自己 → 一对 ping-pong 目标；
//   原作 Image pass 是 1:1 直通（texelFetch iChannel0）——管线按 scale 缩小渲染，
//   这里换成 OVERLAY 线性放大 blit 上屏（uGain=1，语义等价）。
//
// 相对原作的适配（原作 glsl 文件零修改，全部在加载层做字符串替换）：
//   d=l-.7*uH + g=-p*uH/(l*L)
//                                       uH=uHoleBase*(1+uHorizonAmp*env)：默认
//                                       uHorizonAmp=0——洞本体（影子）与引力场恒定，
//                                       星空透镜静止，心跳只作用于盘/云/外围光；
//                                       >0 恢复"洞+引力随心跳胀缩"（光子球随视界
//                                       同步缩放的 GR 关系由 uH 保证；会带动星空位移）
//   N=floor(diskInv(Z)/STEP) + rw*diskF(rw)
//                                       盘响应：径向翘曲场 diskF——内缘严格跟随洞
//                                       （ISCO 随质量），心跳扰动向外按 exp(-x/λ) 衰减、
//                                       按 uPertLag（拍/单位半径）相位延迟传播；
//                                       静态大小项用固定锥度（λ 只属于心跳扰动），
//                                       外缘基本不动（简化的轨道周期 r^1.5 响应）；
//                                       diskInv 数值逆解 u·F(u)=Z（牛顿×2）
//   P=.../R.y+uMouseOff                 鼠标视差：靠近核心时初始光线轻微偏移
//   iTime/ → uSwirlTime/                吸积盘旋转时间由 JS 按状态积分（换挡无相位跳变）
//   o/=5e3; 前插 o*=uGain               曝光增益（tonemap 前）：状态/音频/鼠标能量/闪烁
//   vec4 O; 后补 o=vec3(0)              原作依赖未初始化局部量归零，显式化防驱动差异
//   i<99&&z<29. → ×uHoleBase            步进预算随洞尺寸缩放（大洞的透镜弧路径更长）
//
// envWave（GLSL）与 oscOf（TS）是同一包络的两份实现，改波形公式必须两边同步。
//
// AI 状态只调制节奏/亮度/旋转/闪烁，不改原作配色（沿用 Kerr-Newman 版的定案）。
// 所有动力学参数（状态剖面/心跳波形/耦合强度/鼠标交互/盘响应/渲染）都暴露 setter，
// demo 调参面板实时驱动；React 集成时可挑常用的提为 props。

import * as THREE from 'three';
import { SHIM_PREFIX, SHIM_MAIN, FULLSCREEN_VERT, massOscillation } from '../shadertoy';
import type { PulseCoreState } from '../PulseCorePipeline';
import bufferASrc from './shaders/bufferA.glsl?raw';

// 超采样 blit：scale>1 时对输出像素脚印做 4 tap 盒式降采样（空间抗锯齿，
// 替代低时间累积下失效的 TAA）；scale≤1 时退化为单次线性采样（放大）。
const SS_BLIT_FRAG = /* glsl */ `
precision highp float;
uniform sampler2D uTex;
uniform vec2 uOutRes;   // 输出（画布）分辨率
uniform vec2 uSrcRes;   // 源 RT 分辨率
uniform float uSS;      // 超采样倍率（=内部 scale，>1 生效）
uniform float uGain;
out vec4 fragColor;
void main() {
    vec2 uv = gl_FragCoord.xy / uOutRes;
    vec3 col;
    if (uSS > 1.0) {
        vec2 o = 0.5 * uSS / uSrcRes;
        col = 0.25 * (texture(uTex, uv + o).rgb + texture(uTex, uv - o).rgb
                    + texture(uTex, uv + vec2(o.x, -o.y)).rgb + texture(uTex, uv + vec2(-o.x, o.y)).rgb);
    } else {
        col = texture(uTex, uv).rgb;
    }
    fragColor = vec4(col * uGain, 1.0);
}
`;

export interface StateProfile {
    bpm: number;         // 脉搏（次/分）
    holeAmp: number;     // 盘/云径向脉动幅度（心跳驱动，本体不随动）；0.03 = 峰值外扩 3%
    gain: number;        // 基准曝光增益
    swirl: number;       // 吸积盘旋转速度倍率
    flicker: number;     // 闪烁强度（error）
    wobble: number;      // 心律不齐强度（error：每拍随机化频率与幅度）
}

export type WaveShape = 'continuous' | 'lubdub' | 'pulse';

const STATE_PROFILES: Record<PulseCoreState, StateProfile> = {
    // 四状态剖面均为用户定稿值（2026-09-28 调参面板验收，idle 为基准）
    idle:      { bpm: 20,  holeAmp: 0.038, gain: 0.69, swirl: 0.90, flicker: 0.0,  wobble: 0 },
    thinking:  { bpm: 20,  holeAmp: 0.056, gain: 0.69, swirl: 2.0,  flicker: 0.0,  wobble: 0 },
    streaming: { bpm: 30,  holeAmp: 0.076, gain: 0.69, swirl: 0.90, flicker: 0.0,  wobble: 0 },
    error:     { bpm: 8,   holeAmp: 0.038, gain: 0.69, swirl: 0.2,  flicker: 0.2,  wobble: 0 },
};

// 注入的 uniform 声明与脉动/盘响应函数（shim 之后、原作代码之前）。
// envWave 必须与 TS 侧 oscOf() 公式一致（波形三态），两边同步改。
const UNIFORM_DECLS = /* glsl */ `
uniform float uHoleBase;    // 洞基础尺寸（holeSize，1 = 原作默认构图）
uniform float uPulseAmp;    // 盘/云脉动幅度（分数，含每拍抖动 + 音频耦合）
uniform float uHorizonAmp;  // 洞本体+引力随心跳脉动强度（0=恒定，默认；1=随心跳胀缩）
uniform float uGlowK;       // 辉光脉动：心跳对内盘发射强度的调制系数
uniform float uPhase;       // 心跳相位（拍，已 mod 1）
uniform float uPertLam;     // 盘扰动衰减长度（世界单位）
uniform float uPertLag;     // 盘扰动传播延迟（拍/单位半径）
uniform int   uWave;        // 波形 0 连续正弦 / 1 双峰 lub-dub / 2 快脉冲
uniform float uGain;        // 曝光增益（tonemap 前）
uniform vec2  uMouseOff;    // 鼠标视差偏移（P 空间）
uniform float uSwirlTime;   // 吸积盘旋转时间（JS 积分）
uniform float uFeedback;    // 时间累积混合（0=无残影，0.9=原作 TAA）
uniform float uJitter;      // 亚像素抖动开关（累积关掉时一并关，防边缘爬行）
uniform float uStarGain;    // 背景星空亮度（0=无星）
uniform float uStarLens;    // 星空采样方向：0=初始方向（完全静止，默认）1=弯折方向（透镜拉弯但随脉动漂）

// 星空：原样移植自 fXV3Wm Kerr-Newman 版的星场函数（用户点名的背景元素）。
// 方向向量驱动 → 用弯折后的 crd 采样时星星会被引力透镜拉弯、视界内被吞掉。
vec4 hash43x(vec3 p) {
    uvec3 x = uvec3(ivec3(p));
    x = 1103515245U*((x.xyz >> 1U)^(x.yzx));
    uint h = 1103515245U*((x.x^x.z)^(x.y>>3U));
    uvec4 rz = uvec4(h, h*16807U, h*48271U, h*69621U);
    return vec4((rz >> 1) & uvec4(0x7fffffffU))/float(0x7fffffff);
}
vec3 stars(vec3 p) {
    vec3 col = vec3(0); float rad = .087*iResolution.y; float dens = 0.15; float id = 0.; float z = 1.;
    for (float i = 0.; i < 5.; i++) {
        p *= mat3(0.86564, -0.28535, 0.41140, 0.50033, 0.46255, -0.73193, 0.01856, 0.83942, 0.54317);
        vec3 q = abs(p); vec3 p2 = p/max(q.x, max(q.y,q.z)); p2 *= rad;
        vec3 ip = floor(p2 + 1e-5); vec3 fp = fract(p2 + 1e-5);
        vec4 rand = hash43x(ip*283.1); vec3 q2 = abs(p2);
        vec3 pl = 1.0- step(max(q2.x, max(q2.y, q2.z)), q2);
        vec3 pp = fp - ((rand.xyz-0.5)*.6 + 0.5)*pl;
        float pr = length(ip) - rad;
        if (rand.w > (dens - dens*pr*0.035)) pp += 1e6;
        float d = dot(pp, pp) / (pow(fract(rand.w*172.1), 32.) + .25);
        float bri = dot(rand.xyz*(1.-pl),vec3(1));
        id = fract(rand.w*101.);
        col += bri*z*.00009/pow(d + 0.025, 3.0)*(mix(vec3(1.0,0.45,0.1),vec3(0.75,0.85,1.), id)*0.6+0.4);
        rad = floor(rad*1.08); dens *= 1.45; z *= 0.6; p = p.yxz;
    }
    return col;
}

float envWave(float ph) {
  ph-=floor(ph);
  if(uWave==1){
    float t1=(ph-.18)/.055;
    float t2=(ph-.42)/.045;
    return min(1., exp(-t1*t1*.5)+.55*exp(-t2*t2*.5));
  }
  if(uWave==2)return exp(-ph*5.);
  return .5-.5*cos(6.283185307*ph);
}
// 辉光脉动：心跳包络调制内盘各环的发射强度——亮区随拍胀缩（洞顶白弧是
// 远侧内缘的透镜像，内缘环 flare 时白弧同步缩放），沿与几何涟漪相同的
// 衰减+延迟向外传播；幅度与盘脉动幅度成比例（各状态自动差异化）
float glowF(float r) {
  float x=max(0., r-1.);
  return 1.+uGlowK*uPulseAmp*envWave(uPhase-uPertLag*x)*exp(-x/uPertLam);
}
// 盘径向翘曲场：r 为 u 空间（等距环）半径，返回物理半径倍率。
// 静态项（大小）与动态项（心跳）解耦：
//   x=0（内缘 u=1.0）处两者之和严格等于洞因子（内缘拴在 ISCO 上）；
//   静态项用固定锥度 exp(-x/1.2)——大小改变的是"另一个质量黑洞的稳态盘"，
//   不随 λ 变；λ 只管心跳扰动的传播衰减，外盘都不动。
const float STATIC_TAPER=1.2;     // 静态大小项的固定锥度（λ 只属于心跳扰动；diskInv 导数同步用它）
float diskF(float r) {
  float x=max(0., r-1.);
  return 1.+(uHoleBase-1.)*exp(-x/STATIC_TAPER)+uHoleBase*uPulseAmp*envWave(uPhase-uPertLag*x)*exp(-x/uPertLam);
}
// Z→u 的数值逆（解 u·F(u)=Z）：一阶近似起步 + 两步牛顿。
// 导数近似 F'≈-(F-1)/STATIC_TAPER——静态项主导 F 的变化率，动态项的 λ/延迟
// 导数是高阶小量；这里若误用心跳的 λ 会让反解随 λ 漂移（环带错位、云消失）。
float diskInv(float Z) {
  float u=Z/diskF(Z);
  for(int i=0;i<2;++i){
    float Fu=diskF(u);
    u-=(u*Fu-Z)/(Fu-u*(Fu-1.)/STATIC_TAPER);
  }
  return u;
}
`;

const smoothStep = (a: number, b: number, x: number): number => {
    const t = Math.max(0, Math.min(1, (x - a) / (b - a)));
    return t * t * (3 - 2 * t);
};

interface RT {
    rt: THREE.WebGLRenderTarget;
    w: number;
    h: number;
}

export class AbstractPipeline {
    private renderer: THREE.WebGLRenderer;
    private triScene = new THREE.Scene();
    private triCamera = new THREE.Camera();
    private triangle: THREE.BufferGeometry;
    private passes: Record<'bufA' | 'blit', THREE.RawShaderMaterial>;
    private rtA: [RT, RT];   // ping-pong（Buffer A 自反馈历史）
    private flip = 0;

    private frameNo = 0;
    private time = 0;
    private swirlTime = 0;       // 状态化旋转时间
    private heartPhase = 0;      // 心跳相位（拍数，可非整数）
    private beatIndex = 0;
    private rateJitter = 1;      // error 心律不齐：本拍频率/幅度抖动
    private ampJitter = 1;
    private lastNow = 0;
    private raf = 0;
    private frameAcc = 0;
    private disposed = false;

    // 状态机
    private state: PulseCoreState = 'idle';
    private cur: StateProfile = { ...STATE_PROFILES.idle };
    private profiles: Record<PulseCoreState, StateProfile>;  // 可调状态剖面（实例级）
    private waveShape: WaveShape = 'lubdub';
    private audioEnergy = 0;
    private energySmooth = 0;

    // 耦合与交互参数（用户定稿默认值，2026-09-28；demo 调参面板实时改）
    private oscGainK = 0.2;       // 心跳→亮度
    private audioGainK = 0.15;    // 音频→亮度
    private audioScaleK = 0.012;  // 音频→脉动幅度
    private pertLam = 1.45;       // 盘扰动衰减长度（世界单位）：越大盘跟随越多
    private pertLag = 0.33;       // 盘扰动传播延迟（拍/单位半径）：涟漪外传速度
    private mouseOffsetK = 0.01;  // 鼠标视差幅度
    private mouseBoost = 0.35;    // 鼠标→能量增强
    private proxNear = 0.12;      // 接近半径内缘（P 空间，半高=1）
    private proxFar = 0.4;        // 接近半径外缘
    private feedback = 0.05;      // 时间累积混合：0=无残影，0.9=原作 TAA 手感
    private starGain = 1;         // 背景星空亮度
    private starLens = 0;         // 星空采样方向（0=初始 rd 静止，1=弯折 crd 随盘脉动漂）
    private horizonAmp = 0;       // 洞本体+引力随心跳脉动（0=恒定，星空不受扰；1=全随动）
    private glowK = 8;            // 辉光脉动系数：白弧/亮带随心跳 flare 的强度

    // 鼠标
    private hasPointer = false;
    private pointerP = new THREE.Vector2();      // P 空间坐标（半高=1）
    private proxSmooth = 0;                      // 靠近核心程度 0..1
    private mouseOff = new THREE.Vector2();
    private mouseOffTarget = new THREE.Vector2();

    private canvas: HTMLCanvasElement;
    private scale: number;
    private fpsCap: number;
    private holeSize = 0.6;      // 基础尺寸倍率（用户定稿默认 0.6；1.0 = 原作构图）
    private resizeObserver: ResizeObserver;

    constructor(canvas: HTMLCanvasElement, opts?: { scale?: number; fpsCap?: number }) {
        this.canvas = canvas;
        this.profiles = structuredClone(STATE_PROFILES);
        this.scale = opts?.scale ?? 1.25;
        // 120 上限：shader 很便宜，跟垂直同步走（100Hz 屏=100fps）——
        // 残影时长=时间常数/帧率，帧率越高运动越干净
        this.fpsCap = opts?.fpsCap ?? 120;

        this.renderer = new THREE.WebGLRenderer({
            canvas,
            antialias: false,
            depth: false,
            stencil: false,
            powerPreference: 'high-performance',
        });
        this.renderer.autoClear = false;
        const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
        this.renderer.setPixelRatio(1);
        this.renderer.setSize(Math.floor((canvas.clientWidth || 800) * dpr), Math.floor((canvas.clientHeight || 600) * dpr), false);

        // 全屏三角形（属性名必须是 position，three 用它计算 drawRange）
        this.triangle = new THREE.BufferGeometry();
        this.triangle.setAttribute('position', new THREE.BufferAttribute(new Float32Array([-1, -1, 3, -1, -1, 3]), 2));

        // 加载层适配原作 Buffer A（正文零修改；替换点见文件头注释）
        const aSrc = bufferASrc
            .replace('float j,z=0.,d,D,L,l,s,N,a,H,A,Z;', 'float j,z=0.,d,D,L,l,s,N,a,H,A,Z,rw;')
            // 洞本体与引力场：uH=uHoleBase*(1+uHorizonAmp*env)——默认 uHorizonAmp=0，
            // 洞大小与引力恒定（星空透镜完全静止），脉动只作用于盘/云/外围光；
            // >0 恢复"洞+引力随心跳胀缩"（大洞时靠 uH 保证光子球 ∝ 视界，弧不被吞）
            .replace('d=l-.7;', 'float uH=uHoleBase*(1.+uHorizonAmp*envWave(uPhase));d=l-.7*uH;')
            // 引力随质量缩放：光子球 ∝ 视界 ∝ M（GR 关系），弧始终贴着洞缘
            .replace('g=-p/(l*L);', 'g=-p*uH/(l*L);')
            // 盘响应：径向翘曲——内环随洞进出，扰动向外衰减+延迟传播
            .replace('N=clamp(floor(length(p.xz)/STEP+.5),1./STEP,6./STEP);',
                'N=clamp(floor(diskInv(Z)/STEP+.5),1./STEP,6./STEP);')
            .replace('w=vec2(abs(Z-STEP*(N+j/(2.*REPS+1.))), ',
                'rw=STEP*(N+j/(2.*REPS+1.));w=vec2(abs(Z-rw*diskF(rw)), ')
            // 辉光脉动：内盘发射强度随心跳 flare（白弧/亮带胀缩），几何不动
            .replace('O=(160.*(1.-dot(crd,rd))+1.+sin(a-1.*(N*STEP)+2.5*H+vec4(7,2,9,7)))/(1.+N*STEP);',
                'O=(160.*(1.-dot(crd,rd))+1.+sin(a-1.*(N*STEP)+2.5*H+vec4(7,2,9,7)))/(1.+N*STEP);O*=glowF(N*STEP);')
            // 鼠标视差：P 空间偏移初始光线
            .replace('P=(C+C-R)/R.y', 'P=(C+C-R)/R.y+uMouseOff')
            // 状态化旋转：吸积盘角速度由 JS 积分（状态切换无相位跳变）
            .replace('iTime/', 'uSwirlTime/')
            // 曝光增益：tonemap 前乘（亮度呼吸/音频/鼠标能量/闪烁都在 uGain 里）
            .replace('o/=5e3;', 'o*=uGain;\n  o/=5e3;')
            // 背景星空：只给逃逸光线加（视界内 d<1e-2=被吞，不加）；
            // 盘面辉光亮处权重衰减（星星被辉光淹没，物理观感）。
            // 采样方向默认用初始 rd（完全静止）——用弯折后的 crd 会让步进
            // （步长=min(球距,盘环距)）受盘面脉动影响而微动 → 星点漂移
            .replace('o*=smoothstep(.0,.2,dot(o,vec3(.299, .587, .114)));',
                'o*=smoothstep(.0,.2,dot(o,vec3(.299, .587, .114)));\n  if(d>1e-2)o+=stars(mix(rd,crd,uStarLens))*uStarGain*(1.0-smoothstep(0.05,0.35,dot(o,vec3(.299,.587,.114))));')
            // 时间累积可调：0.9 是原作的 TAA（静止抗锯齿，运动=残影）；
            // uJitter 与之联动——没有累积时抖动只剩噪声
            .replace('C+=jitter(iFrame);', 'C+=jitter(iFrame)*uJitter;')
            .replace('.xyz,.9);', '.xyz,uFeedback);')
            // 原作依赖未初始化局部量归零，显式化（驱动差异防御）
            .replace('vec4 O;', 'vec4 O;\n  o=vec3(0);')
            // 步进预算随洞尺寸缩放：光弧来自贴洞绕行的长路径光线，洞变大后
            // 99 步不够烧，上缘白弧会在绕行途中断掉（被"吞"）。
            // 上界只放宽到 130（弧路径只多 ~1.2×：贴洞段随 h 变长，其余段不变）；
            // 不要用动态上界或更大的常量——ANGLE/D3D 的 FXC 前者编译劣化
            // （帧率崩）、后者展开爆炸（主线程冻结几十秒）
            .replace('i<99&&z<29.', 'i<130&&z<29.');
        for (const marker of ['uHoleBase', 'uPulseAmp', 'uHorizonAmp', 'uGlowK', 'glowF(', 'envWave(', 'diskF(', 'diskInv(', 'uSwirlTime/', 'uGain;\n', 'g=-p*uH', 'uFeedback', 'jitter(iFrame)*', 'stars(mix(rd,crd', 'uStarGain', 'uStarLens']) {
            if (!aSrc.includes(marker)) console.warn('[PulseCore:abstract] 原作源码漂移，替换点未命中：', marker);
        }

        const mkUniforms = () => ({
            iResolution: { value: new THREE.Vector3(1, 1, 1) },
            iTime: { value: 0 },
            iTimeDelta: { value: 0 },
            iFrame: { value: 0 },
            iMouse: { value: new THREE.Vector4(0, 0, -1, -1) },
            iChannelResolution: { value: [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()] },
            iChannel0: { value: null },
            iChannel1: { value: null },
            iChannel2: { value: null },
            iChannel3: { value: null },
            uHoleBase: { value: 1 },
            uPulseAmp: { value: 0 },
            uHorizonAmp: { value: 0 },
            uGlowK: { value: 8 },
            uPhase: { value: 0 },
            uPertLam: { value: 1.2 },
            uPertLag: { value: 0.12 },
            uWave: { value: 0 },
            uGain: { value: 1 },
            uMouseOff: { value: new THREE.Vector2(0, 0) },
            uSwirlTime: { value: 0 },
            uFeedback: { value: 0.05 },
            uJitter: { value: 1 },
            uStarGain: { value: 1 },
            uStarLens: { value: 0 },
        });

        this.passes = {
            bufA: new THREE.RawShaderMaterial({
                glslVersion: THREE.GLSL3,
                vertexShader: FULLSCREEN_VERT,
                fragmentShader: SHIM_PREFIX + UNIFORM_DECLS + aSrc + SHIM_MAIN,
                depthTest: false,
                depthWrite: false,
                uniforms: mkUniforms(),
            }),
            blit: new THREE.RawShaderMaterial({
                glslVersion: THREE.GLSL3,
                vertexShader: FULLSCREEN_VERT,
                fragmentShader: SS_BLIT_FRAG,
                depthTest: false,
                depthWrite: false,
                uniforms: {
                    uTex: { value: null },
                    uOutRes: { value: new THREE.Vector2(1, 1) },
                    uSrcRes: { value: new THREE.Vector2(1, 1) },
                    uSS: { value: 0 },
                    uGain: { value: 1 },
                },
            }),
        };

        const w = Math.max(2, Math.floor((canvas.clientWidth || 800) * dpr * this.scale));
        const h = Math.max(2, Math.floor((canvas.clientHeight || 600) * dpr * this.scale));
        this.rtA = [this.mkRT(w, h), this.mkRT(w, h)];

        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(canvas);

        window.addEventListener('pointermove', this.onPointerMove);
        document.documentElement.addEventListener('pointerleave', this.onPointerLeave);
    }

    private mkRT(w: number, h: number): RT {
        const rt = new THREE.WebGLRenderTarget(w, h, {
            // 16F：0..1 值域足够（tanh 后），WebGL2 核心支持 16F 线性过滤
            type: THREE.HalfFloatType,
            format: THREE.RGBAFormat,
            minFilter: THREE.LinearFilter,
            magFilter: THREE.LinearFilter,
            wrapS: THREE.ClampToEdgeWrapping,
            wrapT: THREE.ClampToEdgeWrapping,
            depthBuffer: false,
            stencilBuffer: false,
            generateMipmaps: false,
        });
        return { rt, w, h };
    }

    private onPointerMove = (e: PointerEvent) => {
        const r = this.canvas.getBoundingClientRect();
        const w = r.width || 1, h = r.height || 1;
        // P 空间坐标（原作以半高归一）；gl_FragCoord.y 自下而上 → 指针 y 翻转
        this.pointerP.set((2 * (e.clientX - r.left) - w) / h, (2 * (h - (e.clientY - r.top)) - h) / h);
        this.hasPointer = true;
    };

    private onPointerLeave = () => { this.hasPointer = false; };

    private resize() {
        const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
        const cw = this.canvas.clientWidth || 800;
        const ch = this.canvas.clientHeight || 600;
        this.renderer.setSize(Math.floor(cw * dpr), Math.floor(ch * dpr), false);
        const w = Math.max(2, Math.floor(cw * dpr * this.scale));
        const h = Math.max(2, Math.floor(ch * dpr * this.scale));
        for (const old of this.rtA) { old.rt.dispose(); }
        this.rtA = [this.mkRT(w, h), this.mkRT(w, h)];
        this.flip = 0;
    }

    setState(s: PulseCoreState) { this.state = s; }
    setAudioEnergy(e: number) { this.audioEnergy = Math.max(0, Math.min(1, e)); }
    /** 洞基础尺寸倍率：1.0 = 原作默认构图。只缩洞与盘内缘（ISCO），外盘不动，范围可放宽 */
    setHoleSize(v: number) { this.holeSize = Math.max(0.3, Math.min(2, v)); }

    // ---- 调参 API（demo 调参面板实时驱动）----

    /** 覆盖某状态的剖面字段（bpm/holeAmp/gain/swirl/flicker/wobble） */
    setProfile(s: PulseCoreState, patch: Partial<StateProfile>) {
        const clamps: Record<keyof StateProfile, [number, number]> = {
            bpm: [0, 200], holeAmp: [0, 0.15], gain: [0.2, 2], swirl: [0, 4], flicker: [0, 1], wobble: [0, 1],
        };
        for (const [k, v] of Object.entries(patch)) {
            if (v === undefined) continue;
            const key = k as keyof StateProfile;
            const [lo, hi] = clamps[key];
            this.profiles[s][key] = Math.max(lo, Math.min(hi, v));
        }
    }

    getProfile(s: PulseCoreState): StateProfile { return { ...this.profiles[s] }; }

    /** 心跳波形：continuous 连续正弦（默认）/ lubdub 双峰 / pulse 快脉冲 */
    setWaveShape(w: WaveShape) { this.waveShape = w; }

    /** 耦合强度：心跳→亮度 / 音频→亮度 / 音频→脉动幅度 */
    setDynamics(patch: { oscGainK?: number; audioGainK?: number; audioScaleK?: number }) {
        if (patch.oscGainK !== undefined) this.oscGainK = Math.max(0, Math.min(0.3, patch.oscGainK));
        if (patch.audioGainK !== undefined) this.audioGainK = Math.max(0, Math.min(0.8, patch.audioGainK));
        if (patch.audioScaleK !== undefined) this.audioScaleK = Math.max(0, Math.min(0.05, patch.audioScaleK));
    }

    /** 盘响应：扰动衰减长度 λ（世界单位）/ 传播延迟（拍/单位半径） */
    setPert(patch: { lam?: number; lag?: number }) {
        if (patch.lam !== undefined) this.pertLam = Math.max(0.2, Math.min(4, patch.lam));
        if (patch.lag !== undefined) this.pertLag = Math.max(0, Math.min(0.6, patch.lag));
    }

    /** 时间累积混合：0=无运动残影（同时关亚像素抖动），0.9=原作 TAA 手感 */
    setFeedback(v: number) { this.feedback = Math.max(0, Math.min(0.95, v)); }

    /** 背景星空亮度：0=无星，1=kerr 原版观感 */
    setStarGain(v: number) { this.starGain = Math.max(0, Math.min(3, v)); }

    /** 洞本体+引力随心跳的脉动强度：0=洞大小恒定（默认，星空不受脉动影响），1=随心跳胀缩 */
    setHorizonAmp(v: number) { this.horizonAmp = Math.max(0, Math.min(1, v)); }

    /** 星空采样方向：0=初始方向（完全静止，默认），1=弯折方向（被引力透镜拉弯，但随盘脉动漂移） */
    setStarLens(v: number) { this.starLens = Math.max(0, Math.min(1, v)); }

    /** 辉光脉动系数：内盘发射（白弧/亮带）随心跳 flare 的强度，0=只余几何涟漪 */
    setGlowK(v: number) { this.glowK = Math.max(0, Math.min(20, v)); }

    /** 鼠标交互：视差幅度 / 能量增强 / 接近内缘 / 接近外缘（P 空间，半高=1） */
    setMouseParams(patch: { offsetK?: number; boost?: number; near?: number; far?: number }) {
        if (patch.offsetK !== undefined) this.mouseOffsetK = Math.max(0, Math.min(0.5, patch.offsetK));
        if (patch.boost !== undefined) this.mouseBoost = Math.max(0, Math.min(1.5, patch.boost));
        if (patch.near !== undefined) this.proxNear = Math.max(0, Math.min(this.proxFar, patch.near));
        if (patch.far !== undefined) this.proxFar = Math.max(this.proxNear + 0.05, Math.min(2, patch.far));
    }

    /** 内部渲染分辨率系数：>1 为超采样（空间抗锯齿，代价∝平方），重建渲染目标 */
    setScale(v: number) {
        this.scale = Math.max(0.25, Math.min(1.5, v));
        this.resize();
    }

    setFpsCap(v: number) { this.fpsCap = Math.max(10, Math.min(120, v)); }

    private oscOf(phase: number): number {
        if (this.waveShape === 'lubdub') {
            const f = phase % 1;
            const g = (c: number, w: number) => Math.exp(-((f - c) ** 2) / (2 * w * w));
            return Math.min(1, g(0.18, 0.055) + 0.55 * g(0.42, 0.045));
        }
        if (this.waveShape === 'pulse') {
            const f = phase % 1;
            return Math.exp(-f * 5);
        }
        return massOscillation(phase);
    }

    start() {
        this.lastNow = performance.now();
        const loop = (now: number) => {
            if (this.disposed) return;
            this.raf = requestAnimationFrame(loop);
            if (document.hidden) { this.lastNow = now; return; }
            this.frameAcc += Math.min(0.1, (now - this.lastNow) / 1000);
            this.lastNow = now;
            if (this.frameAcc < 1 / this.fpsCap) return;
            const dt = this.frameAcc;
            this.frameAcc = 0;
            this.frame(dt);
        };
        this.raf = requestAnimationFrame(loop);
    }

    stop() { cancelAnimationFrame(this.raf); }

    private frame(dt: number) {
        this.frameNo++;
        this.time += dt;
        const p = this.profiles[this.state];

        // 状态参数平滑过渡
        const k = 1 - Math.exp(-dt * 3);
        this.cur.bpm += (p.bpm - this.cur.bpm) * k;
        this.cur.holeAmp += (p.holeAmp - this.cur.holeAmp) * k;
        this.cur.gain += (p.gain - this.cur.gain) * k;
        this.cur.swirl += (p.swirl - this.cur.swirl) * k;
        this.cur.flicker += (p.flicker - this.cur.flicker) * k;
        this.cur.wobble += (p.wobble - this.cur.wobble) * k;

        // 音频能量包络（attack 快 / release 慢）
        this.energySmooth += (this.audioEnergy - this.energySmooth) * (1 - Math.exp(-dt * (this.audioEnergy > this.energySmooth ? 12 : 3)));

        // 心跳：连续振荡；error 每拍随机化频率与幅度（心律不齐）
        const beat = Math.floor(this.heartPhase);
        if (beat !== this.beatIndex) {
            this.beatIndex = beat;
            this.rateJitter = 1 + (Math.random() - 0.5) * 0.9 * this.cur.wobble;
            this.ampJitter = 1 + (Math.random() - 0.5) * 0.6 * this.cur.wobble;
        }
        this.heartPhase += dt * this.cur.bpm * this.rateJitter / 60;
        const osc = this.oscOf(this.heartPhase);

        // 鼠标：靠近核心程度 → 视差偏移 + 能量增强
        const proxT = this.hasPointer ? 1 - smoothStep(this.proxNear, this.proxFar, Math.hypot(this.pointerP.x, this.pointerP.y)) : 0;
        this.proxSmooth += (proxT - this.proxSmooth) * (1 - Math.exp(-dt * (proxT > this.proxSmooth ? 10 : 3)));
        this.mouseOffTarget.set(this.pointerP.x * this.mouseOffsetK * this.proxSmooth, this.pointerP.y * this.mouseOffsetK * this.proxSmooth);
        this.mouseOff.lerp(this.mouseOffTarget, 1 - Math.exp(-dt * 6));

        const flick = 1 - this.cur.flicker * Math.max(0, Math.sin(this.time * 23.0) * Math.sin(this.time * 7.3));
        const gain = this.cur.gain * (1 + this.oscGainK * osc + this.energySmooth * this.audioGainK) * (1 + this.proxSmooth * this.mouseBoost) * flick;
        this.swirlTime += dt * this.cur.swirl;

        const w = this.rtA[0].w, h = this.rtA[0].h;
        // ---- Buffer A：光线步进 + 上帧自反馈混合 ----
        const m = this.passes.bufA;
        m.uniforms.iResolution.value.set(w, h, w / h);
        m.uniforms.iTime.value = this.time;
        m.uniforms.iTimeDelta.value = dt;
        m.uniforms.iFrame.value = this.frameNo;
        m.uniforms.uHoleBase.value = this.holeSize;
        m.uniforms.uPulseAmp.value = this.cur.holeAmp * this.ampJitter + this.energySmooth * this.audioScaleK;
        m.uniforms.uPhase.value = this.heartPhase - Math.floor(this.heartPhase);
        m.uniforms.uPertLam.value = this.pertLam;
        m.uniforms.uPertLag.value = this.pertLag;
        m.uniforms.uWave.value = this.waveShape === 'lubdub' ? 1 : this.waveShape === 'pulse' ? 2 : 0;
        m.uniforms.uGain.value = gain;
        m.uniforms.uMouseOff.value.copy(this.mouseOff);
        m.uniforms.uSwirlTime.value = this.swirlTime;
        m.uniforms.uFeedback.value = this.feedback;
        m.uniforms.uStarGain.value = this.starGain;
        m.uniforms.uStarLens.value = this.starLens;
        m.uniforms.uGlowK.value = this.glowK;
        m.uniforms.uHorizonAmp.value = this.horizonAmp;
        // 抖动只在有足够时间累积时才有意义（累积平均掉抖动=AA）；
        // 低累积下抖动没有历史可平均，纯剩噪声——关掉
        m.uniforms.uJitter.value = this.feedback > 0.3 ? 1 : 0;
        m.uniforms.iChannel0.value = this.rtA[this.flip].rt.texture;   // 上帧 Buffer A
        const dst = this.rtA[1 - this.flip];
        this.renderPass(m, dst);
        this.flip = 1 - this.flip;

        // ---- Blit：超采样盒式降采样 / 线性放大上屏 ----
        const mo = this.passes.blit;
        mo.uniforms.uTex.value = dst.rt.texture;
        const cw = this.renderer.domElement.width, ch = this.renderer.domElement.height;
        (mo.uniforms.uOutRes.value as THREE.Vector2).set(cw, ch);
        (mo.uniforms.uSrcRes.value as THREE.Vector2).set(w, h);
        mo.uniforms.uSS.value = this.scale > 1.05 ? this.scale : 0;
        this.renderPass(mo, null);
    }

    private renderPass(mat: THREE.RawShaderMaterial, target: RT | null) {
        // 复用同一个 mesh，按 pass 换材质（材质/程序只编译一次）
        const mesh = this.triScene.children[0] as THREE.Mesh | undefined;
        if (mesh) { mesh.material = mat; } else {
            const m = new THREE.Mesh(this.triangle, mat);
            m.frustumCulled = false;
            this.triScene.add(m);
        }
        this.renderer.setRenderTarget(target ? target.rt : null);
        this.renderer.clear();
        this.renderer.render(this.triScene, this.triCamera);
    }

    dispose() {
        this.disposed = true;
        this.stop();
        this.resizeObserver.disconnect();
        window.removeEventListener('pointermove', this.onPointerMove);
        document.documentElement.removeEventListener('pointerleave', this.onPointerLeave);
        for (const m of Object.values(this.passes)) m.dispose();
        this.triangle.dispose();
        for (const r of this.rtA) r.rt.dispose();
        this.renderer.dispose();
    }
}
