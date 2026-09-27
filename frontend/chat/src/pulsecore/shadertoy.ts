// Shadertoy → WebGL2(GLSL ES 3.0) 兼容层
// 原则：原作 pass 代码零修改，所有适配都在这层壳里。

export const SHIM_PREFIX = /* glsl */ `
precision highp float;
precision highp int;
precision highp sampler2D;

uniform vec3  iResolution;      // 当前 pass 渲染目标分辨率 (w,h,aspect)
uniform float iTime;
uniform float iTimeDelta;
uniform int   iFrame;
uniform vec4  iMouse;           // xy=位置, z>0 表示拖拽中 (Shadertoy 语义)
uniform vec3  iChannelResolution[4];

uniform sampler2D iChannel0;
uniform sampler2D iChannel1;
uniform sampler2D iChannel2;
uniform sampler2D iChannel3;

out vec4 _nxFragColor;
`;

// mainImage(c, gl_FragCoord) 调用壳：拼在原作代码之后
export const SHIM_MAIN = /* glsl */ `
void main() {
    vec4 c = vec4(0.0);
    mainImage(c, gl_FragCoord.xy);
    _nxFragColor = c;
}
`;

export const FULLSCREEN_VERT = /* glsl */ `
precision highp float;
// 属性名必须是 position：three 用它计算 drawRange
in vec2 position;
void main() { gl_Position = vec4(position, 0.0, 1.0); }
`;

// 心跳包络：lub-dub 双峰（主峰 0.10，次峰 0.32），JS 传相位
export const heartbeatPulse = (t: number): number => {
    const x = t - Math.floor(t);
    const a = (x - 0.10) / 0.05;
    const b = (x - 0.32) / 0.075;
    return Math.exp(-a * a) + 0.55 * Math.exp(-b * b);
};

// Overlay：亮度增益上屏（黑洞本体的心跳缩放在 Pass A 里改质量尺度实现）
export const OVERLAY_FRAG = /* glsl */ `
precision highp float;
uniform sampler2D uTex;
uniform vec2 uOutRes;
uniform float uGain;         // 亮度增益（已含心跳/音频/闪烁）
out vec4 fragColor;
void main() {
    vec2 uv = gl_FragCoord.xy / uOutRes;
    vec3 col = texture(uTex, uv).rgb;
    fragColor = vec4(col * uGain, 1.0);
}
`;
