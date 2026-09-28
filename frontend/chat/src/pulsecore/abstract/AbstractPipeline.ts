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
//                                       心跳脉动：只有洞本体（视界/光子环）随包络胀缩，
//                                       相机真正固定——不是整体缩放；引力弯折随瞬时
//                                       质量 uH 同比缩放（光子球 ∝ 视界 ∝ M，GR 关系），
//                                       弧始终贴着洞缘——只放大视界不放大会把光子球
//                                       吞进视界，透镜弧消失
//   N=floor(diskInv(Z)/STEP) + rw*diskF(rw)
//                                       盘响应：径向翘曲场 diskF——内缘严格跟随洞
//                                       （ISCO 随质量），扰动向外按 exp(-x/λ) 衰减、
//                                       按 uPertLag（拍/单位半径）相位延迟传播，
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
import { SHIM_PREFIX, SHIM_MAIN, FULLSCREEN_VERT, OVERLAY_FRAG, massOscillation } from '../shadertoy';
import type { PulseCoreState } from '../PulseCorePipeline';
import bufferASrc from './shaders/bufferA.glsl?raw';

export interface StateProfile {
    bpm: number;         // 脉搏（次/分）
    holeAmp: number;     // 整体呼吸缩放幅度（0.03 = 峰值放大 3%）
    gain: number;        // 基准曝光增益
    swirl: number;       // 吸积盘旋转速度倍率
    flicker: number;     // 闪烁强度（error）
    wobble: number;      // 心律不齐强度（error：每拍随机化频率与幅度）
}

export type WaveShape = 'continuous' | 'lubdub' | 'pulse';

const STATE_PROFILES: Record<PulseCoreState, StateProfile> = {
    idle:      { bpm: 36,  holeAmp: 0.016, gain: 0.92, swirl: 0.55, flicker: 0.0,  wobble: 0 },
    thinking:  { bpm: 72,  holeAmp: 0.030, gain: 1.00, swirl: 1.05, flicker: 0.0,  wobble: 0 },
    streaming: { bpm: 96,  holeAmp: 0.044, gain: 1.12, swirl: 1.60, flicker: 0.0,  wobble: 0 },
    error:     { bpm: 104, holeAmp: 0.050, gain: 0.88, swirl: 0.75, flicker: 0.28, wobble: 1 },
};

// 注入的 uniform 声明与脉动/盘响应函数（shim 之后、原作代码之前）。
// envWave 必须与 TS 侧 oscOf() 公式一致（波形三态），两边同步改。
const UNIFORM_DECLS = /* glsl */ `
uniform float uHoleBase;    // 洞基础尺寸（holeSize，1 = 原作默认构图）
uniform float uPulseAmp;    // 洞脉动幅度（分数，含每拍抖动 + 音频耦合）
uniform float uPhase;       // 心跳相位（拍，已 mod 1）
uniform float uPertLam;     // 盘扰动衰减长度（世界单位）
uniform float uPertLag;     // 盘扰动传播延迟（拍/单位半径）
uniform int   uWave;        // 波形 0 连续正弦 / 1 双峰 lub-dub / 2 快脉冲
uniform float uGain;        // 曝光增益（tonemap 前）
uniform vec2  uMouseOff;    // 鼠标视差偏移（P 空间）
uniform float uSwirlTime;   // 吸积盘旋转时间（JS 积分）
uniform float uFeedback;    // 时间累积混合（0=无残影，0.9=原作 TAA）
uniform float uJitter;      // 亚像素抖动开关（累积关掉时一并关，防边缘爬行）

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
// 盘径向翘曲场：r 为 u 空间（等距环）半径，返回物理半径倍率。
// x=0（内缘 u=1.0）处严格等于洞因子（内缘拴在 ISCO 上），向外衰减到 1（外盘不动）。
float diskF(float r) {
  float x=max(0., r-1.);
  return 1.+((uHoleBase-1.)+uHoleBase*uPulseAmp*envWave(uPhase-uPertLag*x))*exp(-x/uPertLam);
}
// Z→u 的数值逆（解 u·F(u)=Z）：一阶近似起步 + 两步牛顿。
// 导数近似 F'≈-(F-1)/λ（把随 u 缓变的脉动项视为局部常数）。
// 大 holeSize 时 F 在内缘变化剧烈，一步近似会错好几个环带——最内侧的厚云环
// 会被推错位、配错厚度（云消失）。牛顿收敛后按环带精确归位。
float diskInv(float Z) {
  float u=Z/diskF(Z);
  for(int i=0;i<2;++i){
    float Fu=diskF(u);
    u-=(u*Fu-Z)/(Fu-u*(Fu-1.)/uPertLam);
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
    private waveShape: WaveShape = 'continuous';
    private audioEnergy = 0;
    private energySmooth = 0;

    // 耦合与交互参数（demo 调参面板实时改）
    private oscGainK = 0.06;      // 心跳→亮度
    private audioGainK = 0.15;    // 音频→亮度
    private audioScaleK = 0.012;  // 音频→脉动幅度
    private pertLam = 1.2;        // 盘扰动衰减长度（世界单位）：越大盘跟随越多
    private pertLag = 0.12;       // 盘扰动传播延迟（拍/单位半径）：涟漪外传速度
    private mouseOffsetK = 0.12;  // 鼠标视差幅度
    private mouseBoost = 0.35;    // 鼠标→能量增强
    private proxNear = 0.12;      // 接近半径内缘（P 空间，半高=1）
    private proxFar = 0.7;        // 接近半径外缘
    private feedback = 0.55;      // 时间累积混合：0=无残影，0.9=原作 TAA 手感

    // 鼠标
    private hasPointer = false;
    private pointerP = new THREE.Vector2();      // P 空间坐标（半高=1）
    private proxSmooth = 0;                      // 靠近核心程度 0..1
    private mouseOff = new THREE.Vector2();
    private mouseOffTarget = new THREE.Vector2();

    private canvas: HTMLCanvasElement;
    private scale: number;
    private fpsCap: number;
    private holeSize = 1;        // 基础尺寸倍率（1.0 = 原作默认构图）
    private resizeObserver: ResizeObserver;

    constructor(canvas: HTMLCanvasElement, opts?: { scale?: number; fpsCap?: number }) {
        this.canvas = canvas;
        this.profiles = structuredClone(STATE_PROFILES);
        this.scale = opts?.scale ?? 0.5;
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
            // 洞本体脉动：只有视界随包络胀缩，相机固定。
            // uH = 瞬时质量因子（基础尺寸 × 脉动包络），引力弯折必须同比例缩放——
            // 否则光子球不随视界走，大洞会把绕行发光区整个吞进视界（上缘白弧消失）
            .replace('d=l-.7;', 'float uH=uHoleBase*(1.+uPulseAmp*envWave(uPhase));d=l-.7*uH;')
            // 引力随质量缩放：光子球 ∝ 视界 ∝ M（GR 关系），弧始终贴着洞缘
            .replace('g=-p/(l*L);', 'g=-p*uH/(l*L);')
            // 盘响应：径向翘曲——内环随洞进出，扰动向外衰减+延迟传播
            .replace('N=clamp(floor(length(p.xz)/STEP+.5),1./STEP,6./STEP);',
                'N=clamp(floor(diskInv(Z)/STEP+.5),1./STEP,6./STEP);')
            .replace('w=vec2(abs(Z-STEP*(N+j/(2.*REPS+1.))), ',
                'rw=STEP*(N+j/(2.*REPS+1.));w=vec2(abs(Z-rw*diskF(rw)), ')
            // 鼠标视差：P 空间偏移初始光线
            .replace('P=(C+C-R)/R.y', 'P=(C+C-R)/R.y+uMouseOff')
            // 状态化旋转：吸积盘角速度由 JS 积分（状态切换无相位跳变）
            .replace('iTime/', 'uSwirlTime/')
            // 曝光增益：tonemap 前乘（亮度呼吸/音频/鼠标能量/闪烁都在 uGain 里）
            .replace('o/=5e3;', 'o*=uGain;\n  o/=5e3;')
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
        for (const marker of ['uHoleBase', 'uPulseAmp', 'envWave(', 'diskF(', 'diskInv(', 'uSwirlTime/', 'uGain;\n', 'g=-p*uH', 'uFeedback', 'jitter(iFrame)*']) {
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
            uPhase: { value: 0 },
            uPertLam: { value: 1.2 },
            uPertLag: { value: 0.12 },
            uWave: { value: 0 },
            uGain: { value: 1 },
            uMouseOff: { value: new THREE.Vector2(0, 0) },
            uSwirlTime: { value: 0 },
            uFeedback: { value: 0.55 },
            uJitter: { value: 1 },
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
                fragmentShader: OVERLAY_FRAG,
                depthTest: false,
                depthWrite: false,
                uniforms: {
                    uTex: { value: null },
                    uOutRes: { value: new THREE.Vector2(1, 1) },
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

    /** 鼠标交互：视差幅度 / 能量增强 / 接近内缘 / 接近外缘（P 空间，半高=1） */
    setMouseParams(patch: { offsetK?: number; boost?: number; near?: number; far?: number }) {
        if (patch.offsetK !== undefined) this.mouseOffsetK = Math.max(0, Math.min(0.5, patch.offsetK));
        if (patch.boost !== undefined) this.mouseBoost = Math.max(0, Math.min(1.5, patch.boost));
        if (patch.near !== undefined) this.proxNear = Math.max(0, Math.min(this.proxFar, patch.near));
        if (patch.far !== undefined) this.proxFar = Math.max(this.proxNear + 0.05, Math.min(2, patch.far));
    }

    /** 内部渲染分辨率系数（重建渲染目标，反馈历史清零会闪一帧重新收敛） */
    setScale(v: number) {
        this.scale = Math.max(0.25, Math.min(0.85, v));
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
        m.uniforms.uJitter.value = this.feedback > 0.03 ? 1 : 0;
        m.uniforms.iChannel0.value = this.rtA[this.flip].rt.texture;   // 上帧 Buffer A
        const dst = this.rtA[1 - this.flip];
        this.renderPass(m, dst);
        this.flip = 1 - this.flip;

        // ---- Blit：线性放大上屏（原作 Image 直通的等价物）----
        const mo = this.passes.blit;
        mo.uniforms.uTex.value = dst.rt.texture;
        const cw = this.renderer.domElement.width, ch = this.renderer.domElement.height;
        (mo.uniforms.uOutRes.value as THREE.Vector2).set(cw, ch);
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
