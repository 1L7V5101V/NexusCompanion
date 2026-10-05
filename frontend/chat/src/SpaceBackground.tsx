/**
 * 发送态深空背景：顶部锚定的流动极光（WebGL 全屏 quad）。
 *
 * 着色器参考 Shadertoy 的 hash/noise 与「四层恒定叠加」思路——混色感来自多层同时发光，
 * 而不是把峰收尖成单色主导。纵向包络 sheet 与横向调制 modu 分开算，调制对比度随亮度
 * 衰减：否则暗区会被拉出局部亮点，看着就是光斑从屏幕中间长出来。
 *
 * - 形状/色相/覆盖等参数全部走 uniform，可调项与区间以 PARAM_META 为准，
 *   DEV 下由面板滑条实时驱动（面板在本组件内，仅 import.meta.env.DEV 渲染）；
 * - fixed 层 z -1，自带纯黑底，未激活时即全黑；active 切换只做整层 opacity 过渡；
 * - 非 active 且淡出完成后停掉 RAF，不空转；reduced-motion 下只画静态一帧。
 */
import { useEffect, useRef, useState } from "react";

export type SpaceParams = {
  wavelength: number;
  waveRatio: number;
  waveEvolve: number;
  edgeAmp: number;
  ampContrast: number;
  ampEvolve: number;
  fallBase: number;
  fallVar: number;
  coverage: number;
  coverSoft: number;
  rayDepth: number;
  texDepth: number;
  hueSpeed: number;
  hueFloor: number;
  hueSharp: number;
  hueSpread: number;
  flow: number;
  gain: number;
};

export const DEFAULT_SPACE_PARAMS: SpaceParams = {
  wavelength: 0.55,
  waveRatio: 0.0,
  waveEvolve: 1.7,
  edgeAmp: 2.5,
  ampContrast: 1.26,
  ampEvolve: 0.0,
  fallBase: 1.9,
  fallVar: 3.0,
  coverage: 0.78,
  coverSoft: 0.6,
  rayDepth: 1.0,
  texDepth: 1.0,
  hueSpeed: 2.45,
  hueFloor: 0.34,
  hueSharp: 1.25,
  hueSpread: 0.7,
  flow: 2.2,
  gain: 2.76,
};

const PARAM_META: {
  key: keyof SpaceParams;
  label: string;
  min: number;
  max: number;
  step: number;
}[] = [
  { key: "wavelength", label: "波长", min: 0.3, max: 3, step: 0.05 },
  { key: "waveRatio", label: "波长比例", min: 0, max: 3, step: 0.02 },
  { key: "waveEvolve", label: "波长变化速度", min: 0, max: 3, step: 0.02 },
  { key: "edgeAmp", label: "波幅", min: 0, max: 3, step: 0.02 },
  { key: "ampContrast", label: "波高比例", min: 0, max: 3, step: 0.02 },
  { key: "ampEvolve", label: "波高变化速度", min: 0, max: 3, step: 0.02 },
  { key: "fallBase", label: "纵向衰减", min: 0.5, max: 6, step: 0.1 },
  { key: "fallVar", label: "衰减起伏", min: 0, max: 6, step: 0.05 },
  { key: "coverage", label: "覆盖高度", min: 0.15, max: 1, step: 0.01 },
  { key: "coverSoft", label: "收边宽度", min: 0.04, max: 0.6, step: 0.01 },
  { key: "rayDepth", label: "光柱深浅", min: 0, max: 1, step: 0.02 },
  { key: "texDepth", label: "纹理深浅", min: 0, max: 1, step: 0.02 },
  { key: "hueSpeed", label: "变色速度", min: 0.2, max: 4, step: 0.05 },
  { key: "hueFloor", label: "混色底", min: 0, max: 0.5, step: 0.01 },
  { key: "hueSharp", label: "色相锐度", min: 0.8, max: 5, step: 0.05 },
  { key: "hueSpread", label: "色相分区差", min: 0, max: 3, step: 0.05 },
  { key: "flow", label: "流速", min: 0, max: 3, step: 0.05 },
  { key: "gain", label: "整体亮度", min: 0.2, max: 3, step: 0.02 },
];

const STORE_KEY = "nexus.space.params";

function loadParams(): SpaceParams {
  try {
    const raw = localStorage.getItem(STORE_KEY);
    if (!raw) return DEFAULT_SPACE_PARAMS;
    const saved = JSON.parse(raw) as Partial<SpaceParams>;
    const next = { ...DEFAULT_SPACE_PARAMS };
    for (const { key } of PARAM_META) {
      const v = saved[key];
      if (typeof v === "number" && Number.isFinite(v)) next[key] = v;
    }
    return next;
  } catch {
    return DEFAULT_SPACE_PARAMS;
  }
}

const VERT = `
attribute vec2 a_pos;
void main() {
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`;

const FRAG = `
precision highp float;

uniform vec2 u_res;
uniform float u_time;
uniform float u_wavelength;
uniform float u_waveRatio;
uniform float u_waveEvolve;
uniform float u_edgeAmp;
uniform float u_ampEvolve;
uniform float u_ampContrast;
uniform float u_fallBase;
uniform float u_fallVar;
uniform float u_coverage;
uniform float u_coverSoft;
uniform float u_rayDepth;
uniform float u_texDepth;
uniform float u_hueSpeed;
uniform float u_hueFloor;
uniform float u_hueSharp;
uniform float u_hueSpread;
uniform float u_flow;
uniform float u_gain;

float hash(float n) {
  return fract(sin(n) * 43758.5453);
}

float noise(vec2 p) {
  vec2 i = floor(p);
  vec2 f = fract(p);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(mix(hash(i.x + hash(i.y)), hash(i.x + 1.0 + hash(i.y)), u.x),
             mix(hash(i.x + hash(i.y + 1.0)), hash(i.x + 1.0 + hash(i.y + 1.0)), u.x), u.y);
}

// 下缘线本身再被 2D 噪声 warp 上下推，纵向不再等距插值；各层 fx/dir 不同使波长与流向错开。
// 波长与波幅各由一条绝对时间驱动的低频噪声缩放（stretch / amp），所以图案不是刚性平移，
// 而是边走边呼吸；两者仍只依赖 x，列内纵向一致，不会因此长出孤立亮斑。
vec3 auroraLayer(vec2 uv, float speed, float intensity, vec3 color, float w,
                 float fx, float dir, float base) {
  float t = u_time * speed * u_flow;
  // 局部波长 = 基准波长 × (1 ± waveRatio)，摆动幅度可调、且绕 1.0 对称；
  // waveEvolve 控它换相的快慢。只依赖 x，列内纵向一致，不会长出孤立亮斑。
  float stretch = 1.0 + (noise(vec2(u_time * u_waveEvolve + color.x * 4.0, uv.x * fx * 0.5)) - 0.5)
                * 2.0 * u_waveRatio;
  float cx = uv.x * fx * u_wavelength * stretch + t * 2.0 * dir;
  // 包络频率取载波的 0.9 倍：波峰间隔 1 个 cx 单位，包络每过一个峰换约 0.9 胞，
  // 相邻峰就落在异相位置、高度差才拉得开（取 0.3 时包络比载波慢 3 倍 → 所有峰一样高）。
  float env = 0.15 + 1.65 * noise(vec2(cx * 0.9 + 5.0, u_time * u_ampEvolve + color.x * 3.0));
  float amp = max(0.0, mix(1.0, env, u_ampContrast));
  // 门限噪声取中心化 (−0.5)，使 edge 能双向摆动；否则它只会把下缘往下推，
  // 一旦落到收边遮罩以下就被遮罩接管，各列下缘变成同一条线（相邻波等高）。
  float edge = base + amp * u_edgeAmp * (0.6 * noise(vec2(cx, color.x * 7.0 - t))
                                       + 0.4 * noise(vec2(cx * 1.6 + 13.0, color.x * 5.0 - t * 1.7)) - 0.5);
  float fall = u_fallBase + u_fallVar * noise(vec2(cx * 0.8 + 31.0, color.y * 9.0 + t * 0.5));
  float warp = 0.22 * noise(vec2(cx * 0.7 + 3.0, uv.y * 0.5 - t * 0.6));
  float sheet = exp(-uv.y * fall) * (1.0 - smoothstep(edge, edge + 0.35, uv.y + warp));
  // 纹理偏竖向（y 向频率低）：横向变化读作光柱，纵向变化才读作圆斑
  float tex = mix(1.0 - u_texDepth, 1.0, noise(vec2(uv.x * 1.6 + t * 2.0 + color.x, uv.y * 0.25 - t * 0.5 + color.y)));
  float r = uv.x * 3.0 * u_wavelength * stretch + t * 3.0 * dir + color.z * 11.0;
  float ray = mix(1.0 - u_rayDepth, 1.0, noise(vec2(r, 0.5)));
  float modu = tex * mix(1.0, ray, noise(vec2(r * 0.25 + 7.0, 1.5)));
  // 关键：amp 不只移动门限，还直接缩放亮度。真正决定「相邻波谁高谁低」的是
  // 指数衰减本身，门限只在其上方微调；不缩放亮度则各列衰减曲线几乎重合。
  float body = sheet * mix(1.0, modu, pow(sheet, 0.45)) * amp;
  return color * body * intensity * w * 2.0;
}

// 混色底 + 宽裙边：四层始终同时发光、只是强弱轮转，色相在重叠处相加。
// phase 带空间项（见 main 的 field），故变色沿屏幕逐区推进而非整屏同步。
float hueWeight(float phase) {
  return u_hueFloor + (1.0 - u_hueFloor)
       * pow(0.5 + 0.5 * sin(u_time * u_hueSpeed + phase), u_hueSharp);
}

void main() {
  vec2 sn = gl_FragCoord.xy / u_res.xy;
  float v = 1.0 - sn.y;
  vec2 uv = vec2(sn.x * (u_res.x / u_res.y), v);

  // 推进方向由低频噪声连续游走决定（按周期硬切会让整屏瞬间换向 = 闪烁）
  float ang = 6.2831853 * noise(vec2(u_time * 0.07, 4.3));
  float field = (dot(sn - vec2(0.5, 0.4), vec2(cos(ang), sin(ang))) * 1.1
               + 0.45 * noise(vec2(sn.x * 1.6 + u_time * 0.05, sn.y * 1.6))) * u_hueSpread;

  vec3 c = vec3(0.0);
  c += auroraLayer(uv, 0.06, 0.38, vec3(0.18, 0.35, 0.95), hueWeight(0.6 + field), 1.5, 1.0, 0.18);
  c += auroraLayer(uv, 0.10, 0.28, vec3(0.00, 0.65, 0.50), hueWeight(4.77 + field * 1.35), 1.9, -1.0, 0.22);
  c += auroraLayer(uv, 0.14, 0.32, vec3(0.85, 0.75, 0.05), hueWeight(3.33 + field * 0.8), 1.3, 1.0, 0.14);
  c += auroraLayer(uv, 0.08, 0.17, vec3(0.12, 0.16, 0.55), 0.25, 1.7, -1.0, 0.24);

  c *= 1.0 - smoothstep(u_coverage, u_coverage + u_coverSoft, v);
  c = 1.0 - exp(-c * u_gain * 1.5);
  gl_FragColor = vec4(c, 1.0);
}
`;

function compileShader(
  gl: WebGLRenderingContext,
  type: number,
  source: string,
): WebGLShader | null {
  const shader = gl.createShader(type);
  if (!shader) return null;
  gl.shaderSource(shader, source);
  gl.compileShader(shader);
  if (!gl.getShaderParameter(shader, gl.COMPILE_STATUS)) {
    console.error("[space-bg] shader compile failed:", gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

const TUNABLE = import.meta.env.DEV;

export function SpaceBackground({ active }: { active: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  const startRef = useRef<(() => void) | null>(null);
  const stopRef = useRef<(() => void) | null>(null);
  const paramsRef = useRef<SpaceParams>(DEFAULT_SPACE_PARAMS);
  const uniformRef = useRef<Partial<Record<keyof SpaceParams, WebGLUniformLocation | null>>>({});
  const [params, setParams] = useState<SpaceParams>(DEFAULT_SPACE_PARAMS);
  const [panelOpen, setPanelOpen] = useState(false);
  const [preview, setPreview] = useState(false);

  useEffect(() => {
    if (!TUNABLE) return;
    const saved = loadParams();
    paramsRef.current = saved;
    setParams(saved);
  }, []);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    const opts = { alpha: false, antialias: false, powerPreference: "low-power" } as const;
    const gl = (canvas.getContext("webgl2", opts) ??
      canvas.getContext("webgl", opts)) as WebGLRenderingContext | null;
    if (!gl) return;

    const vert = compileShader(gl, gl.VERTEX_SHADER, VERT);
    const frag = compileShader(gl, gl.FRAGMENT_SHADER, FRAG);
    if (!vert || !frag) return;
    const program = gl.createProgram();
    if (!program) return;
    gl.attachShader(program, vert);
    gl.attachShader(program, frag);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error("[space-bg] link failed:", gl.getProgramInfoLog(program));
      return;
    }
    gl.useProgram(program);

    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const aPos = gl.getAttribLocation(program, "a_pos");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    const uRes = gl.getUniformLocation(program, "u_res");
    const uTime = gl.getUniformLocation(program, "u_time");
    const locs: Partial<Record<keyof SpaceParams, WebGLUniformLocation | null>> = {};
    for (const { key } of PARAM_META) locs[key] = gl.getUniformLocation(program, `u_${key}`);
    uniformRef.current = locs;

    // 全屏噪声场不需要高解析度，限 1.25x 省填充率
    const dpr = Math.min(window.devicePixelRatio || 1, 1.25);
    const resize = () => {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.uniform2f(uRes, canvas.width, canvas.height);
    };
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    resize();

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;
    let raf = 0;
    let running = false;
    // 时间原点：每次开始发送时归零，令色相叙事总从蓝起步
    let t0 = performance.now();

    const drawAt = (now: number) => {
      const p = paramsRef.current;
      for (const { key } of PARAM_META) {
        const loc = locs[key];
        if (loc) gl.uniform1f(loc, p[key]);
      }
      gl.uniform1f(uTime, (now - t0) / 1000);
      gl.drawArrays(gl.TRIANGLES, 0, 3);
    };
    const loop = (now: number) => {
      drawAt(now);
      raf = requestAnimationFrame(loop);
    };

    startRef.current = () => {
      if (running) return;
      t0 = performance.now();
      if (reduced) {
        drawAt(performance.now());
        return;
      }
      running = true;
      raf = requestAnimationFrame(loop);
    };
    stopRef.current = () => {
      running = false;
      cancelAnimationFrame(raf);
    };

    return () => {
      stopRef.current?.();
      ro.disconnect();
      startRef.current = null;
      stopRef.current = null;
    };
  }, []);

  const on = TUNABLE ? active || preview : active;

  useEffect(() => {
    if (on) {
      startRef.current?.();
      return;
    }
    // 等整层 opacity 淡出（450ms）完成后再停 RAF
    const timer = setTimeout(() => stopRef.current?.(), 500);
    return () => clearTimeout(timer);
  }, [on]);

  const update = (key: keyof SpaceParams, value: number) => {
    const next = { ...paramsRef.current, [key]: value };
    paramsRef.current = next;
    setParams(next);
    try {
      localStorage.setItem(STORE_KEY, JSON.stringify(next));
    } catch {
      /* 隐私模式下忽略：只影响调参记忆，不影响渲染 */
    }
  };

  const reset = () => {
    paramsRef.current = DEFAULT_SPACE_PARAMS;
    setParams(DEFAULT_SPACE_PARAMS);
    try {
      localStorage.removeItem(STORE_KEY);
    } catch {
      /* 同上 */
    }
  };

  return (
    <>
      <div className={`space-bg${on ? " is-active" : ""}`} aria-hidden="true">
        <canvas ref={canvasRef} className="space-bg-canvas" />
      </div>
      {TUNABLE ? (
        <>
          <button
            type="button"
            className="space-tuner-toggle"
            onClick={() => setPanelOpen((v) => !v)}
          >
            {panelOpen ? "收起极光参数" : "极光参数"}
          </button>
          {panelOpen ? (
            <div className="space-tuner">
              <label className="space-tuner-row space-tuner-switch">
                <input
                  type="checkbox"
                  checked={preview}
                  onChange={(e) => setPreview(e.target.checked)}
                />
                <span>常驻预览（不用反复发送）</span>
              </label>
              {PARAM_META.map(({ key, label, min, max, step }) => (
                <label key={key} className="space-tuner-row">
                  <span className="space-tuner-name">{label}</span>
                  <input
                    type="range"
                    min={min}
                    max={max}
                    step={step}
                    value={params[key]}
                    onChange={(e) => update(key, Number(e.target.value))}
                  />
                  <span className="space-tuner-val">{params[key].toFixed(2)}</span>
                </label>
              ))}
              <button type="button" className="space-tuner-reset" onClick={reset}>
                复位
              </button>
            </div>
          ) : null}
        </>
      ) : null}
    </>
  );
}
