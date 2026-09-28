import { useEffect, useRef, useState } from "react";

/**
 * 极光边框画布（Audio Eclipse 思想的连续场版本）：
 * 对每个像素求圆角矩形 SDF 与最近边框点的弧长参数 s，
 * 发光 = palette(s + 时间) × 振幅(s,t) × exp(-d/厚度(s,t))，
 * 振幅与厚度都是 (环向圆域坐标, 时间) 的 fbn 噪声——
 * 光带连续流动、粗细明暗平滑起伏，没有离散光点。
 * 原生 WebGL 零依赖（单个全屏 quad；shader 可原样平移到 three）。
 */

const MARGIN = 150;

const VERT = `
attribute vec2 a_pos;
void main() {
  gl_Position = vec4(a_pos, 0.0, 1.0);
}
`;

const FRAG = `
precision highp float;

uniform vec2 u_res;       // canvas 尺寸（css px）
uniform float u_dpr;
uniform float u_time;
uniform vec4 u_rect;      // center.xy, half.xy（css px，canvas 局部）
uniform float u_radius;
uniform float u_active;
uniform float u_debug;    // ?aurora-debug=1：输出 s 灰度(R)与分支编号(G)

float hash(vec2 q) {
  return fract(sin(dot(q, vec2(127.1, 311.7))) * 43758.5453123);
}
float vnoise(vec2 q) {
  vec2 i = floor(q);
  vec2 f = fract(q);
  vec2 u = f * f * (3.0 - 2.0 * f);
  return mix(
    mix(hash(i), hash(i + vec2(1.0, 0.0)), u.x),
    mix(hash(i + vec2(0.0, 1.0)), hash(i + vec2(1.0, 1.0)), u.x),
    u.y
  );
}
float fbn(vec2 q) {
  float v = 0.0;
  float a = 0.5;
  for (int i = 0; i < 3; i++) {
    v += a * vnoise(q);
    q = q * 2.07 + vec2(11.3, 7.9);
    a *= 0.5;
  }
  return v;
}
vec3 palette(float t) {
  t = fract(t);
  vec3 c1 = vec3(0.26, 0.52, 0.96);
  vec3 c2 = vec3(0.61, 0.45, 0.80);
  vec3 c3 = vec3(0.86, 0.40, 0.45);
  vec3 c4 = vec3(0.95, 0.66, 0.24);
  float s = t * 4.0;
  vec3 col = mix(c1, c2, clamp(s, 0.0, 1.0));
  col = mix(col, c3, clamp(s - 1.0, 0.0, 1.0));
  col = mix(col, c4, clamp(s - 2.0, 0.0, 1.0));
  col = mix(col, c1, clamp(s - 3.0, 0.0, 1.0));
  return col;
}

// 最近边框点 -> vec4(bp.xy, s, br)（bp 相对 composer 中心；s 周长参数 0..1；
// br 分段编号 1-8：TR/BR/BL/TL 角与上右下左边，用于 ?aurora-debug 诊断）
vec4 closestBorder(vec2 pc, vec2 h, float r) {
  float ax = h.x - r;
  float ay = h.y - r;
  float arc = 1.5707963 * r;
  vec2 aq = abs(pc) - h + r;
  vec2 sgn = sign(pc);
  vec2 bp;
  float s;
  float br;
  if (aq.x > 0.0 && aq.y > 0.0) {
    // 角区：最近点在四分之一圆弧上
    vec2 dir = sgn * aq / max(length(aq), 1e-4);
    float phi = atan(dir.y, dir.x);
    bp = sgn * vec2(ax, ay) + r * dir;
    if (sgn.x > 0.0 && sgn.y < 0.0) {
      s = 2.0 * ax + (phi + 1.5707963) * r;
      br = 1.0;
    } else if (sgn.x > 0.0 && sgn.y > 0.0) {
      s = 2.0 * ax + arc + 2.0 * ay + phi * r;
      br = 2.0;
    } else if (sgn.x < 0.0 && sgn.y > 0.0) {
      s = 4.0 * ax + 2.0 * arc + 2.0 * ay + (phi - 1.5707963) * r;
      br = 3.0;
    } else {
      s = 4.0 * ax + 3.0 * arc + 4.0 * ay + (phi + 3.14159265) * r;
      br = 4.0;
    }
  } else if (aq.y > 0.0) {
    // 上/下直边
    bp = vec2(clamp(pc.x, -ax, ax), sgn.y * h.y);
    if (sgn.y < 0.0) {
      s = bp.x + ax;
      br = 5.0;
    } else {
      s = 2.0 * ax + 2.0 * arc + 2.0 * ay + (ax - bp.x);
      br = 6.0;
    }
  } else {
    // 左/右直边
    bp = vec2(sgn.x * h.x, clamp(pc.y, -ay, ay));
    if (sgn.x > 0.0) {
      s = 2.0 * ax + arc + (bp.y + ay);
      br = 7.0;
    } else {
      s = 4.0 * ax + 3.0 * arc + 2.0 * ay + (ay - bp.y);
      br = 8.0;
    }
  }
  float total = 4.0 * (ax + ay) + 2.0 * 3.14159265 * r;
  return vec4(bp, s / total, br / 8.0);
}

void main() {
  vec2 p = gl_FragCoord.xy / u_dpr;
  vec2 local = vec2(p.x, u_res.y - p.y);
  vec2 pc = local - u_rect.xy;

  vec2 h = u_rect.zw;
  float r = u_radius;
  vec2 aq = abs(pc) - h + r;
  vec2 clq = max(aq, 0.0);
  float d = length(clq) + min(max(aq.x, aq.y), 0.0) - r;

  vec4 border = closestBorder(pc, h, r);
  float s = border.z;

  // 环向圆域坐标：噪声沿周长首尾连续
  vec2 sc = vec2(cos(s * 6.2831853), sin(s * 6.2831853));

  // 单段短流光：长度≈输入框横向宽度的一半，绕边框流动（约 37s 一圈）。
  // 头部锐利、尾部弥散（Gemini 渐变解剖），长度轻微呼吸；其余部分全暗。
  float cPos = fract(0.12 + u_time * 0.027 + 0.05 * sin(u_time * 0.11));
  float x = s - cPos;
  x = x - floor(x + 0.5);
  float w = 0.105 * (1.0 + 0.18 * sin(u_time * 0.23 + 1.7));
  float prof = smoothstep(-1.25 * w, -0.12 * w, x) * smoothstep(1.05 * w, 0.30 * w, x);
  float thick = 13.0 + 30.0 * fbn(sc * 2.6 + vec2(u_time * 0.10, -u_time * 0.07));
  float amp = 1.25 * prof * (0.62 + 0.50 * fbn(sc * 3.0 + vec2(-u_time * 0.06, u_time * 0.16)));
  vec3 col = palette(s + u_time * 0.02 + 0.18 * amp);
  vec3 c = col * amp * exp(-max(d, -2.5) / thick) * exp(-max(d, 0.0) / 50.0);

  // 细边线：1.5px 活线，跟随振幅——暗段里细线同样熄灭，无固定颜色
  float rimN = fbn(sc * 1.8 + vec2(u_time * 0.31, -u_time * 0.23));
  c += palette(s + 0.5 + u_time * 0.02) * exp(-abs(d) / 1.4) * (0.42 + 0.48 * rimN) * smoothstep(0.02, 0.35, amp);

  // 只保留边框外圈与 2px 内衬
  c *= smoothstep(-3.0, 1.5, d);

  c *= u_active;
  c = 1.0 - exp(-c * 1.35);
  // 预乘 alpha：无光处透明，光晕按亮度与下方内容合成
  float alpha = clamp(max(c.r, max(c.g, c.b)), 0.0, 1.0);
  if (u_debug > 0.5) {
    gl_FragColor = vec4(border.z, border.w, 0.0, 1.0);
    return;
  }
  gl_FragColor = vec4(c * alpha, alpha);
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
    console.error("[aurora] shader compile failed:", gl.getShaderInfoLog(shader));
    gl.deleteShader(shader);
    return null;
  }
  return shader;
}

export function AuroraCanvas({ active }: { active: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement | null>(null);
  const activeRef = useRef(active);
  activeRef.current = active;
  const kickRef = useRef<(() => void) | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    const canvas = canvasRef.current;
    if (!canvas) return;
    // alpha:true + 亮度作 alpha：无光处全透明，不遮挡画布上方叠着的内容
    // （画布向上伸 150px，opaque 上下文会把消息列表底部盖成黑块）
    const opts = { alpha: true, antialias: false, powerPreference: "low-power" } as const;
    const gl = (canvas.getContext("webgl2", opts) ??
      canvas.getContext("webgl", opts)) as WebGLRenderingContext | null;
    if (!gl) {
      setFailed(true);
      return;
    }

    const vert = compileShader(gl, gl.VERTEX_SHADER, VERT);
    const frag = compileShader(gl, gl.FRAGMENT_SHADER, FRAG);
    if (!vert || !frag) {
      setFailed(true);
      return;
    }
    const program = gl.createProgram();
    if (!program) {
      setFailed(true);
      return;
    }
    gl.attachShader(program, vert);
    gl.attachShader(program, frag);
    gl.linkProgram(program);
    if (!gl.getProgramParameter(program, gl.LINK_STATUS)) {
      console.error("[aurora] link failed:", gl.getProgramInfoLog(program));
      setFailed(true);
      return;
    }
    gl.useProgram(program);
    canvas.dataset.gl = "ok";

    const buffer = gl.createBuffer();
    gl.bindBuffer(gl.ARRAY_BUFFER, buffer);
    gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 3, -1, -1, 3]), gl.STATIC_DRAW);
    const aPos = gl.getAttribLocation(program, "a_pos");
    gl.enableVertexAttribArray(aPos);
    gl.vertexAttribPointer(aPos, 2, gl.FLOAT, false, 0, 0);

    const uRes = gl.getUniformLocation(program, "u_res");
    const uDpr = gl.getUniformLocation(program, "u_dpr");
    const uTime = gl.getUniformLocation(program, "u_time");
    const uRect = gl.getUniformLocation(program, "u_rect");
    const uRadius = gl.getUniformLocation(program, "u_radius");
    const uActive = gl.getUniformLocation(program, "u_active");
    const uDebug = gl.getUniformLocation(program, "u_debug");
    const debugMode = new URLSearchParams(window.location.search).get("aurora-debug") === "1" ? 1 : 0;

    const dpr = Math.min(window.devicePixelRatio || 1, 1.75);
    const resize = () => {
      const w = canvas.clientWidth;
      const h = canvas.clientHeight;
      if (!w || !h) return;
      canvas.width = Math.round(w * dpr);
      canvas.height = Math.round(h * dpr);
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(0, 0, 0, 0);
      gl.clear(gl.COLOR_BUFFER_BIT);
      const cw = w - 2 * MARGIN;
      const ch = h - 2 * MARGIN;
      const radius = Math.min(28, cw / 2, ch / 2);
      gl.uniform2f(uRes, w, h);
      gl.uniform4f(uRect, w / 2, h / 2, cw / 2, ch / 2);
      gl.uniform1f(uRadius, radius);
      gl.uniform1f(uDpr, dpr);
      gl.uniform1f(uDebug, debugMode);
    };
    const ro = new ResizeObserver(resize);
    ro.observe(canvas);
    resize();

    const reduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

    let running = false;
    let raf = 0;
    let lastTick = 0;
    let prevActive = activeRef.current;
    let turnAt = -1e9;

    const drawFrame = (now: number) => {
      lastTick = now;
      const act = activeRef.current;
      if (act !== prevActive) {
        prevActive = act;
        turnAt = now;
      }
      gl.uniform1f(uTime, now / 1000);
      gl.uniform1f(uActive, act ? 1 : Math.max(0, 1 - (now - turnAt) / 600));
      gl.drawArrays(gl.TRIANGLES, 0, 3);
      if (!act && now - turnAt > 700) {
        running = false;
        return;
      }
      raf = requestAnimationFrame(drawFrame);
    };

    // 安全网：标签页进后台后 rAF 停摆，光段会冻在半路（看似另一块静止光斑）；
    // rAF 停摆超 400ms 时用低速定时器续命（约 4fps 爬行），回前台自动交还 rAF。
    const safety = setInterval(() => {
      if (!running) return;
      const now = performance.now();
      if (now - lastTick > 400) drawFrame(now);
    }, 250);

    kickRef.current = () => {
      if (running) return;
      if (reduced) {
        // 降级：静止单帧，不进 rAF 循环。
        gl.uniform1f(uTime, 4.2);
        gl.uniform1f(uActive, 1);
        gl.drawArrays(gl.TRIANGLES, 0, 3);
        return;
      }
      running = true;
      raf = requestAnimationFrame(drawFrame);
    };
    if (activeRef.current) kickRef.current();

    return () => {
      running = false;
      cancelAnimationFrame(raf);
      clearInterval(safety);
      ro.disconnect();
      kickRef.current = null;
      // 不主动 loseContext：StrictMode 的卸载重挂会复用同一 canvas，
      // 丢失的 context 无法恢复，第二次挂载会全部失败。
    };
  }, []);

  useEffect(() => {
    if (!failed && active) kickRef.current?.();
  }, [active, failed]);

  if (failed) {
    // WebGL 不可用：退回 CSS 渐变环。
    return (
      <div
        className="composer-ring-fallback"
        style={{ opacity: active ? 1 : 0 }}
        aria-hidden="true"
      />
    );
  }
  return <canvas ref={canvasRef} className="composer-aurora-canvas" aria-hidden="true" />;
}
