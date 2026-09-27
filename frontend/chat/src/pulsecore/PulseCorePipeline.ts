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
// 心跳：双峰(lub-dub)包络同时驱动 ①整体亮度 ②围绕洞心的径向扩张收缩
// （被引力透镜扭曲的线条随画面一起泵动，即用户要的"星云心跳"）。

import * as THREE from 'three';
import { SHIM_PREFIX, SHIM_MAIN, FULLSCREEN_VERT, OVERLAY_FRAG, heartbeatPulse } from './shadertoy';
import bufferASrc from './shaders/bufferA.glsl?raw';
import bufferBSrc from './shaders/bufferB.glsl?raw';
import bufferCSrc from './shaders/bufferC.glsl?raw';
import bufferDSrc from './shaders/bufferD.glsl?raw';
import imageSrc from './shaders/image.glsl?raw';

export type PulseCoreState = 'idle' | 'thinking' | 'streaming' | 'error';

interface StateProfile {
    bpm: number;         // 心率（次/分）
    zoomAmp: number;     // 心跳径向扩张幅度（0.05 = 峰值放大 5%）
    gain: number;        // 基准亮度增益
    flicker: number;     // 闪烁强度 (error)
}

// 色彩一律不动（用户要求保留原作配色），状态只调制亮度/心跳节奏
const STATE_PROFILES: Record<PulseCoreState, StateProfile> = {
    idle:      { bpm: 42,  zoomAmp: 0.022, gain: 1.00, flicker: 0.0 },
    thinking:  { bpm: 84,  zoomAmp: 0.048, gain: 1.06, flicker: 0.0 },
    streaming: { bpm: 96,  zoomAmp: 0.060, gain: 1.10, flicker: 0.0 },
    error:     { bpm: 110, zoomAmp: 0.075, gain: 0.90, flicker: 0.20 },
};

/** 黑洞（透镜影）在画面中的归一化位置：按默认机位实际渲染结果标定 */
const HOLE_CENTER = new THREE.Vector2(0.6, 0.32);

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
    private audioEnergy = 0;
    private energySmooth = 0;

    private canvas: HTMLCanvasElement;
    private scale: number;
    private fpsCap: number;
    private resizeObserver: ResizeObserver;

    constructor(canvas: HTMLCanvasElement, opts?: { scale?: number; fpsCap?: number; hideTopologyMap?: boolean }) {
        this.canvas = canvas;
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
            },
        });

        // 拓扑图 UI 是原作调试面板（右上角蓝框），网站背景用不到。
        // 加载层精确替换调用行，磁盘上的原作 glsl 文件保持零修改；
        // 若上游版本改了这行导致 no-op，拓扑图会原样出现（可发现、可再适配）。
        const aSrc = opts?.hideTopologyMap === false
            ? bufferASrc
            : bufferASrc.replace(/vec4 mapCol = RenderTopologyMap\([^;]+;/, 'vec4 mapCol = vec4(0.0);');

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
                    uCenter: { value: HOLE_CENTER.clone() },
                    uGain: { value: 1 },
                    uZoom: { value: 0 },
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
        const p = STATE_PROFILES[this.state];

        // 状态参数平滑过渡
        const k = 1 - Math.exp(-dt * 3);
        this.cur.bpm += (p.bpm - this.cur.bpm) * k;
        this.cur.zoomAmp += (p.zoomAmp - this.cur.zoomAmp) * k;
        this.cur.gain += (p.gain - this.cur.gain) * k;
        this.cur.flicker += (p.flicker - this.cur.flicker) * k;

        // 音频能量包络（attack 快 / release 慢）
        this.energySmooth += (this.audioEnergy - this.energySmooth) * (1 - Math.exp(-dt * (this.audioEnergy > this.energySmooth ? 12 : 3)));

        // lub-dub 双峰心跳
        this.heartPhase += dt * this.cur.bpm / 60;
        const beat = heartbeatPulse(this.heartPhase);
        const flick = 1 - this.cur.flicker * Math.max(0, Math.sin(this.time * 23.0) * Math.sin(this.time * 7.3));
        const gain = this.cur.gain * (1 + 0.10 * beat + this.energySmooth * 0.12) * flick;
        const zoom = this.cur.zoomAmp * beat + this.energySmooth * 0.012;

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

        // ---- Overlay: 心跳径向扩张收缩 + 增益，上屏 ----
        const mo = this.passes.overlay;
        mo.uniforms.uTex.value = this.rtImage.rt.texture;
        const cw = this.renderer.domElement.width, chh = this.renderer.domElement.height;
        (mo.uniforms.uOutRes.value as THREE.Vector2).set(cw, chh);
        (mo.uniforms.uCenter.value as THREE.Vector2).copy(HOLE_CENTER);
        mo.uniforms.uGain.value = gain;
        mo.uniforms.uZoom.value = zoom;
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
