// Abstract 黑洞渲染管线：完整移植 Shadertoy fXVGDm（mrange "Abstract black hole"，CC0）
//
// Shadertoy 语义还原：
//   Buffer A 每帧光线步进，末尾与自身上帧输出做 0.9 混合（子像素抖动时间累积/TAA），
//   iChannel0 = Buffer A 自己 → 一对 ping-pong 目标；
//   原作 Image pass 是 1:1 直通（texelFetch iChannel0）——管线按 scale 缩小渲染，
//   这里换成 OVERLAY 线性放大 blit 上屏（uGain=1，语义等价）。
//
// 相对原作的适配（原作 glsl 文件零修改，全部在加载层做字符串替换）：
//   p=(z*crd+ro)/uScale + z+=s*uScale   心跳呼吸：相机与引力系统同一把尺子，
//                                       洞+光晕+吸积盘连贯泵动（非 2D 整画面缩放）
//   P=.../R.y+uMouseOff                 鼠标视差：靠近核心时初始光线轻微偏移
//   iTime/ → uSwirlTime/                吸积盘旋转时间由 JS 按状态积分（换挡无相位跳变）
//   o/=5e3; 前插 o*=uGain               曝光增益（tonemap 前）：状态/音频/鼠标能量/闪烁
//   vec4 O; 后补 o=vec3(0)              原作依赖未初始化局部量归零，显式化防驱动差异
//
// AI 状态只调制节奏/亮度/旋转/闪烁，不改原作配色（沿用 Kerr-Newman 版的定案）。

import * as THREE from 'three';
import { SHIM_PREFIX, SHIM_MAIN, FULLSCREEN_VERT, OVERLAY_FRAG, massOscillation } from '../shadertoy';
import type { PulseCoreState } from '../PulseCorePipeline';
import bufferASrc from './shaders/bufferA.glsl?raw';

interface StateProfile {
    bpm: number;         // 脉搏（次/分）
    holeAmp: number;     // 整体呼吸缩放幅度（0.03 = 峰值放大 3%）
    gain: number;        // 基准曝光增益
    swirl: number;       // 吸积盘旋转速度倍率
    flicker: number;     // 闪烁强度（error）
    wobble: number;      // 心律不齐强度（error：每拍随机化频率与幅度）
}

const STATE_PROFILES: Record<PulseCoreState, StateProfile> = {
    idle:      { bpm: 36,  holeAmp: 0.016, gain: 0.92, swirl: 0.55, flicker: 0.0,  wobble: 0 },
    thinking:  { bpm: 72,  holeAmp: 0.030, gain: 1.00, swirl: 1.05, flicker: 0.0,  wobble: 0 },
    streaming: { bpm: 96,  holeAmp: 0.044, gain: 1.12, swirl: 1.60, flicker: 0.0,  wobble: 0 },
    error:     { bpm: 104, holeAmp: 0.050, gain: 0.88, swirl: 0.75, flicker: 0.28, wobble: 1 },
};

// 注入的 uniform 声明（shim 之后、原作代码之前）
const UNIFORM_DECLS = /* glsl */ `
uniform float uScale;       // 心跳整体缩放（1 = 原作默认构图）
uniform float uGain;        // 曝光增益（tonemap 前）
uniform vec2  uMouseOff;    // 鼠标视差偏移（P 空间）
uniform float uSwirlTime;   // 吸积盘旋转时间（JS 积分）
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
    private audioEnergy = 0;
    private energySmooth = 0;

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
        this.scale = opts?.scale ?? 0.5;
        this.fpsCap = opts?.fpsCap ?? 60;

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
            .replace('p=z*crd+ro;', 'p=(z*crd+ro)/uScale;')
            .replace('z+=s;', 'z+=s*uScale;')
            .replace('P=(C+C-R)/R.y', 'P=(C+C-R)/R.y+uMouseOff')
            .replace('iTime/', 'uSwirlTime/')
            .replace('o/=5e3;', 'o*=uGain;\n  o/=5e3;')
            .replace('vec4 O;', 'vec4 O;\n  o=vec3(0);');
        for (const marker of ['uScale;', 'uMouseOff', 'uSwirlTime/', 'uGain;\n']) {
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
            uScale: { value: 1 },
            uGain: { value: 1 },
            uMouseOff: { value: new THREE.Vector2(0, 0) },
            uSwirlTime: { value: 0 },
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
    /** 基础尺寸倍率：1.0 = 原作默认构图。相机距 8、盘外缘 6×size，>1.5 会贴到盘外缘 */
    setHoleSize(v: number) { this.holeSize = Math.max(0.3, Math.min(1.5, v)); }

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
        const p = STATE_PROFILES[this.state];

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
        const osc = massOscillation(this.heartPhase);

        // 鼠标：靠近核心程度 → 视差偏移 + 能量增强
        const proxT = this.hasPointer ? 1 - smoothStep(0.12, 0.7, Math.hypot(this.pointerP.x, this.pointerP.y)) : 0;
        this.proxSmooth += (proxT - this.proxSmooth) * (1 - Math.exp(-dt * (proxT > this.proxSmooth ? 10 : 3)));
        this.mouseOffTarget.set(this.pointerP.x * 0.12 * this.proxSmooth, this.pointerP.y * 0.12 * this.proxSmooth);
        this.mouseOff.lerp(this.mouseOffTarget, 1 - Math.exp(-dt * 6));

        const flick = 1 - this.cur.flicker * Math.max(0, Math.sin(this.time * 23.0) * Math.sin(this.time * 7.3));
        const gain = this.cur.gain * (1 + 0.06 * osc + this.energySmooth * 0.15) * (1 + this.proxSmooth * 0.35) * flick;
        this.swirlTime += dt * this.cur.swirl;

        const w = this.rtA[0].w, h = this.rtA[0].h;
        // ---- Buffer A：光线步进 + 上帧自反馈混合 ----
        const m = this.passes.bufA;
        m.uniforms.iResolution.value.set(w, h, w / h);
        m.uniforms.iTime.value = this.time;
        m.uniforms.iTimeDelta.value = dt;
        m.uniforms.iFrame.value = this.frameNo;
        m.uniforms.uScale.value = this.holeSize * (1 + this.cur.holeAmp * this.ampJitter * osc + this.energySmooth * 0.012);
        m.uniforms.uGain.value = gain;
        m.uniforms.uMouseOff.value.copy(this.mouseOff);
        m.uniforms.uSwirlTime.value = this.swirlTime;
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
