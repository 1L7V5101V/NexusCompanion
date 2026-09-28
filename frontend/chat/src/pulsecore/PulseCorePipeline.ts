// PulseCore 多 Pass 渲染管线：完整移植 Shadertoy fXV3Wm (Kerr-Newman 黑洞)
//
// Shadertoy 语义还原：
//   每帧按 A→B→C→D→Image 顺序渲染；
//   A 读 B 上帧(相机状态) + A 上帧(时间累积/TAA)
//   B 读 A 本帧(bloom 源) + B 上帧(相机状态持久化)
//   C 读 B 本帧, D 读 C 本帧, Image 读 A/D 本帧
//   A/B 各配一对 ping-pong 目标。
//
// 相机固定：iMouse 恒为未拖拽、无按键 → Buffer B 保持初始机位 (-2,-3.6,22)。
// 心跳：质量尺度（CONST_M）连续正弦振荡，黑洞+光子环+吸积盘整体平滑胀缩；
// 亮度同步微调。AI 状态只改振荡频率/幅度/亮度/闪烁。

import * as THREE from 'three';
import { SHIM_PREFIX, SHIM_MAIN, FULLSCREEN_VERT, OVERLAY_FRAG, envelopeOf, type WaveShape } from './shadertoy';
import bufferASrc from './shaders/bufferA.glsl?raw';
import bufferBSrc from './shaders/bufferB.glsl?raw';
import bufferCSrc from './shaders/bufferC.glsl?raw';
import bufferDSrc from './shaders/bufferD.glsl?raw';
import imageSrc from './shaders/image.glsl?raw';

export type PulseCoreState = 'idle' | 'thinking' | 'streaming' | 'error';

interface StateProfile {
    bpm: number;         // 心率（次/分）
    holeAmp: number;     // 盘内缘呼吸幅度（本体恒定语义；0.038 = 内缘峰值外扩 3.8%）
    gain: number;        // 基准曝光增益
    swirl: number;       // 盘演化时间速率倍率
    flicker: number;     // 闪烁强度 (error)
    wobble: number;      // 心律不齐强度（每拍随机化频率与幅度）
}

// 与 abstract 变体共用同一套用户定稿剖面（2026-09-28）
const STATE_PROFILES: Record<PulseCoreState, StateProfile> = {
    idle:      { bpm: 20,  holeAmp: 0.038, gain: 0.69, swirl: 0.90, flicker: 0.0,  wobble: 0 },
    thinking:  { bpm: 20,  holeAmp: 0.056, gain: 0.69, swirl: 2.0,  flicker: 0.0,  wobble: 0 },
    streaming: { bpm: 30,  holeAmp: 0.076, gain: 0.69, swirl: 0.90, flicker: 0.0,  wobble: 0 },
    error:     { bpm: 8,   holeAmp: 0.038, gain: 0.69, swirl: 0.2,  flicker: 0.2,  wobble: 0 },
};

interface RT {
    rt: THREE.WebGLRenderTarget;
    w: number;
    h: number;
}

export class PulseCorePipeline {
    private renderer: THREE.WebGLRenderer;
    private triScene = new THREE.Scene();
    private triCamera = new THREE.Camera();
    private triangle: THREE.BufferGeometry;
    private passes: Record<'A' | 'B' | 'C' | 'D' | 'image' | 'overlay', THREE.RawShaderMaterial>;
    private rtA: [RT, RT];   // ping-pong (TAA 历史)
    private rtB: [RT, RT];   // ping-pong (相机状态)
    private rtC!: RT;
    private rtD!: RT;
    private rtImage!: RT;
    private flip = 0;
    private keyboardTex: THREE.DataTexture;
    private keyboardData: Uint8Array;

    private frameNo = 0;
    private time = 0;
    private heartPhase = 0;      // 心跳相位（拍数，可非整数）
    private lastNow = 0;
    private raf = 0;
    private frameAcc = 0;        // 帧率上限累积器
    private disposed = false;

    // 状态机
    private state: PulseCoreState = 'idle';
    private cur: StateProfile = { ...STATE_PROFILES.idle };
    private profiles: Record<PulseCoreState, StateProfile>;  // 可调状态剖面（实例级）
    private audioEnergy = 0;
    private energySmooth = 0;

    // 心跳语义（与 abstract 对齐）：本体恒定（horizonAmp=0）+ 盘呼吸/辉光
    private waveShape: WaveShape = 'lubdub';
    private glowK = 8;            // 辉光脉动系数（盘亮度 flare）
    private starGain = 1;         // 背景/星空亮度（iBackgroundBrightmut）
    private horizonAmp = 0;       // 本体脉动：1=CONST_M 随心跳（旧行为），0=本体恒定
    private oscGainK = 0.2;       // 心跳→亮度
    private audioGainK = 0.15;    // 音频→亮度
    private audioScaleK = 0.012;  // 音频→盘脉动幅度
    private bhTime = 0;           // 盘演化时间（swirl 积分，替代 2·iTime）
    private beatIndex = 0;
    private rateJitter = 1;       // 心律不齐：本拍频率/幅度抖动
    private ampJitter = 1;

    private canvas: HTMLCanvasElement;
    private scale: number;
    private fpsCap: number;
    private baseMass = 0.5;      // 黑洞基础尺寸倍率（1.0 = 原作默认；整个引力系统等比缩放）
    private resizeObserver: ResizeObserver;

    constructor(canvas: HTMLCanvasElement, opts?: { scale?: number; fpsCap?: number; hideTopologyMap?: boolean }) {
        this.canvas = canvas;
        this.profiles = structuredClone(STATE_PROFILES);
        this.scale = opts?.scale ?? 0.4;
        this.fpsCap = opts?.fpsCap ?? 30;

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

        // 键盘纹理：恒零 = 无按键（相机固定；保留通道绑定满足原作采样）
        this.keyboardData = new Uint8Array(256 * 3 * 4);
        this.keyboardTex = new THREE.DataTexture(this.keyboardData, 256, 3, THREE.RGBAFormat, THREE.UnsignedByteType);
        this.keyboardTex.needsUpdate = true;

        // 全屏三角形（属性名必须是 position，three 用它算 drawRange）
        this.triangle = new THREE.BufferGeometry();
        this.triangle.setAttribute('position', new THREE.BufferAttribute(new Float32Array([-1, -1, 3, -1, -1, 3]), 2));

        const mkPass = (src: string) => new THREE.RawShaderMaterial({
            glslVersion: THREE.GLSL3,
            vertexShader: FULLSCREEN_VERT,
            fragmentShader: SHIM_PREFIX + src + SHIM_MAIN,
            depthTest: false,
            depthWrite: false,
                uniforms: {
                    iResolution: { value: new THREE.Vector3(1, 1, 1) },
                    iTime: { value: 0 },
                    iTimeDelta: { value: 0 },
                    iFrame: { value: 0 },
                    iMouse: { value: new THREE.Vector4(0, 0, -1, -1) },   // 恒未拖拽 → 相机固定
                    iChannelResolution: { value: [new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3(), new THREE.Vector3()] },
                    iChannel0: { value: null },
                    iChannel1: { value: null },
                    iChannel2: { value: null },
                    iChannel3: { value: null },
                    uCONST_M: { value: 0.5 },   // 仅 Pass A 使用：几何质量（本体，默认恒定）
                    iInterRadiusRs: { value: 2.0 },  // 盘内半径 (Rs)——JS 注入（盘呼吸）
                    iBrightmut: { value: 1.0 },      // 盘亮度乘数——JS 注入（辉光脉动）
                    iBackgroundBrightmut: { value: 1.0 }, // 背景亮度乘数——JS 注入（星空亮度）
                    uBHTime: { value: 0 },           // 盘演化时间——JS 按 swirl 积分
                },
        });

        // 拓扑图 UI 是原作调试面板（右上角蓝框），网站背景用不到。
        // 加载层精确替换调用行，磁盘上的原作 glsl 文件保持零修改；
        // 若上游版本改了这行导致 no-op，拓扑图会原样出现（可发现、可再适配）。
        const aSrc0 = opts?.hideTopologyMap === false
            ? bufferASrc
            : bufferASrc.replace(/vec4 mapCol = RenderTopologyMap\([^;]+;/, 'vec4 mapCol = vec4(0.0);');
        // 黑洞本体心跳：把几何质量 CONST_M（度规/视界/盘半径 Rs 的共同尺度根）
        // 换成 uniform，由 JS 每帧按心跳包络设值——洞、光子环、吸积盘整体缩放
        const aSrc = aSrc0
            .replace(/const float CONST_M\s*=\s*0\.5;/, 'uniform float uCONST_M;')
            .replace(/\bCONST_M\b/g, 'uCONST_M')
            // 盘呼吸/辉光/星空/演化时间：原 #define 常量 → uniform，JS 每帧注入
            .replace(/#define iInterRadiusRs.*$/m, 'uniform float iInterRadiusRs; // 吸积盘内半径(Rs)——JS 注入（盘呼吸）')
            .replace(/#define iBrightmut.*$/m, 'uniform float iBrightmut; // 吸积盘亮度乘数——JS 注入（辉光脉动）')
            .replace(/#define iBackgroundBrightmut.*$/m, 'uniform float iBackgroundBrightmut; // 背景亮度乘数——JS 注入（星空亮度）')
            .replace(/#define iBlackHoleTime\s*\(2\.0\*iTime\)/, 'uniform float uBHTime; // 盘演化时间——JS 按 swirl 积分')
            .replace(/\biBlackHoleTime\b/g, 'uBHTime');

        this.passes = {
            A: mkPass(aSrc),
            B: mkPass(bufferBSrc),
            C: mkPass(bufferCSrc),
            D: mkPass(bufferDSrc),
            image: mkPass(imageSrc),
            overlay: new THREE.RawShaderMaterial({
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

        // 渲染目标
        const w = Math.max(2, Math.floor((canvas.clientWidth || 800) * dpr * this.scale));
        const h = Math.max(2, Math.floor((canvas.clientHeight || 600) * dpr * this.scale));
        this.rtA = [this.mkRT(w, h), this.mkRT(w, h)];
        this.rtB = [this.mkRT(w, h), this.mkRT(w, h)];
        this.rtC = this.mkRT(w, h);
        this.rtD = this.mkRT(w, h);
        this.rtImage = this.mkRT(w, h);

        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(canvas);
    }

    private mkRT(w: number, h: number): RT {
        const rt = new THREE.WebGLRenderTarget(w, h, {
            // 优先 32F（相机状态精度），线性过滤不可用则退 16F（WebGL2 核心支持线性）
            type: this.floatLinear ? THREE.FloatType : THREE.HalfFloatType,
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

    private get floatLinear(): boolean {
        const gl = this.renderer.getContext();
        return this.renderer.capabilities.isWebGL2 &&
            !!gl.getExtension('OES_texture_float_linear') &&
            !!gl.getExtension('EXT_color_buffer_float');
    }

    private resize() {
        const dpr = Math.min(window.devicePixelRatio || 1, 1.5);
        const cw = this.canvas.clientWidth || 800;
        const ch = this.canvas.clientHeight || 600;
        this.renderer.setSize(Math.floor(cw * dpr), Math.floor(ch * dpr), false);
        const w = Math.max(2, Math.floor(cw * dpr * this.scale));
        const h = Math.max(2, Math.floor(ch * dpr * this.scale));
        const rebuild = (old: RT): RT => { old.rt.dispose(); return this.mkRT(w, h); };
        this.rtA = [rebuild(this.rtA[0]), rebuild(this.rtA[1])];
        this.rtB = [rebuild(this.rtB[0]), rebuild(this.rtB[1])];
        this.rtC = rebuild(this.rtC);
        this.rtD = rebuild(this.rtD);
        this.rtImage = rebuild(this.rtImage);
        this.flip = 0;
    }

    setState(s: PulseCoreState) { this.state = s; }
    setAudioEnergy(e: number) { this.audioEnergy = Math.max(0, Math.min(1, e)); }
    /** 黑洞基础尺寸倍率：1.0=原作默认。>1.3 时相机(距离~22)会进入吸积盘外缘，不建议 */
    setHoleSize(v: number) { this.baseMass = 0.5 * Math.max(0.2, Math.min(1.3, v)); }

    // ---- 调参 API（与 abstract 对齐；kerr 不支持的项为 no-op）----

    private static PROFILE_CLAMP: Record<keyof StateProfile, [number, number]> = {
        bpm: [0, 200], holeAmp: [0, 0.15], gain: [0.2, 2], swirl: [0, 4], flicker: [0, 1], wobble: [0, 1],
    };

    setProfile(s: PulseCoreState, patch: Partial<StateProfile>) {
        for (const [k, v] of Object.entries(patch)) {
            if (v === undefined) continue;
            const key = k as keyof StateProfile;
            const [lo, hi] = PulseCorePipeline.PROFILE_CLAMP[key];
            this.profiles[s][key] = Math.max(lo, Math.min(hi, v));
        }
    }
    getProfile(s: PulseCoreState): StateProfile { return { ...this.profiles[s] }; }
    setWaveShape(w: WaveShape) { this.waveShape = w; }
    setGlowK(v: number) { this.glowK = Math.max(0, Math.min(20, v)); }
    setStarGain(v: number) { this.starGain = Math.max(0, Math.min(3, v)); }
    /** 本体脉动：1 = CONST_M 随心跳（旧行为，星空随之动），0 = 本体恒定（默认） */
    setHorizonAmp(v: number) { this.horizonAmp = Math.max(0, Math.min(1, v)); }
    setDynamics(p: { oscGainK?: number; audioGainK?: number; audioScaleK?: number }) {
        if (p.oscGainK !== undefined) this.oscGainK = Math.max(0, Math.min(0.3, p.oscGainK));
        if (p.audioGainK !== undefined) this.audioGainK = Math.max(0, Math.min(0.8, p.audioGainK));
        if (p.audioScaleK !== undefined) this.audioScaleK = Math.max(0, Math.min(0.05, p.audioScaleK));
    }
    /** kerr 的盘为 GR 体渲染，abstract 式波纹参数不适用——no-op */
    setPert(_p: { lam?: number; lag?: number }) { /* no-op */ }
    /** kerr 星空的透镜由 GR 光线追踪内生——no-op（恒定开启） */
    setStarLens(_v: number) { /* no-op */ }
    setMouseParams(_p: { offsetK?: number; boost?: number; near?: number; far?: number }) { /* no-op：相机由 iMouse 驱动 */ }
    /** kerr 无独立时间累积开关（TAA 内建于 Buffer A）——no-op */
    setFeedback(_v: number) { /* no-op */ }
    setScale(v: number) { this.scale = Math.max(0.25, Math.min(0.85, v)); this.resize(); }
    setFpsCap(v: number) { this.fpsCap = Math.max(10, Math.min(120, v)); }

    start() {
        this.lastNow = performance.now();
        const loop = (now: number) => {
            if (this.disposed) return;
            this.raf = requestAnimationFrame(loop);
            if (document.hidden) { this.lastNow = now; return; }
            // 帧率上限：GR 体渲染开销大，30fps 足够氛围动画
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

        // 心跳机械（与 abstract 一致）：连续振荡 + error 每拍随机化
        const beat = Math.floor(this.heartPhase);
        if (beat !== this.beatIndex) {
            this.beatIndex = beat;
            this.rateJitter = 1 + (Math.random() - 0.5) * 0.9 * this.cur.wobble;
            this.ampJitter = 1 + (Math.random() - 0.5) * 0.6 * this.cur.wobble;
        }
        this.heartPhase += dt * this.cur.bpm * this.rateJitter / 60;
        const osc = envelopeOf(this.heartPhase, this.waveShape);
        const flick = 1 - this.cur.flicker * Math.max(0, Math.sin(this.time * 23.0) * Math.sin(this.time * 7.3));
        const gain = this.cur.gain * (1 + this.oscGainK * osc + this.energySmooth * this.audioGainK) * flick;

        // 心跳语义（与 abstract 对齐）：pulseFrac = 盘呼吸/辉光的公共分数
        const pulseFrac = this.cur.holeAmp * this.ampJitter * osc + this.energySmooth * this.audioScaleK;
        // 本体脉动（horizonAmp>0 = 旧行为）：CONST_M 随心跳——影子/透镜/星空随之动
        const holePulse = this.horizonAmp * pulseFrac;
        // 盘呼吸（horizonAmp=0 时的全部脉动）：盘内缘随心跳外扩/收缩，外缘固定
        const diskPulse = (1 - this.horizonAmp) * pulseFrac;
        // 辉光脉动：盘亮度随心跳 flare
        const glow = this.glowK * pulseFrac;
        this.bhTime += dt * 2 * this.cur.swirl;
        this.bhTime -= Math.floor(this.bhTime / 1000) * 1000;   // 防大数精度（原 hazeTime mod 1000 同款）

        const w = this.rtImage.w, h = this.rtImage.h;
        const aspect = w / h;
        const setRes = (m: THREE.RawShaderMaterial) => {
            const u = m.uniforms;
            u.iResolution.value.set(w, h, aspect);
            u.iTime.value = this.time;
            u.iTimeDelta.value = dt;
            u.iFrame.value = this.frameNo;
            u.iChannelResolution.value[0].set(256, 3, 1);          // keyboard
            u.iChannelResolution.value[1].set(w, h, aspect);
            u.iChannelResolution.value[2].set(w, h, aspect);
            u.iChannelResolution.value[3].set(w, h, aspect);
        };

        // ---- Pass A: GR 渲染（读 B 上帧 + A 上帧历史 + 键盘） ----
        const m = this.passes.A;
        setRes(m);
        m.uniforms.uCONST_M.value = this.baseMass * (1 + holePulse);
        m.uniforms.iInterRadiusRs.value = 2.0 * (1 + diskPulse);
        m.uniforms.iBrightmut.value = 1 + glow;
        m.uniforms.iBackgroundBrightmut.value = this.starGain;
        m.uniforms.uBHTime.value = this.bhTime;
        m.uniforms.iChannel0.value = this.keyboardTex;
        m.uniforms.iChannel1.value = null;
        m.uniforms.iChannel2.value = this.rtB[this.flip].rt.texture;         // 上帧 B
        m.uniforms.iChannel3.value = this.rtA[this.flip].rt.texture;         // 上帧 A (TAA)
        const dstA = this.rtA[1 - this.flip];
        this.renderPass(m, dstA);

        // ---- Pass B: 相机更新 + bloom 明亮层（读 A 本帧 + B 上帧） ----
        const mb = this.passes.B;
        setRes(mb);
        mb.uniforms.iChannel0.value = dstA.rt.texture;                       // 本帧 A
        mb.uniforms.iChannel1.value = this.rtB[this.flip].rt.texture;        // 上帧 B
        mb.uniforms.iChannel2.value = null;
        mb.uniforms.iChannel3.value = this.keyboardTex;
        const dstB = this.rtB[1 - this.flip];
        this.renderPass(mb, dstB);

        // ---- Pass C/D: 横竖高斯模糊 ----
        const mc = this.passes.C;
        setRes(mc);
        mc.uniforms.iChannel0.value = dstB.rt.texture;
        this.renderPass(mc, this.rtC);

        const md = this.passes.D;
        setRes(md);
        md.uniforms.iChannel0.value = this.rtC.rt.texture;
        this.renderPass(md, this.rtD);

        // ---- Image: 合成 + tonemap ----
        const mi = this.passes.image;
        setRes(mi);
        mi.uniforms.iChannel0.value = dstA.rt.texture;
        mi.uniforms.iChannel3.value = this.rtD.rt.texture;
        this.renderPass(mi, this.rtImage);

        // ---- Overlay: 增益调制，上屏 ----
        const mo = this.passes.overlay;
        mo.uniforms.uTex.value = this.rtImage.rt.texture;
        const cw = this.renderer.domElement.width, chh = this.renderer.domElement.height;
        (mo.uniforms.uOutRes.value as THREE.Vector2).set(cw, chh);
        mo.uniforms.uGain.value = gain;
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
        for (const m of Object.values(this.passes)) m.dispose();
        this.triangle.dispose();
        this.keyboardTex.dispose();
        for (const r of [this.rtA[0], this.rtA[1], this.rtB[0], this.rtB[1], this.rtC, this.rtD, this.rtImage]) r.rt.dispose();
        this.renderer.dispose();
    }
}
