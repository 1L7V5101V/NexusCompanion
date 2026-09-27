// PulseCore 多 Pass 渲染管线：完整移植 Shadertoy fXV3Wm (Kerr-Newman 黑洞)
//
// Shadertoy 语义还原：
//   每帧按 A→B→C→D→Image 顺序渲染；
//   pass 读取其它 buffer 时拿到的是"最近完成的输出"：
//     A 读 B 上帧(相机状态) + A 上帧(时间累积/TAA)
//     B 读 A 本帧(bloom 源) + B 上帧(相机状态持久化)
//     C 读 B 本帧, D 读 C 本帧, Image 读 A/D 本帧
//   所以 A/B 各配一对 ping-pong 目标，C/D/Image 每帧重写、只需单目标
//   (但 Image 输出给 overlay 采样，也做双缓冲以防读写下冲突——单目标即可，
//    overlay 在 Image 之后的同一帧读取，three 的 RT 在解绑后可安全采样)。

import * as THREE from 'three';
import { SHIM_PREFIX, SHIM_MAIN, FULLSCREEN_VERT, OVERLAY_FRAG, PARTICLE_VERT, PARTICLE_FRAG } from './shadertoy';
import bufferASrc from './shaders/bufferA.glsl?raw';
import bufferBSrc from './shaders/bufferB.glsl?raw';
import bufferCSrc from './shaders/bufferC.glsl?raw';
import bufferDSrc from './shaders/bufferD.glsl?raw';
import imageSrc from './shaders/image.glsl?raw';

export type PulseCoreState = 'idle' | 'thinking' | 'streaming' | 'error';

interface StateProfile {
    orbitSpeed: number;        // 自动环绕角速度 rad/s
    gain: number;              // 整体亮度增益
    breathAmp: number;         // 呼吸幅度
    breathFreq: number;        // 呼吸频率 Hz
    flicker: number;           // 闪烁强度 (error)
    particleSpeed: number;     // 星尘速度
    particleBrightness: number;
}

// 色彩一律不动（用户要求保留原作配色），状态只调制亮度/节奏
const STATE_PROFILES: Record<PulseCoreState, StateProfile> = {
    idle:      { orbitSpeed: 0.020, gain: 1.00, breathAmp: 0.04, breathFreq: 0.22, flicker: 0.0, particleSpeed: 1.0, particleBrightness: 0.5 },
    thinking:  { orbitSpeed: 0.055, gain: 1.06, breathAmp: 0.10, breathFreq: 0.60, flicker: 0.0, particleSpeed: 2.2, particleBrightness: 0.8 },
    streaming: { orbitSpeed: 0.035, gain: 1.12, breathAmp: 0.14, breathFreq: 0.45, flicker: 0.0, particleSpeed: 3.2, particleBrightness: 1.1 },
    error:     { orbitSpeed: 0.012, gain: 0.92, breathAmp: 0.16, breathFreq: 1.60, flicker: 0.18, particleSpeed: 0.5, particleBrightness: 0.4 },
};

const KEY_CODES: Record<string, number> = {
    KeyW: 87, KeyA: 65, KeyS: 83, KeyD: 68,
    KeyQ: 81, KeyE: 69, KeyR: 82, KeyF: 70,
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
    private particleScene = new THREE.Scene();
    private particleCamera = new THREE.Camera();
    private triangle: THREE.BufferGeometry;
    private passes: Record<'A' | 'B' | 'C' | 'D' | 'image' | 'overlay', THREE.RawShaderMaterial>;
    private particleMat: THREE.RawShaderMaterial;
    private particles: THREE.Points;
    private particleGeom: THREE.BufferGeometry;
    private rtA: [RT, RT];   // ping-pong (历史)
    private rtB: [RT, RT];   // ping-pong (相机状态)
    private rtC!: RT;
    private rtD!: RT;
    private rtImage!: RT;
    private flip = 0;
    private keyboardTex: THREE.DataTexture;
    private keyboardData: Uint8Array;
    private keyListenerDown: (e: KeyboardEvent) => void;
    private keyListenerUp: (e: KeyboardEvent) => void;

    private frameNo = 0;
    private time = 0;
    private lastNow = 0;
    private raf = 0;
    private disposed = false;

    // 状态机
    private state: PulseCoreState = 'idle';
    private cur: StateProfile = { ...STATE_PROFILES.idle };
    private audioEnergy = 0;
    private energySmooth = 0;

    // 合成 iMouse（自动环绕 / 用户拖拽）
    private mouseMode: 'auto' | 'user' | 'switch' = 'auto';
    private userMouse = new THREE.Vector4(0, 0, -1, -1);
    private pointerDrag = false;
    private lastPointer?: { x: number; y: number };

    private canvas: HTMLCanvasElement;
    private scale: number;
    private hideTopologyMap: boolean;
    private dpr: number;
    private resizeObserver: ResizeObserver;

    constructor(canvas: HTMLCanvasElement, opts?: { scale?: number; hideTopologyMap?: boolean }) {
        this.canvas = canvas;
        this.scale = opts?.scale ?? 0.5;
        this.hideTopologyMap = opts?.hideTopologyMap ?? true;
        this.renderer = new THREE.WebGLRenderer({
            canvas,
            antialias: false,
            depth: false,
            stencil: false,
            powerPreference: 'high-performance',
        });
        this.renderer.autoClear = false;
        this.dpr = Math.min(window.devicePixelRatio || 1, 1.5);

        const gl = this.renderer.getContext();
        if (!gl) throw new Error('WebGL2 unavailable');

        // 键盘纹理：256x3, 行0 = 按键状态（Shadertoy keyboard 纹理语义）
        this.keyboardData = new Uint8Array(256 * 3 * 4);
        this.keyboardTex = new THREE.DataTexture(this.keyboardData, 256, 3, THREE.RGBAFormat, THREE.UnsignedByteType);
        this.keyboardTex.needsUpdate = true;

        // 全屏三角形（属性名必须是 position，three 用它算 drawRange）
        this.triangle = new THREE.BufferGeometry();
        this.triangle.setAttribute('position', new THREE.BufferAttribute(new Float32Array([-1, -1, 3, -1, -1, 3]), 2));

        const mkPass = (src: string, extraDefines = '') => new THREE.RawShaderMaterial({
            glslVersion: THREE.GLSL3,
            vertexShader: FULLSCREEN_VERT,
            fragmentShader: SHIM_PREFIX + extraDefines + src + SHIM_MAIN,
            depthTest: false,
            depthWrite: false,
            uniforms: {
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
            },
        });

        // 拓扑图 UI 是原作调试面板（右上角蓝框），网站背景用不到。
        // 加载层精确替换调用行，磁盘上的原作 glsl 文件保持零修改；
        // 若上游版本改了这行导致 no-op，拓扑图会原样出现（可发现、可再适配）。
        const aSrc = this.hideTopologyMap
            ? bufferASrc.replace(/vec4 mapCol = RenderTopologyMap\([^;]+;/, 'vec4 mapCol = vec4(0.0);')
            : bufferASrc;

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
                uniforms: { uTex: { value: null }, uOutRes: { value: new THREE.Vector2(1, 1) }, uGain: { value: 1 } },
            }),
        };

        // 星尘粒子
        const N = 9000;
        const seeds = new Float32Array(N * 3);
        for (let i = 0; i < N; i++) {
            seeds[i * 3] = Math.random();
            seeds[i * 3 + 1] = Math.random() * Math.PI * 2;
            seeds[i * 3 + 2] = Math.random();
        }
        this.particleGeom = new THREE.BufferGeometry();
        // three 需要 position 属性来推导 drawRange；顶点位置在 shader 里由 aSeed 算出
        this.particleGeom.setAttribute('position', new THREE.BufferAttribute(new Float32Array(N * 3), 3));
        this.particleGeom.setAttribute('aSeed', new THREE.BufferAttribute(seeds, 3));
        this.particleMat = new THREE.RawShaderMaterial({
            glslVersion: THREE.GLSL3,
            vertexShader: PARTICLE_VERT,
            fragmentShader: PARTICLE_FRAG,
            transparent: true,
            blending: THREE.AdditiveBlending,
            depthTest: false,
            depthWrite: false,
            uniforms: {
                uTime: { value: 0 },
                uAspect: { value: 1 },
                uSpeed: { value: 1 },
                uPulse: { value: 0 },
                uBrightness: { value: 0.5 },
            },
        });
        this.particles = new THREE.Points(this.particleGeom, this.particleMat);
        this.particles.frustumCulled = false;
        this.particleScene.add(this.particles);

        // 渲染目标（先建一次，resize 里会重建）
        const w = Math.max(2, Math.floor(canvas.clientWidth * this.dpr * this.scale));
        const h = Math.max(2, Math.floor(canvas.clientHeight * this.dpr * this.scale));
        this.rtA = [this.mkRT(w, h), this.mkRT(w, h)];
        this.rtB = [this.mkRT(w, h), this.mkRT(w, h)];
        this.rtC = this.mkRT(w, h);
        this.rtD = this.mkRT(w, h);
        this.rtImage = this.mkRT(w, h);

        // 输入事件
        this.keyListenerDown = (e) => this.setKey(e.code, 255);
        this.keyListenerUp = (e) => this.setKey(e.code, 0);
        window.addEventListener('keydown', this.keyListenerDown);
        window.addEventListener('keyup', this.keyListenerUp);
        canvas.addEventListener('pointerdown', this.onPointerDown);
        window.addEventListener('pointermove', this.onPointerMove);
        window.addEventListener('pointerup', this.onPointerUp);

        this.resizeObserver = new ResizeObserver(() => this.resize());
        this.resizeObserver.observe(canvas);
        this.resize();
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

    private setKey(code: string, v: number) {
        const k = KEY_CODES[code];
        if (k === undefined) return;
        this.keyboardData[k * 4] = v;
        this.keyboardTex.needsUpdate = true;
    }

    private onPointerDown = (e: PointerEvent) => {
        this.pointerDrag = true;
        this.mouseMode = 'switch';  // 先置无效帧清 delta，防视角跳变
        this.lastPointer = { x: e.clientX, y: e.clientY };
    };

    private onPointerMove = (e: PointerEvent) => {
        if (!this.pointerDrag) return;
        this.userMouse.set(e.clientX * this.dpr, (this.canvas.clientHeight - e.clientY) * this.dpr, 1, 1);
        this.lastPointer = { x: e.clientX, y: e.clientY };
    };

    private onPointerUp = () => {
        this.pointerDrag = false;
        this.userMouse.z = -1;
        this.userMouse.w = -1;
    };

    private resize() {
        const cw = this.canvas.clientWidth || 800;
        const ch = this.canvas.clientHeight || 600;
        this.renderer.setPixelRatio(1);
        this.renderer.setSize(cw * this.dpr, ch * this.dpr, false);
        const w = Math.max(2, Math.floor(cw * this.dpr * this.scale));
        const h = Math.max(2, Math.floor(ch * this.dpr * this.scale));
        const rebuild = (old: RT): RT => { old.rt.dispose(); return this.mkRT(w, h); };
        this.rtA = [rebuild(this.rtA[0]), rebuild(this.rtA[1])];
        this.rtB = [rebuild(this.rtB[0]), rebuild(this.rtB[1])];
        this.rtC = rebuild(this.rtC);
        this.rtD = rebuild(this.rtD);
        this.rtImage = rebuild(this.rtImage);
        this.flip = 0;  // 相机状态纹理换了，让 iFrame<=5 之外的兜底(fwd<0.01)自然重置
    }

    setState(s: PulseCoreState) { this.state = s; }
    setAudioEnergy(e: number) { this.audioEnergy = Math.max(0, Math.min(1, e)); }

    start() {
        this.lastNow = performance.now();
        const loop = (now: number) => {
            if (this.disposed) return;
            this.raf = requestAnimationFrame(loop);
            if (document.hidden) { this.lastNow = now; return; }
            const dt = Math.min(0.1, (now - this.lastNow) / 1000);
            this.lastNow = now;
            this.frame(dt);
        };
        this.raf = requestAnimationFrame(loop);
    }

    stop() { cancelAnimationFrame(this.raf); }

    private frame(dt: number) {
        this.frameNo++;
        this.time += dt;
        const p = STATE_PROFILES[this.state];

        // 状态参数平滑过渡（attack/release）
        const k = 1 - Math.exp(-dt * 3);
        this.cur.orbitSpeed += (p.orbitSpeed - this.cur.orbitSpeed) * k;
        this.cur.gain += (p.gain - this.cur.gain) * k;
        this.cur.breathAmp += (p.breathAmp - this.cur.breathAmp) * k;
        this.cur.breathFreq += (p.breathFreq - this.cur.breathFreq) * k;
        this.cur.flicker += (p.flicker - this.cur.flicker) * k;
        this.cur.particleSpeed += (p.particleSpeed - this.cur.particleSpeed) * k;
        this.cur.particleBrightness += (p.particleBrightness - this.cur.particleBrightness) * k;

        // 音频能量包络
        this.energySmooth += (this.audioEnergy - this.energySmooth) * (1 - Math.exp(-dt * (this.audioEnergy > this.energySmooth ? 12 : 3)));

        const breath = 1 + this.cur.breathAmp * Math.sin(this.time * this.cur.breathFreq * Math.PI * 2)
            + this.energySmooth * 0.10;
        const flick = 1 - this.cur.flicker * Math.max(0, Math.sin(this.time * 23.0) * Math.sin(this.time * 7.3));
        const gain = this.cur.gain * breath * flick;

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

        // 合成 iMouse：自动环绕（把角速度换算成像素增量），用户拖拽优先
        let mx: number, my: number, mz: number;
        if (this.mouseMode === 'switch' && !this.pointerDrag) {
            this.mouseMode = 'auto';
        }
        if (this.pointerDrag && this.lastPointer) {
            this.mouseMode = 'user';
            mx = this.userMouse.x; my = this.userMouse.y; mz = 1;
        } else {
            if (this.mouseMode === 'user') { this.mouseMode = 'switch'; }  // 回自动前清一帧
            // 往复摆动（黑洞始终在画面内）：B 由 iMouse 增量还原视角角速度，
            // 所以直接给"角度/灵敏度"的等效像素位置即可
            const yaw = 0.18 * Math.sin(this.time * this.cur.orbitSpeed * 8.0);           // ±10°
            const pit = 0.04 * Math.sin(this.time * this.cur.orbitSpeed * 4.4 + 1.3);     // ±2.3°
            mx = w / 2 + yaw / 0.003;
            my = h / 2 + pit / 0.003;
            mz = 1;
        }

        // ---- Pass A: GR 渲染（读 B 上帧 + A 上帧历史 + 键盘） ----
        const m = this.passes.A;
        setRes(m);
        (m.uniforms.iMouse.value as THREE.Vector4).set(mx, my, mz, mz > 0 ? 1 : -1);
        m.uniforms.iChannel0.value = this.keyboardTex;
        m.uniforms.iChannel1.value = null;
        m.uniforms.iChannel2.value = this.rtB[this.flip].rt.texture;         // 上帧 B
        m.uniforms.iChannel3.value = this.rtA[this.flip].rt.texture;         // 上帧 A (TAA)
        const dstA = this.rtA[1 - this.flip];
        this.renderPass(m, dstA);

        // ---- Pass B: 相机更新 + bloom 明亮层（读 A 本帧 + B 上帧） ----
        const mb = this.passes.B;
        setRes(mb);
        (mb.uniforms.iMouse.value as THREE.Vector4).set(mx, my, mz, mz > 0 ? 1 : -1);
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

        // ---- 星尘粒子（叠加进 Image 输出，避让中心） ----
        this.particleMat.uniforms.uTime.value = this.time;
        this.particleMat.uniforms.uAspect.value = aspect;
        this.particleMat.uniforms.uSpeed.value = this.cur.particleSpeed;
        this.particleMat.uniforms.uPulse.value = this.energySmooth + 0.3 * this.cur.breathAmp;
        this.particleMat.uniforms.uBrightness.value = this.cur.particleBrightness;
        this.renderer.setRenderTarget(this.rtImage.rt);
        this.renderer.render(this.particleScene, this.particleCamera);
        this.renderer.setRenderTarget(null);

        // ---- Overlay: 增益调制 + 上屏（uv 按画布尺寸，把内部缓冲拉伸铺满） ----
        const mo = this.passes.overlay;
        mo.uniforms.uTex.value = this.rtImage.rt.texture;
        const cw = this.renderer.domElement.width, chh = this.renderer.domElement.height;
        (mo.uniforms.uOutRes.value as THREE.Vector2).set(cw, chh);
        mo.uniforms.uGain.value = gain;
        this.renderPass(mo, null);

        this.flip = 1 - this.flip;
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
        window.removeEventListener('keydown', this.keyListenerDown);
        window.removeEventListener('keyup', this.keyListenerUp);
        this.canvas.removeEventListener('pointerdown', this.onPointerDown);
        window.removeEventListener('pointermove', this.onPointerMove);
        window.removeEventListener('pointerup', this.onPointerUp);
        for (const m of Object.values(this.passes)) m.dispose();
        this.particleMat.dispose();
        this.particleGeom.dispose();
        this.triangle.dispose();
        this.keyboardTex.dispose();
        for (const r of [this.rtA[0], this.rtA[1], this.rtB[0], this.rtB[1], this.rtC, this.rtD, this.rtImage]) r.rt.dispose();
        this.renderer.dispose();
    }
}
