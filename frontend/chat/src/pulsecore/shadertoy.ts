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

// 状态调制/上采样输出 pass：乘全局增益后画到屏幕（不改原作色调）
export const OVERLAY_FRAG = /* glsl */ `
precision highp float;
uniform sampler2D uTex;
uniform vec2 uOutRes;
uniform float uGain;
out vec4 fragColor;
void main() {
    vec2 uv = gl_FragCoord.xy / uOutRes;
    vec3 col = texture(uTex, uv).rgb;
    fragColor = vec4(col * uGain, 1.0);
}
`;

// 星尘粒子：绕心流动，避让屏幕中心黑洞区域
export const PARTICLE_VERT = /* glsl */ `
precision highp float;
in vec3 aSeed;                // x:轨道半径(0..1) y:初相 z:速度因子
uniform float uTime;
uniform float uAspect;
uniform float uSpeed;
uniform float uPulse;         // 心跳泵出量 0..1
out float vAlpha;
out vec3 vColor;
void main() {
    float ang = aSeed.y + uTime * uSpeed * (0.05 + 0.12 * aSeed.z);
    float rad = mix(0.30, 0.98, aSeed.x) + uPulse * 0.04 * sin(uTime * (1.0 + aSeed.z * 3.0) + aSeed.y * 7.0);
    vec2 p = vec2(cos(ang) * rad * uAspect, sin(ang) * rad);
    // 微扰动:让粒子轨迹不死板
    p += 0.012 * vec2(sin(uTime * 0.7 + aSeed.y * 13.0), cos(uTime * 0.9 + aSeed.x * 17.0));
    float r = length(p) / max(uAspect, 1.0);
    vAlpha = smoothstep(0.28, 0.5, r) * (0.35 + 0.65 * aSeed.z);
    vColor = mix(vec3(0.75, 0.82, 1.0), vec3(1.0, 0.82, 0.6), step(0.8, fract(aSeed.x * 5.7)));
    gl_Position = vec4(p, 0.0, 1.0);
    gl_PointSize = 1.0 + 2.2 * aSeed.z;
}
`;

export const PARTICLE_FRAG = /* glsl */ `
precision highp float;
in float vAlpha;
in vec3 vColor;
uniform float uBrightness;
out vec4 fragColor;
void main() {
    vec2 d = gl_PointCoord - 0.5;
    float m = exp(-dot(d, d) * 14.0);
    fragColor = vec4(vColor * m * vAlpha * uBrightness, 1.0);
}
`;
