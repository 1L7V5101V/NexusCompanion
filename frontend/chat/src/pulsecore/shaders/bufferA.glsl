//#version 450
//#pragma shader_stage(fragment)
//#extension GL_EXT_samplerless_texture_functions : enable
/*
================================================================================
克尔-纽曼黑洞 (Kerr-Newman Black Hole) 实时广义相对论渲染器
Kerr-Newman Black Hole Real-time General Relativity Renderer
================================================================================

[ 简介 / Introduction ]
    本Shader实现了基于物理的克尔-纽曼时空（带电旋转黑洞）光线追踪。
    This shader implements physically based ray-tracing in Kerr-Newman spacetime
    (charged rotating black hole).

    实现了完整的时空拓扑：外视界、内视界、内外能层、奇环，以及反宇宙。
    Implements complete spacetime topology: outer/inner event horizons,
    outer/inner ergospheres, ringularity, and the antiverse.

    实现了完整的克尔-纽曼时空最大延拓解。光线可以穿过内视界进入
    全新的时空域（白洞区域），并最终抵达另一个宇宙。
    Implements the complete maximal extension of Kerr-Newman spacetime. 
    Rays can traverse the inner horizon into new spacetime domains (white hole regions) 
    and eventually emerge into another  universe.

    实现了基于物理的偏振（极化）模拟。通过在弯曲时空中计算
    Walker-Penrose常数，精确模拟吸积盘发光在强引力场下的偏振态演化。
    Implements physically-based polarization simulation. By calculating 
    Walker-Penrose constants in curved spacetime, it accurately simulates the 
    evolution of polarization states from the accretion disk under strong gravity.

    支持裸奇点。支持切换静态观者、自由落体观者以及自定义坐标速度观者。
    Supports naked singularities. Supports Static, Free-falling, and Custom-velocity observers.

    完全拟真广义相对论效应，包含吸积盘、相对论喷流、引力透镜、
    多普勒频移、引力红移以及热浪折射。
    Fully simulates GR effects: accretion disk, relativistic jets, gravitational
    lensing, Doppler shift, gravitational redshift, and heat haze refraction.

--------------------------------------------------------------------------------

[ 操作方式 / Controls ]
 键盘控制移动，鼠标控制视角：
 Keyboard controls movement, mouse controls camera angle:

     [W] / [S] : 前进 / 后退 (Forward / Backward)
     [A] / [D] : 向左 / 向右 (Left / Right)
     [R] / [F] : 上升 / 下降 (Up / Down - Relative to View)
     [Q] / [E] : 镜头翻滚 (Camera Roll)
     [鼠标/Mouse] : 旋转视角 (Look Around - Pitch/Yaw)
     
     移动速度与鼠标灵敏度调节见bufferB
     Adjustment of movement speed and mouse sensitivity is detailed in bufferB.
--------------------------------------------------------------------------------

[ 性能与建议 / Performance & Advice ]
开启网格(iGrid)或最大延拓(iWhitehole)会增加计算步数，导致帧率下降。
Enabling the grid (iGrid) or maximal extension (iWhitehole) increases the 
number of integration steps, leading to a drop in frame rate.



偏振解算与吸积盘体积云噪声计算开销较大。
Polarization solving and volumetric cloud noise calculations are expensive.

白洞需要消耗大量步数。
White holes  require many steps.
--------------------------------------------------------------------------------

[ 已知问题 / Known Issues ]
这个项目尚未完全完成。
This project is a work in progress and is not yet fully finished.

目前的吸积盘和喷流内部子采样逻辑仍存在未定位的Bug，会导致层纹伪影。
There is currently an unidentified bug in the internal sub-sampling logic
for the accretion disk and jets, causing banding artifacts.

最大延拓模式下，在白洞/黑洞系切换的瞬时可能存在极小范围的视角跳变。
In maximal extension mode, slight view jumps may occur during the 
instantaneous transition between white hole and black hole coordinate systems.

================================================================================

Code introduction and development tutorial（代码介绍与开发教程）:
https://zhuanlan.zhihu.com/p/2003513260645830673

GitHub:
https://github.com/baopinshui/NPGS/blob/master/NPGS/Sources/Engine/Shaders/BlackHole_common.glsl

Videos（视频）：
youtube：https://www.youtube.com/channel/UCLaA2IcAUmnaQfiZ6oES5Ew
BILINBILI：https://space.bilibili.com/95332087
================================================================================
The code rain effect is sourced from https://www.shadertoy.com/view/4t3BWl
================================================================================
*/
// =============================================================================
// SECTION 1: 渲染参数 (Uniforms 转为 Defines)
// =============================================================================

#define iHistoryTex iChannel3
#define textureQueryLod(s, d) vec2(0.0)

// -----------------------------------------------------------------------------
// 物理与渲染参数
// -----------------------------------------------------------------------------
#define iFovRadians  (60.0 * 0.01745329) // 视野 (弧度) / Field of View (Radians)
#define iDEBUG                  0      // 调试模式 (0:正常 1:遮罩/网格 2:动量 3:步数热力图 4:频移) / Debug Mode (0:Normal 1:Mask/Grid 2:Momentum 3:Step Heatmap 4:Frequency Shift)
#define iPrepass                0      // 预渲染低分辨率插值 / Low-res Pre-pass Interpolation
#define iWhitehole              0      // 最大延拓 (白洞与平行宇宙) / Maximal Extension (White Hole & Parallel Universes)
#define iInWhichUniverse        0      // 当前宇宙编号 / Current Universe Index
#define iGrid                   0      // 坐标网格 (0:关 1:黑体 2:固定色) / Coordinate Grid (0:Off 1:Blackbody 2:Fixed Color)
#define iEnableHeatHaze         1      // 开启热浪折射 / Enable Heat Haze Refraction
#define iEnableShadowCulling    0      // 开启阴影剔除优化 / Enable Shadow Culling Optimization
#define iObserverMode           0      // 观者模式 (0:静态 1:落体 2/3:自定义速度) / Observer Mode (0:Static 1:Falling 2/3:Custom Velocity)
#define iPolarization           0      // 偏振模式 (0:关 1:色相映射 2:滤光片) / Polarization Mode (0:Off 1:Hue Mapping 2:Filter)
#define iQuality                1.0    // 渲染质量系数 / Rendering Quality Factor

#define iBlackHoleTime          (2.0*iTime) // 物理演化时间 / Black Hole Evolution Time
#define iBlackHoleMassSol       (1e7)     // 质量 (太阳质量倍数) / Mass (Solar Masses)
#define iSpin                   0.997114514      // 无量纲自旋 a* / Dimensionless Spin a*
#define iQ                      0.0       // 无量纲电荷 Q* / Dimensionless Charge Q*

#define iMu                     1.0       // 吸积物质比荷 / Specific Charge of Accreting Matter
#define iAccretionRate          (5e-4)    // 吸积率 / Accretion Rate

#define iBackShiftMax           2.0       // 背景最大频移限制 / Max Background Frequency Shift
#define iInterRadiusRs          2.0       // 吸积盘内半径 (Rs) / Accretion Disk Inner Radius (Rs)
#define iOuterRadiusRs          20.0      // 吸积盘外半径 (Rs) / Accretion Disk Outer Radius (Rs)
#define iThinRs                 0.75      // 吸积盘半厚度 (Rs) / Accretion Disk Half-Thickness (Rs)
#define iHopper                 0.24      // 吸积盘厚度斜率 / Accretion Disk Thickness Slope
#define iBrightmut              1.0       // 吸积盘亮度乘数 / Accretion Disk Brightness Multiplier
#define iDarkmut                0.5       // 吸积盘不透明度 / Accretion Disk Opacity
#define iReddening              0.3       // 盘红化因子 / Disk Reddening Factor
#define iSaturation             0.5       // 吸积盘饱和度 / Accretion Disk Saturation
#define iBlackbodyIntensityExponent 0.5   // 黑体强度指数 / Blackbody Intensity Exponent
#define iRedShiftColorExponent      3.0   // 频移色温指数 / Redshift Color Temp Exponent
#define iRedShiftIntensityExponent  4.0   // 频移亮度指数 / Redshift Intensity Exponent

#define iPolarizationAngle      0.0       // 偏振片角度 / Polarization Filter Angle
#define iHeatHaze               1.0       // 热浪扰动强度 / Heat Haze Disturbance Strength
#define iBackgroundBrightmut    1.0       // 背景亮度乘数 / Background Brightness Multiplier

#define iPhotonRingBoost             7.0  // 光子环亮度增亮 / Photon Ring Brightness Boost
#define iPhotonRingColorTempBoost    2.0  // 光子环色温增益 (蓝移) / Photon Ring Color Temp Boost (Blue Shift)
#define iBoostRot                    0.75 // 旋转导致的亮度非对称程度 / Brightness Asymmetry Boost for Spin

#define iJetRedShiftIntensityExponent 2.0 // 喷流频移亮度指数 / Jet Redshift Intensity Exponent
#define iJetBrightmut           1.0       // 喷流亮度乘数 / Jet Brightness Multiplier
#define iJetSaturation          0.0       // 喷流饱和度 / Jet Saturation
#define iJetShiftMax            3.0       // 喷流最大蓝移限制 / Jet Max Blue Shift

#define iBlendWeight            0.5       // TAA 混合权重 / TAA Blend Weight
#define iCameraVelocity         vec3(0.0) // 相机坐标速度 / Camera Coordinate Velocity

#define HAZE_STRENGTH           0.3     // 热浪折射强度 / Heat Haze Refraction Strength
#define HAZE_SCALE              5.2     // 噪声频率 / Heat Haze Noise Scale
#define HAZE_DENSITY_THRESHOLD  0.1     // 密度阈值 / Heat Haze Density Threshold
#define HAZE_LAYER_THICKNESS    0.8     // 层厚度范围 / Heat Haze Layer Thickness
#define HAZE_RADIAL_EXPAND      0.8     // 径向范围倍数 / Heat Haze Radial Expansion
#define HAZE_ROT_SPEED          0.1     // 吸积盘热气旋转速度 / Disk Hot Gas Rotation Speed
#define HAZE_FLOW_SPEED         0.15    // 喷流流速系数 / Jet Flow Speed Coefficient
#define HAZE_PROBE_STEPS        12      // 探测步数 / Heat Haze Probe Steps
#define HAZE_STEP_SIZE          0.06    // 步长 (Rg) / Heat Haze Step Size (Rg)
#define HAZE_DEBUG_MASK         0       // 开启热气遮罩调试 / Enable Heat Gas Mask Debug
#define HAZE_DEBUG_VECTOR       0       // 开启力场向量调试 / Enable Force Field Vector Debug
#define HAZE_DISK_DENSITY_REF   (iBrightmut * 30.0) // 盘密度参考值 / Disk Density Reference
#define HAZE_JET_DENSITY_REF    (iJetBrightmut * 1.0) // 喷流密度参考值 / Jet Density Reference
#define SHADOW_SIZE_MULTIPLIER  0.995   // 阴影半径微调系数 / Multiplier for Shadow Radius

vec3 FragUvToDir(vec2 FragUv, float Fov, vec2 NdcResolution) {
    return normalize(vec3(Fov * (2.0 * FragUv.x - 1.0),
                          Fov * (2.0 * FragUv.y - 1.0) * NdcResolution.y / NdcResolution.x,
                          -1.0));
}

const float kPi          = 3.1415926535897932384626433832795;
const float k2Pi         = 6.283185307179586476925286766559;
const float CONST_M      = 0.5; // [PHYS] Mass M = 0.5, DONT CHANGE THIS
const float EPSILON      = 1e-6;

// =============================================================================
// SECTION 2: 基础工具函数 (噪声、插值、随机、背景)
// =============================================================================

float det3(vec3 a, vec3 b, vec3 c) {
    return dot(a, cross(b, c));
}
float RandomStep(vec2 Input, float Seed) {
    return fract(sin(dot(Input + fract(11.4514 * sin(Seed)), vec2(12.9898, 78.233))) * 43758.5453);
}
float CubicInterpolate(float x) {
    return 3.0 * pow(x, 2.0) - 2.0 * pow(x, 3.0);
}
float PerlinNoise(vec3 Position) {
    vec3 PosInt   = floor(Position); vec3 PosFloat = fract(Position);
    float Sx = CubicInterpolate(PosFloat.x); float Sy = CubicInterpolate(PosFloat.y); float Sz = CubicInterpolate(PosFloat.z);
    float v000 = 2.0 * fract(sin(dot(vec3(PosInt.x,       PosInt.y,       PosInt.z),       vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    float v100 = 2.0 * fract(sin(dot(vec3(PosInt.x + 1.0, PosInt.y,       PosInt.z),       vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    float v010 = 2.0 * fract(sin(dot(vec3(PosInt.x,       PosInt.y + 1.0, PosInt.z),       vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    float v110 = 2.0 * fract(sin(dot(vec3(PosInt.x + 1.0, PosInt.y + 1.0, PosInt.z),       vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    float v001 = 2.0 * fract(sin(dot(vec3(PosInt.x,       PosInt.y,       PosInt.z + 1.0), vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    float v101 = 2.0 * fract(sin(dot(vec3(PosInt.x + 1.0, PosInt.y,       PosInt.z + 1.0), vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    float v011 = 2.0 * fract(sin(dot(vec3(PosInt.x,       PosInt.y + 1.0, PosInt.z + 1.0), vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    float v111 = 2.0 * fract(sin(dot(vec3(PosInt.x + 1.0, PosInt.y + 1.0, PosInt.z + 1.0), vec3(12.9898, 78.233, 213.765))) * 43758.5453) - 1.0;
    return mix(mix(mix(v000, v100, Sx), mix(v010, v110, Sx), Sy), mix(mix(v001, v101, Sx), mix(v011, v111, Sx), Sy), Sz);
}
float SoftSaturate(float x) { return 1.0 - 1.0 / (max(x, 0.0) + 1.0); }
float PerlinNoise1D(float Position) {
    float PosInt   = floor(Position); float PosFloat = fract(Position);
    float v0 = 2.0 * fract(sin(PosInt * 12.9898) * 43758.5453) - 1.0;
    float v1 = 2.0 * fract(sin((PosInt + 1.0) * 12.9898) * 43758.5453) - 1.0;
    return v1 * CubicInterpolate(PosFloat) + v0 * CubicInterpolate(1.0 - PosFloat);
}
float GenerateAccretionDiskNoise(vec3 Position, float NoiseStartLevel, float NoiseEndLevel, float ContrastLevel) {
    float NoiseAccumulator = 10.0;
    int iStart = int(floor(NoiseStartLevel)); int iEnd = int(ceil(NoiseEndLevel));
    int maxIterations = iEnd - iStart;
    for (int delta = 0; delta < maxIterations; delta++) {
        int i = iStart + delta; float iFloat = float(i);
        float w = max(0.0, min(NoiseEndLevel, iFloat + 1.0) - max(NoiseStartLevel, iFloat));
        if (w <= 0.0) continue;
        float NoiseFrequency = pow(3.0, iFloat);
        float noise = PerlinNoise(NoiseFrequency * Position);
        NoiseAccumulator *= (1.0 + 0.1 * noise * w);
    }
    return log(1.0 + pow(0.1 * NoiseAccumulator, ContrastLevel));
}
float Vec2ToTheta(vec2 v1, vec2 v2) {
    float VecDot = dot(v1, v2); float VecCross = v1.x * v2.y - v1.y * v2.x;
    float Angle = asin(0.999999 * VecCross / (length(v1) * length(v2)));
    float Dx = step(0.0, VecDot); float Cx = step(0.0, VecCross);
    return mix(mix(-kPi - Angle, kPi - Angle, Cx), Angle, Dx);
}
float Shape(float x, float Alpha, float Beta) {
    float k = pow(Alpha + Beta, Alpha + Beta) / (pow(Alpha, Alpha) * pow(Beta, Beta));
    return k * pow(x, Alpha) * pow(1.0 - x, Beta);
}

// Shadertoy 背景生成保留
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
const float XYCELL_SIZE = 1.2; const float ZCELL_SIZE = 6.0; const int BLOCK_SIZE = 10; const int BLOCK_GAP = 2; const int ITERATIONS = 40;
float hash(float v) { return fract(sin(v)*43758.5453123); }
float hash(vec2 v) { return hash(dot(v, vec2(5.3983, 5.4427))); }
vec2 hash2(vec2 v) { v = vec2(v * mat2(127.1, 311.7,  269.5, 183.3)); return fract(sin(v)*43758.5453123); }
vec4 hash4(vec2 v) { vec4 p = vec4(v * mat4x2( 127.1, 311.7, 269.5, 183.3, 113.5, 271.9, 246.1, 124.6 )); return fract(sin(p)*43758.5453123); }
vec4 hash4(vec3 v) { vec4 p = vec4(v * mat4x3( 127.1, 311.7, 74.7, 269.5, 183.3, 246.1, 113.5, 271.9, 124.6, 271.9, 269.5, 311.7 ) ); return fract(sin(p)*43758.5453123); }
float rune_line(vec2 p, vec2 a, vec2 b) { p -= a, b -= a; float h = clamp(dot(p, b) / dot(b, b), 0., 1.); return length(p - b * h); }
float rune(vec2 U, vec2 seed, float highlight) { float d = 1e5; for (int i = 0; i < 4; i++) { vec4 pos = hash4(seed); seed += 1.; if (i == 0) pos.y = .0; if (i == 1) pos.x = .999; if (i == 2) pos.x = .0; if (i == 3) pos.y = .999; vec4 snaps = vec4(2, 3, 2, 3); pos = ( floor(pos * snaps) + .5) / snaps; if (pos.xy != pos.zw) d = min(d, rune_line(U, pos.xy, pos.zw + .001) ); } return smoothstep(0.1, 0., d) + highlight*smoothstep(0.4, 0., d); }
float random_char(vec2 outer, vec2 inner, float highlight) { vec2 seed = vec2(dot(outer, vec2(269.5, 183.3)), dot(outer, vec2(113.5, 271.9))); return rune(inner, seed, highlight); }
vec3 rain(vec3 ro3, vec3 rd3, float time) { vec4 result = vec4(0.); vec2 ro2 = vec2(ro3); vec2 rd2 = normalize(vec2(rd3)); bool prefer_dx = abs(rd2.x) > abs(rd2.y); float t3_to_t2 = prefer_dx ? rd3.x / rd2.x : rd3.y / rd2.y; ivec3 cell_side = ivec3(step(0., rd3)); ivec3 cell_shift = ivec3(sign(rd3)); float t2 = 0.; ivec2 next_cell = ivec2(floor(ro2/XYCELL_SIZE)); for (int i=0; i<ITERATIONS; i++) { ivec2 cell = next_cell; float t2s = t2; vec2 side = vec2(next_cell + cell_side.xy) * XYCELL_SIZE; vec2 t2_side = (side - ro2) / rd2; if (t2_side.x < t2_side.y) { t2 = t2_side.x; next_cell.x += cell_shift.x; } else { t2 = t2_side.y; next_cell.y += cell_shift.y; } vec2 cell_in_block = fract(vec2(cell) / float(BLOCK_SIZE)); float gap = float(BLOCK_GAP) / float(BLOCK_SIZE); if (cell_in_block.x < gap || cell_in_block.y < gap || (cell_in_block.x < (gap+0.1) && cell_in_block.y < (gap+0.1))) { continue; } float t3s = t2s / t3_to_t2; float pos_z = ro3.z + rd3.z * t3s; float xycell_hash = hash(vec2(cell)); float z_shift = xycell_hash*11. - time * (0.5 + xycell_hash * 1.0 + xycell_hash * xycell_hash * 1.0 + pow(xycell_hash, 16.) * 3.0); float char_z_shift = floor(z_shift / 0.15); z_shift = char_z_shift * 0.15; int zcell = int(floor((pos_z - z_shift)/ZCELL_SIZE)); for (int j=0; j<2; j++) { vec4 cell_hash = hash4(vec3(ivec3(cell, zcell))); vec4 cell_hash2 = fract(cell_hash * vec4(127.1, 311.7, 271.9, 124.6)); float chars_count = cell_hash.w * 33. + 7.; float target_length = chars_count * 0.15; float target_rad = 0.05; float target_z = (float(zcell)*ZCELL_SIZE + z_shift) + cell_hash.z * (ZCELL_SIZE - target_length); vec2 target = vec2(cell) * XYCELL_SIZE + target_rad + cell_hash.xy * (XYCELL_SIZE - target_rad*2.); vec2 s = target - ro2; float tmin = dot(s, rd2); if (tmin >= t2s && tmin <= t2) { float u = s.x * rd2.y - s.y * rd2.x; if (abs(u) < target_rad) { u = (u/target_rad + 1.) / 2.; float z = ro3.z + rd3.z * tmin/t3_to_t2; float v = (z - target_z) / target_length; if (v >= 0.0 && v < 1.0) { float c = floor(v * chars_count); float q = fract(v * chars_count); vec2 char_hash = hash2(vec2(c+char_z_shift, cell_hash2.x)); if (char_hash.x >= 0.1 || c == 0.) { float time_factor = floor(c == 0. ? time*5.0 : time*(1.0*cell_hash2.z + cell_hash2.w*cell_hash2.w*4.*pow(char_hash.y, 4.))); float a = random_char(vec2(char_hash.x, time_factor), vec2(u,q), max(1., 3. - c/2.)*0.2); a *= clamp((chars_count - 0.5 - c) / 2., 0., 1.); if (a > 0.) { float attenuation = 1. + pow(0.06*tmin/t3_to_t2, 2.); vec3 col = (c == 0. ? vec3(0.67, 1.0, 0.82) : vec3(0.25, 0.80, 0.40)) / attenuation; float a1 = result.a; result.a = a1 + (1. - a1) * a; result.xyz = (result.xyz * a1 + col * (1. - a1) * a) / result.a; if (result.a > 0.98) return result.xyz; } } } } } zcell += cell_shift.z; } } return result.xyz * result.a; }

vec3 KelvinToRgb(float Kelvin) {
    if (Kelvin < 400.01) return vec3(0.0);
    float Teff = (Kelvin - 6500.0) / (6500.0 * Kelvin * 2.2);
    vec3 RgbColor = vec3(exp(2.05539304e4 * Teff), exp(2.63463675e4 * Teff), exp(3.30145739e4 * Teff));
    float BrightnessScale = 1.0 / max(max(1.5 * RgbColor.r, RgbColor.g), RgbColor.b);
    if (Kelvin < 1000.0) BrightnessScale *= (Kelvin - 400.0) / 600.0;
    return RgbColor * BrightnessScale;
}

vec3 WavelengthToRgb(float wavelength) {
    vec3 color = vec3(0.0);
    if (wavelength <= 380.0 ) { color.r = 1.0; color.g = 0.0; color.b = 1.0; } 
    else if (wavelength < 440.0) { color.r = -(wavelength - 440.0) / 60.0; color.g = 0.0; color.b = 1.0; } 
    else if (wavelength < 490.0) { color.r = 0.0; color.g = (wavelength - 440.0) / 50.0; color.b = 1.0; } 
    else if (wavelength < 510.0) { color.r = 0.0; color.g = 1.0; color.b = -(wavelength - 510.0) / 20.0; } 
    else if (wavelength < 580.0) { color.r = (wavelength - 510.0) / 70.0; color.g = 1.0; color.b = 0.0; } 
    else if (wavelength < 645.0) { color.r = 1.0; color.g = -(wavelength - 645.0) / 65.0; color.b = 0.0; } 
    else { color.r = 1.0; color.g = 0.0; color.b = 0.0; }
    
    float factor = 0.3;
    if (wavelength >= 380.0 && wavelength < 420.0) factor = 0.3 + 0.7 * (wavelength - 380.0) / 40.0;
    else if (wavelength >= 420.0 && wavelength < 645.0) factor = 1.0;
    else if (wavelength >= 645.0 && wavelength <= 750.0) factor = 0.3 + 0.7 * (750.0 - wavelength) / 105.0;
    return color * factor / pow(color.r * color.r + 2.25 * color.g * color.g + 0.36 * color.b * color.b, 0.5) * (0.1 * (color.r + color.g + color.b) + 0.9);
}

vec4 SampleBackground(vec3 Dir, float Shift, float Status) {
    float rStatus = round(Status);
    bool isNegativeMass = (iBlackHoleMassSol < 0.0);
    bool isAntiverse    = (mod(rStatus, 3.0) == 2.0); 

    vec4 Backcolor;
    if (isNegativeMass != isAntiverse) {
        Backcolor = vec4(rain(vec3(0.0), Dir, iTime+1.0), 1.0);
    } else {
        Backcolor = vec4(stars(Dir), 1.0);
    }

    vec3 Rcolor = Backcolor.r * 1.0 * WavelengthToRgb(max(453.0, 645.0 / Shift));
    vec3 Gcolor = Backcolor.g * 1.5 * WavelengthToRgb(max(416.0, 510.0 / Shift));
    vec3 Bcolor = Backcolor.b * 0.6 * WavelengthToRgb(max(380.0, 440.0 / Shift));
    vec3 Scolor = Rcolor + Gcolor + Bcolor;
    float OStrength = 0.3 * Backcolor.r + 0.6 * Backcolor.g + 0.1 * Backcolor.b;
    float RStrength = 0.3 * Scolor.r + 0.6 * Scolor.g + 0.1 * Scolor.b;
    Scolor *= OStrength / max(RStrength, 0.001);
    
    return iBackgroundBrightmut * vec4(Scolor, Backcolor.a) * pow(Shift, 4.0);
}

vec4 ApplyToneMapping(vec4 Result,float shift) {
    float RedFactor   = 3.0 * Result.r / (Result.r + Result.b + Result.g + 1e-6);
    float BlueFactor  = 3.0 * Result.b / (Result.r + Result.b + Result.g + 1e-6);
    float GreenFactor = 3.0 * Result.g / (Result.r + Result.b + Result.g + 1e-6);
    float BloomMax    = max(8.0, shift) + log(max(shift - 8.0 + 1.0, 1.0));
    vec4 Mapped;
    Mapped.r = min(-4.0 * log(1.0 - pow(clamp(Result.r, 0.0, 0.999), 2.2)), BloomMax * RedFactor);
    Mapped.g = min(-4.0 * log(1.0 - pow(clamp(Result.g, 0.0, 0.999), 2.2)), BloomMax * GreenFactor);
    Mapped.b = min(-4.0 * log(1.0 - pow(clamp(Result.b, 0.0, 0.999), 2.2)), BloomMax * BlueFactor);
    Mapped.a = min(-4.0 * log(1.0 - pow(clamp(Result.a, 0.0, 0.999), 2.2)), 4.0);
    return Mapped;
}

// =============================================================================
// SECTION 4: 广相计算。Y为自旋方向，ins/outgoing方向笛卡尔形式kerrscild系。+++-。
// =============================================================================

float GetKeplerianAngularVelocity(float Radius, float Rs, float PhysicalSpinA, float PhysicalQ) {
    float M = 0.5 * Rs; 
    float Mr_minus_Q2 = M * Radius - PhysicalQ * PhysicalQ;
    if (Mr_minus_Q2 < 0.0) return 0.0;
    float sqrt_Term = sqrt(Mr_minus_Q2);
    float denominator = Radius * Radius + PhysicalSpinA * sqrt_Term;
    return sqrt_Term / max(EPSILON, denominator);
}

float KerrSchildRadius(vec3 p, float PhysicalSpinA, float r_sign) {
    float r_sign_len = r_sign * length(p);
    if (PhysicalSpinA == 0.0) return r_sign_len; 
    float a2 = PhysicalSpinA * PhysicalSpinA;
    float rho2 = dot(p.xz, p.xz);
    float y2 = p.y * p.y;
    float b = rho2 + y2 - a2;
    float det = sqrt(b * b + 4.0 * a2 * y2);
    float r2;
    if (b >= 0.0) r2 = 0.5 * (b + det);
    else r2 = (2.0 * a2 * y2) / max(1e-20, det - b);
    return r_sign * sqrt(r2);
}

float GetZamoOmega(float r, float a, float Q, float y) {
    float r2 = r * r; float a2 = a * a; float y2 = y * y;
    float cos2 = min(1.0, y2 / (r2 + 1e-9)); float sin2 = 1.0 - cos2;
    float Delta = r2 - r + a2 + Q * Q;
    float Sigma = r2 + a2 * cos2;
    float A_metric = (r2 + a2) * (r2 + a2) - Delta * a2 * sin2;
    return a * (r - Q * Q) / max(1e-9, A_metric);
}

vec2 IntersectKerrEllipsoid(vec3 O, vec3 D, float r, float a) {
    float r2 = r * r; float a2 = a * a;
    float A = r2 + a2; float B = r2;
    float qa = B * (D.x * D.x + D.z * D.z) + A * D.y * D.y;
    float qb = 2.0 * (B * (O.x * D.x + O.z * D.z) + A * O.y * D.y);
    float qc = B * (O.x * O.x + O.z * O.z) + A * O.y * O.y - A * B;
    if (abs(qa) < 1e-9) return vec2(-1.0);
    float disc = qb * qb - 4.0 * qa * qc;
    if (disc < 0.0) return vec2(-1.0);
    float sqrtDisc = sqrt(disc);
    return vec2((-qb - sqrtDisc) / (2.0 * qa), (-qb + sqrtDisc) / (2.0 * qa));
}

struct KerrGeometry {
    float r; float r2; float a2; float f;              
    vec3 grad_r; vec3 grad_f;         
    vec4 l_up; vec4 l_down;
    float inv_r2_a2; float inv_den_f; float num_f;          
};

void ComputeGeometryScalars(vec3 X, float PhysicalSpinA, float PhysicalQ, float fade, float r_sign, bool isOutgoing, out KerrGeometry geo) {
    geo.a2 = PhysicalSpinA * PhysicalSpinA;
    float dirSign = isOutgoing ? -1.0 : 1.0;
    if (PhysicalSpinA == 0.0) {
        geo.r = r_sign * length(X);
        geo.r2 = geo.r * geo.r;
        float inv_r = 1.0 / geo.r;
        float inv_r2 = inv_r * inv_r;
        geo.l_up = vec4(dirSign * X * inv_r, -1.0);
        geo.l_down = vec4(dirSign * X * inv_r, 1.0);
        geo.num_f = (2.0 * CONST_M * geo.r - PhysicalQ * PhysicalQ);
        geo.f = (2.0 * CONST_M * inv_r - (PhysicalQ * PhysicalQ) * inv_r2) * fade;
        geo.inv_r2_a2 = inv_r2; 
        geo.inv_den_f = inv_r2 * inv_r2; 
        return;
    }
    geo.r = KerrSchildRadius(X, PhysicalSpinA, r_sign);
    geo.r2 = geo.r * geo.r;
    float r3 = geo.r2 * geo.r;
    float z_coord = X.y; float z2 = z_coord * z_coord;
    geo.inv_r2_a2 = 1.0 / (geo.r2 + geo.a2);
    
    float lx = (dirSign * geo.r * X.x - PhysicalSpinA * X.z) * geo.inv_r2_a2;
    float ly = (dirSign * X.y) / geo.r;
    float lz = (dirSign * geo.r * X.z + PhysicalSpinA * X.x) * geo.inv_r2_a2;
    
    geo.l_up = vec4(lx, ly, lz, -1.0);
    geo.l_down = vec4(lx, ly, lz, 1.0); 
    geo.num_f = 2.0 * CONST_M * r3 - PhysicalQ * PhysicalQ * geo.r2;
    float den_f = geo.r2 * geo.r2 + geo.a2 * z2;
    geo.inv_den_f = 1.0 / max(1e-20, den_f);
    geo.f = (geo.num_f * geo.inv_den_f) * fade;
}

void ComputeGeometryGradients(vec3 X, float PhysicalSpinA, float PhysicalQ, float fade, inout KerrGeometry geo) {
    if (PhysicalSpinA == 0.0) {
        float inv_r = 1.0 / geo.r; float inv_r2 = inv_r * inv_r;
        geo.grad_r = X * inv_r;
        float df_dr = (-2.0 * CONST_M + 2.0 * PhysicalQ * PhysicalQ * inv_r) * inv_r2 * fade;
        geo.grad_f = df_dr * geo.grad_r;
        return;
    }
    float inv_denom_grad = geo.r * geo.inv_den_f;
    geo.grad_r = vec3(X.x * geo.r2, X.y * (geo.r2 + geo.a2), X.z * geo.r2) * inv_denom_grad;
    float z_coord = X.y; float z2 = z_coord * z_coord;
    float term_M  = -2.0 * CONST_M * geo.r2 * geo.r2 * geo.r;
    float term_Q  = 2.0 * PhysicalQ * PhysicalQ * geo.r2 * geo.r2;
    float term_Ma = 6.0 * CONST_M * geo.a2 * geo.r * z2;
    float term_Qa = -2.0 * PhysicalQ * PhysicalQ * geo.a2 * z2;
    float df_dr_num_reduced = term_M + term_Q + term_Ma + term_Qa;
    float df_dr = (geo.r * df_dr_num_reduced) * (geo.inv_den_f * geo.inv_den_f);
    float df_dy = -(geo.num_f * 2.0 * geo.a2 * z_coord) * (geo.inv_den_f * geo.inv_den_f);
    geo.grad_f = df_dr * geo.grad_r; geo.grad_f.y += df_dy; geo.grad_f *= fade;
}

vec4 RaiseIndex(vec4 P_cov, KerrGeometry geo) {
    vec4 P_flat = vec4(P_cov.xyz, -P_cov.w); 
    float L_dot_P = dot(geo.l_up, P_cov);
    return P_flat - geo.f * L_dot_P * geo.l_up;
}
vec4 LowerIndex(vec4 P_contra, KerrGeometry geo) {
    vec4 P_flat = vec4(P_contra.xyz, -P_contra.w);
    float L_dot_P = dot(geo.l_down, P_contra);
    return P_flat + geo.f * L_dot_P * geo.l_down;
}

vec4 GetInitialMomentum(vec3 RayDir, vec4 X, int obsMode, float universesign, float PhysicalSpinA, float PhysicalQ, float GravityFade, bool isOutgoing) {
    KerrGeometry geo;
    ComputeGeometryScalars(X.xyz, PhysicalSpinA, PhysicalQ, GravityFade, universesign, isOutgoing, geo);
    vec4 U_up;
    float g_tt = -1.0 + geo.f;
    float time_comp = 1.0 / sqrt(max(1e-9, -g_tt));
    U_up = vec4(0.0, 0.0, 0.0, time_comp);
    if (obsMode == 1) {
        float r = geo.r; float r2 = geo.r2; float a = PhysicalSpinA; float a2 = geo.a2; float y_phys = X.y; 
        float rho2 = r2 + a2 * (y_phys * y_phys) / (r2 + 1e-9);
        float Q2 = PhysicalQ * PhysicalQ;
        float MassChargeTerm = 2.0 * CONST_M * r - Q2;
        float Xi = sqrt(max(0.0, MassChargeTerm * (r2 + a2)));
        float DenomPhi = rho2 * (MassChargeTerm + Xi);
        float U_phi_KS = (abs(DenomPhi) > 1e-9) ? (-MassChargeTerm * a / DenomPhi) : 0.0;
        float U_r_KS = -Xi / max(1e-9, rho2);
        float inv_r2_a2 = 1.0 / (r2 + a2);
        float Ux_rad = (r * X.x - a * X.z) * inv_r2_a2 * U_r_KS;
        float Uz_rad = (r * X.z + a * X.x) * inv_r2_a2 * U_r_KS;
        float Uy_rad = (X.y / r) * U_r_KS;
        float Ux_tan =  X.z * U_phi_KS;
        float Uz_tan = -X.x * U_phi_KS;
        vec3 U_spatial = vec3(Ux_rad + Ux_tan, Uy_rad, Uz_rad + Uz_tan);
        float l_dot_u_spatial = dot(geo.l_down.xyz, U_spatial);
        float U_spatial_sq = dot(U_spatial, U_spatial);
        float A = -1.0 + geo.f; float B = 2.0 * geo.f * l_dot_u_spatial;
        float C = U_spatial_sq + geo.f * (l_dot_u_spatial * l_dot_u_spatial) + 1.0; 
        float Det = max(0.0, B*B - 4.0 * A * C); float sqrtDet = sqrt(Det);
        float Ut = (abs(A) < 1e-7) ? (-C / max(1e-19, B)) : ((B < 0.0) ? (2.0 * C / (-B + sqrtDet)) : ((-B - sqrtDet) / (2.0 * A)));
        U_up = mix(vec4(0.0, 0.0, 0.0, time_comp), vec4(U_spatial, Ut), GravityFade);
    } else if (obsMode == 2 || obsMode == 3) {
        vec3 v_in = (obsMode == 2) ? iCameraVelocity : -iCameraVelocity;
        if (any(isnan(v_in)) || any(isinf(v_in))) v_in = vec3(0.0);
        vec4 V_up = vec4(v_in, 1.0); vec4 V_down = LowerIndex(V_up, geo);
        float V_sq = dot(V_up, V_down);
        if (V_sq < 0.0) U_up = V_up * inversesqrt(-V_sq);
        else return vec4(114514.0);
    }
       
    vec4 U_down = LowerIndex(U_up, geo);
    vec3 m_r = -normalize(X.xyz);
    vec3 WorldUp = vec3(0.0, 1.0, 0.0);
    if (abs(dot(m_r, WorldUp)) > 0.999) WorldUp = vec3(1.0, 0.0, 0.0);
    vec3 m_phi = normalize(cross(WorldUp, m_r)); 
    vec3 m_theta = cross(m_phi, m_r); 
    float k_r = dot(RayDir, m_r); float k_theta = dot(RayDir, m_theta); float k_phi = dot(RayDir, m_phi);

vec4 e1 = vec4(m_r, 0.0);
    e1 += dot(e1, U_down) * U_up; 
    vec4 e1_d = LowerIndex(e1, geo);
    float n1 = sqrt(max(1e-9, dot(e1, e1_d)));
    e1 /= n1; e1_d /= n1;

    vec4 e2 = vec4(m_theta, 0.0);
    e2 += dot(e2, U_down) * U_up;
    e2 -= dot(e2, e1_d) * e1;
    vec4 e2_d = LowerIndex(e2, geo);
    float n2 = sqrt(max(1e-9, dot(e2, e2_d)));
    e2 /= n2; e2_d /= n2;

    vec4 e3 = vec4(m_phi, 0.0);
    e3 += dot(e3, U_down) * U_up;
    e3 -= dot(e3, e1_d) * e1;
    e3 -= dot(e3, e2_d) * e2;
    vec4 e3_d = LowerIndex(e3, geo);
    float n3 = sqrt(max(1e-9, dot(e3, e3_d)));
    e3 /= n3;
    vec4 P_up = U_up - (k_r * e1 + k_theta * e2 + k_phi * e3);
    return LowerIndex(P_up, geo);
}

vec3 DebugInitialMomentum(vec4 P_cov, vec4 X, int obsMode, float universesign, float PhysicalSpinA, float PhysicalQ, float GravityFade, bool isOutgoing, vec3 CameraVelocity) {
    if (P_cov == vec4(114514.0)) return vec3(0.0);
    KerrGeometry geo; ComputeGeometryScalars(X.xyz, PhysicalSpinA, PhysicalQ, GravityFade, universesign, isOutgoing, geo);
    vec4 P_up = RaiseIndex(P_cov, geo); float norm_sq = dot(P_cov, P_up);
    
    vec4 U_up; float g_tt = -1.0 + geo.f; float time_comp = 1.0 / sqrt(max(1e-9, -g_tt)); U_up = vec4(0.0, 0.0, 0.0, time_comp);
    if (obsMode == 1) {
        float r = geo.r; float r2 = geo.r2; float a = PhysicalSpinA; float a2 = geo.a2; float y_phys = X.y; 
        float rho2 = r2 + a2 * (y_phys * y_phys) / (r2 + 1e-9); float Q2 = PhysicalQ * PhysicalQ;
        float MassChargeTerm = 2.0 * CONST_M * r - Q2; float Xi = sqrt(max(0.0, MassChargeTerm * (r2 + a2))); float DenomPhi = rho2 * (MassChargeTerm + Xi);
        float U_phi_KS = (abs(DenomPhi) > 1e-9) ? (-MassChargeTerm * a / DenomPhi) : 0.0; float U_r_KS = -Xi / max(1e-9, rho2);
        float inv_r2_a2 = 1.0 / (r2 + a2);
        float Ux_rad = (r * X.x - a * X.z) * inv_r2_a2 * U_r_KS; float Uz_rad = (r * X.z + a * X.x) * inv_r2_a2 * U_r_KS; float Uy_rad = (X.y / r) * U_r_KS;
        float Ux_tan =  X.z * U_phi_KS; float Uz_tan = -X.x * U_phi_KS; vec3 U_spatial = vec3(Ux_rad + Ux_tan, Uy_rad, Uz_rad + Uz_tan);
        float l_dot_u_spatial = dot(geo.l_down.xyz, U_spatial); float U_spatial_sq = dot(U_spatial, U_spatial);
        float A = -1.0 + geo.f; float B = 2.0 * geo.f * l_dot_u_spatial; float C = U_spatial_sq + geo.f * (l_dot_u_spatial * l_dot_u_spatial) + 1.0; 
        float Det = max(0.0, B*B - 4.0 * A * C); float Ut = (abs(A) < 1e-7) ? (-C / max(1e-19, B)) : ((B < 0.0) ? (2.0 * C / (-B + sqrt(Det))) : ((-B - sqrt(Det)) / (2.0 * A)));
        U_up = mix(vec4(0.0, 0.0, 0.0, time_comp), vec4(U_spatial, Ut), GravityFade);
    } else if (obsMode == 2) {
        vec3 v_in = CameraVelocity; if (any(isnan(v_in)) || any(isinf(v_in))) v_in = vec3(0.0);
        vec4 V_up = vec4(v_in, 1.0); vec4 V_down = LowerIndex(V_up, geo);
        if (dot(V_up, V_down) < 0.0) U_up = V_up * inversesqrt(-dot(V_up, V_down));
    }
    vec4 U_down = LowerIndex(U_up, geo);
    vec3 m_r = -normalize(X.xyz); vec3 WorldUp = vec3(0.0, 1.0, 0.0); if (abs(dot(m_r, WorldUp)) > 0.999) WorldUp = vec3(1.0, 0.0, 0.0);
    vec3 m_phi = normalize(cross(WorldUp, m_r)); vec3 m_theta = cross(m_phi, m_r); 
    vec4 e1 = vec4(m_r, 0.0); e1 += dot(e1, U_down) * U_up; vec4 e1_d = LowerIndex(e1, geo); e1 /= sqrt(max(1e-9, dot(e1, e1_d))); e1_d /= sqrt(max(1e-9, dot(e1, e1_d)));
    vec4 e2 = vec4(m_theta, 0.0); e2 += dot(e2, U_down) * U_up; e2 -= dot(e2, e1_d) * e1; vec4 e2_d = LowerIndex(e2, geo); e2 /= sqrt(max(1e-9, dot(e2, e2_d))); e2_d /= sqrt(max(1e-9, dot(e2, e2_d)));
    vec4 e3 = vec4(m_phi, 0.0); e3 += dot(e3, U_down) * U_up; e3 -= dot(e3, e1_d) * e1; e3 -= dot(e3, e2_d) * e2; vec4 e3_d = LowerIndex(e3, geo); e3 /= sqrt(max(1e-9, dot(e3, e3_d)));

    vec3 p_local = vec3(dot(P_cov, e1), dot(P_cov, e2), dot(P_cov, e3)); vec3 local_dir = normalize(p_local); 
    float valid_r = (abs(norm_sq) < 1e-4) ? 1.0 : 0.0;
    return vec3(valid_r, local_dir.x * 0.5 + 0.5, local_dir.y * 0.5 + 0.5);
}

void transformKerrSchild_YSpin(inout vec4 X, in float r_sign, inout vec4 P, float M, float a, float Q, bool out_to_in) {
    float x = X.x, y = X.y, z = X.z, t = X.w;
    float px = P.x, py = P.y, pz = P.z, pt = P.w;
    float a2 = a * a; float M2 = M * M; float Q2 = Q * Q;

    float R2 = x*x + y*y + z*z; float u = R2 - a2; float v = 4.0 * a2 * y * y; 
    float r2; if (u >= 0.0) r2 = 0.5 * (u + sqrt(u*u + v)); else r2 = 0.5 * v / max(1e-20, sqrt(u*u + v) - u);
    float r = r_sign * sqrt(max(r2, 0.0));

    float Delta = r*r - 2.0*M*r + a2 + Q2;
    float safe_Delta = sign(Delta) * max(abs(Delta), 1e-16); if (safe_Delta == 0.0) safe_Delta = 1e-16;

    float r3 = r * r * r; float D = r3 * r + a2 * y * y; float safe_D = max(D, 1e-12);

    vec3 grad_r = vec3(r3 * x / safe_D, r * (r*r + a2) * y / safe_D, r3 * z / safe_D);

    float delta_disc = M2 - a2 - Q2; float F_r = 0.0; float g_r = 0.0; float abs_Delta_safe = max(abs(Delta), 1e-16);

    if (delta_disc > 1e-16) {
        float K = sqrt(delta_disc); float r_plus = M + K; float r_minus = M - K;
        float frac = abs(r - r_plus) / max(abs(r - r_minus), 1e-16);
        F_r = 2.0 * M * log(abs_Delta_safe) + ((2.0 * M2 - Q2) / K) * log(max(frac, 1e-16));
        g_r = (a / K) * log(max(frac, 1e-16));
    } else if (delta_disc < -1e-16) {
        float K = sqrt(-delta_disc); float atan_arg = atan((r - M) / K);
        F_r = 2.0 * M * log(abs_Delta_safe) + (2.0 * (2.0 * M2 - Q2) / K) * atan_arg;
        g_r = (2.0 * a / K) * atan_arg;
    } else {
        float rM = r - M; float safe_rM = sign(rM) * max(abs(rM), 1e-16); if (safe_rM == 0.0) safe_rM = 1e-16;
        F_r = 4.0 * M * log(max(abs(rM), 1e-16)) - 2.0 * (2.0 * M2 - Q2) / safe_rM;
        g_r = -2.0 * a / safe_rM;
    }
    g_r += 2.0 * atan(a, r); 
    
    float F_prime = 2.0 * (2.0 * M * r - Q2) / safe_Delta;
    float g_prime = 2.0 * a / safe_Delta - 2.0 * a / (r * r + a * a);
    float Ly = z * px - x * pz; 
    float K_p = F_prime * pt + g_prime * Ly;
    float dir = out_to_in ? -1.0 : 1.0; 
    
    float angle = -dir * g_r; float time_shift = -dir * F_r;
    vec3 P_tilde = vec3(px, py, pz) + dir * grad_r * K_p;

    float cos_a = cos(angle); float sin_a = sin(angle);
    X.x = x * cos_a + z * sin_a; X.y = y; X.z = z * cos_a - x * sin_a; X.w = t + time_shift;
    P.x = P_tilde.z * sin_a + P_tilde.x * cos_a; P.y = P_tilde.y; P.z = - P_tilde.x * sin_a + P_tilde.z * cos_a; P.w = pt;
}

vec2 GetWalkerPenrose(vec4 X, vec4 P_cov, vec4 F_cov, float Physicala, float PhysicalQ, float r) {
    if (abs(r) < 1e-6) return vec2(0.0);
    float a = Physicala; float Q = PhysicalQ; float a2 = a * a; float r2 = r * r; float r2a2 = r2 + a2; float sqrt_r2a2 = sqrt(r2a2);
    float x = X.x, y = X.y, z = X.z;
    float R2 = x * x + z * z; float R = sqrt(R2);
    float sinTheta = R / sqrt_r2a2; float cosTheta = clamp(y / r, -1.0, 1.0); float sinTheta2 = sinTheta * sinTheta;
    bool onAxis = (R < 1e-12);   
    float Delta = r2 - r + a2 + Q * Q; float Sigma = r2 + a2 * cosTheta * cosTheta;
    float invSigma = 1.0 / max(1e-20, Sigma); float invDelta = 1.0 / max(1e-20, Delta);

    float P_t = P_cov.w; float P_x = P_cov.x, P_y = P_cov.y, P_z = P_cov.z;
    float P_r_BL = (r2a2) * invDelta * P_t + (x * r / r2a2) * P_x + (y / r) * P_y + (z * r / r2a2) * P_z;
    float P_phi_BL = x * P_z - z * P_x;
    float P_theta_BL = onAxis ? 0.0 : (cosTheta * sqrt_r2a2 * ((x * P_x + z * P_z) / R) - r * sinTheta * P_y);

    float f_t = F_cov.w; float f_x = F_cov.x, f_y = F_cov.y, f_z = F_cov.z;
    float f_r_BL = (r2a2) * invDelta * f_t + (x * r / r2a2) * f_x + (y / r) * f_y + (z * r / r2a2) * f_z;
    float f_phi_BL = x * f_z - z * f_x;
    float f_theta_BL = onAxis ? 0.0 : (cosTheta * sqrt_r2a2 * ((x * f_x + z * f_z) / R) - r * sinTheta * f_y);

    float g_tphi = -a * (r - Q * Q) * invSigma * invDelta; float g_tt = -((r2a2) * (r2a2) - Delta * a2 * sinTheta2) * invSigma * invDelta;
    float g_rr = Delta * invSigma; float g_thth = invSigma;

    float P_t_con = g_tt * P_t + g_tphi * P_phi_BL; float P_r_con = g_rr * P_r_BL; float P_theta_con = g_thth * P_theta_BL;
    float f_t_con = g_tt * f_t + g_tphi * f_phi_BL; float f_r_con = g_rr * f_r_BL; float f_theta_con = g_thth * f_theta_BL;

    float P_phi_s1 = 0.0, P_phi_s2 = 0.0; float f_phi_s1 = 0.0, f_phi_s2 = 0.0;
    if (!onAxis) {
        float invR = 1.0 / R; float common_ = (Delta - a2 * sinTheta2) * invSigma * invDelta;
        P_phi_s2 = sinTheta2 * g_tphi * P_t + common_ * P_phi_BL;
        P_phi_s1 = sinTheta  * g_tphi * P_t + common_ * (sqrt_r2a2 * (x * P_z - z * P_x) * invR);
        f_phi_s2 = sinTheta2 * g_tphi * f_t + common_ * f_phi_BL;
        f_phi_s1 = sinTheta  * g_tphi * f_t + common_ * (sqrt_r2a2 * (x * f_z - z * f_x) * invR);
    }

    float tr = P_t_con * f_r_con - P_r_con * f_t_con;
    float ttheta = P_t_con * f_theta_con - P_theta_con * f_t_con;
    float rphi2 = P_r_con * f_phi_s2 - P_phi_s2 * f_r_con;
    float thetaphi1 = P_theta_con * f_phi_s1 - P_phi_s1 * f_theta_con;

    float K_re = r * tr + a2 * cosTheta * sinTheta * ttheta + a * r * rphi2 + a * (r2a2) * cosTheta * thetaphi1;
    float K_im = a * cosTheta * tr - a * r * sinTheta * ttheta + a2 * cosTheta * rphi2 - r * (r2a2) * thetaphi1;

    return vec2(K_re, K_im);
}

vec2 SolvePolarization(vec2 K_photon, vec2 K_right, vec2 K_up) {
    float det = K_right.x * K_up.y - K_right.y * K_up.x;
    if (abs(det) < 1e-25) return vec2(1.0, 0.0); 
    float inv_det = 1.0 / det;
    float alpha = ( K_up.y * K_photon.x - K_up.x * K_photon.y) * inv_det;
    float beta  = (-K_right.y * K_photon.x + K_right.x * K_photon.y) * inv_det;
    vec2 result = vec2(alpha, beta);
    float mag = length(result);
    return (mag > 1e-19) ? (result / mag) : vec2(1.0, 0.0);
}

// =============================================================================
// 5.积分器
// =============================================================================
struct State { vec4 X; vec4 P; };

void ApplyHamiltonianCorrection(inout vec4 P, vec4 X, float E, float PhysicalSpinA, float PhysicalQ, float fade, float r_sign, bool isOutgoing) {
    P.w = -E; vec3 p = P.xyz;    
    KerrGeometry geo; ComputeGeometryScalars(X.xyz, PhysicalSpinA, PhysicalQ, fade, r_sign, isOutgoing, geo);
    float L_dot_p_s = dot(geo.l_up.xyz, p); float Pt = P.w; 
    float p2 = dot(p, p);
    float Coeff_A = p2 - geo.f * L_dot_p_s * L_dot_p_s;
    float Coeff_B = 2.0 * geo.f * L_dot_p_s * Pt;
    float Coeff_C = -Pt * Pt * (1.0 + geo.f);
    float disc = Coeff_B * Coeff_B - 4.0 * Coeff_A * Coeff_C;
    if (disc >= 0.0) {
        float sqrtDisc = sqrt(disc); float denom = 2.0 * Coeff_A;
        if (abs(denom) > 1e-9) {
            float k1 = (-Coeff_B + sqrtDisc) / denom; float k2 = (-Coeff_B - sqrtDisc) / denom;
            float k = (abs(k1 - 1.0) < abs(k2 - 1.0)) ? k1 : k2;
            P.xyz *= mix(k, 1.0, clamp(abs(k - 1.0) / 0.1 - 1.0, 0.0, 1.0));
        }
    }
}

State GetDerivativesAnalytic(State S, float PhysicalSpinA, float PhysicalQ, float fade, bool isOutgoing, inout KerrGeometry geo) {
    State deriv;
    ComputeGeometryGradients(S.X.xyz, PhysicalSpinA, PhysicalQ, fade, geo);
    float l_dot_P = dot(geo.l_up.xyz, S.P.xyz) + geo.l_up.w * S.P.w;
    vec4 P_flat = vec4(S.P.xyz, -S.P.w); 
    deriv.X = P_flat - geo.f * l_dot_P * geo.l_up;
    vec3 grad_A = (-2.0 * geo.r * geo.inv_r2_a2) * geo.inv_r2_a2 * geo.grad_r;
    float dirSign = isOutgoing ? -1.0 : 1.0;
    float rx_az = dirSign * geo.r * S.X.x - PhysicalSpinA * S.X.z;
    float rz_ax = dirSign * geo.r * S.X.z + PhysicalSpinA * S.X.x;
    vec3 d_num_lx = dirSign * S.X.x * geo.grad_r; d_num_lx.x += dirSign * geo.r; d_num_lx.z -= PhysicalSpinA;
    vec3 grad_lx = geo.inv_r2_a2 * d_num_lx + rx_az * grad_A;
    vec3 grad_ly = dirSign * (geo.r * geo.inv_den_f) * vec3(-S.X.x * S.X.y, geo.r2 - S.X.y * S.X.y, -S.X.z * S.X.y);
    vec3 d_num_lz = dirSign * S.X.z * geo.grad_r; d_num_lz.z += dirSign * geo.r; d_num_lz.x += PhysicalSpinA;
    vec3 grad_lz = geo.inv_r2_a2 * d_num_lz + rz_ax * grad_A;
    vec3 P_dot_grad_l = S.P.x * grad_lx + S.P.y * grad_ly + S.P.z * grad_lz;
    vec3 Force = 0.5 * ( (l_dot_P * l_dot_P) * geo.grad_f + (2.0 * geo.f * l_dot_P) * P_dot_grad_l );
    deriv.P = vec4(Force, 0.0); 
    return deriv;
}

float GetIntermediateSign(vec4 StartX, vec4 CurrentX, float CurrentSign, float PhysicalSpinA) {
    if (StartX.y * CurrentX.y < 0.0) {
        float t = StartX.y / (StartX.y - CurrentX.y);
        float rho_cross = length(mix(StartX.xz, CurrentX.xz, t));
        if (rho_cross < abs(PhysicalSpinA)) return -CurrentSign;
    }
    return CurrentSign;
}

void StepGeodesicRK4_Optimized(inout vec4 X, inout vec4 P, float E, float dt, float PhysicalSpinA, float PhysicalQ, float fade, float r_sign, bool isOutgoing, KerrGeometry geo0, State k1) {
    State s0; s0.X = X; s0.P = P;
    State s1; s1.X = s0.X + 0.5 * dt * k1.X; s1.P = s0.P + 0.5 * dt * k1.P;
    float sign1 = GetIntermediateSign(s0.X, s1.X, r_sign, PhysicalSpinA);
    KerrGeometry geo1; ComputeGeometryScalars(s1.X.xyz, PhysicalSpinA, PhysicalQ, fade, sign1, isOutgoing, geo1);
    State k2 = GetDerivativesAnalytic(s1, PhysicalSpinA, PhysicalQ, fade, isOutgoing, geo1);
    
    State s2; s2.X = s0.X + 0.5 * dt * k2.X; s2.P = s0.P + 0.5 * dt * k2.P;
    float sign2 = GetIntermediateSign(s0.X, s2.X, r_sign, PhysicalSpinA);
    KerrGeometry geo2; ComputeGeometryScalars(s2.X.xyz, PhysicalSpinA, PhysicalQ, fade, sign2, isOutgoing, geo2);
    State k3 = GetDerivativesAnalytic(s2, PhysicalSpinA, PhysicalQ, fade, isOutgoing, geo2);
    
    State s3; s3.X = s0.X + dt * k3.X; s3.P = s0.P + dt * k3.P;
    float sign3 = GetIntermediateSign(s0.X, s3.X, r_sign, PhysicalSpinA);
    KerrGeometry geo3; ComputeGeometryScalars(s3.X.xyz, PhysicalSpinA, PhysicalQ, fade, sign3, isOutgoing, geo3);
    State k4 = GetDerivativesAnalytic(s3, PhysicalSpinA, PhysicalQ, fade, isOutgoing, geo3);
    
    vec4 finalX = s0.X + (dt / 6.0) * (k1.X + 2.0 * k2.X + 2.0 * k3.X + k4.X);
    vec4 finalP = s0.P + (dt / 6.0) * (k1.P + 2.0 * k2.P + 2.0 * k3.P + k4.P);
    
    float finalSign = GetIntermediateSign(s0.X, finalX, r_sign, PhysicalSpinA);
    if(finalSign > 0.0) ApplyHamiltonianCorrection(finalP, finalX, E, PhysicalSpinA, PhysicalQ, fade, finalSign, isOutgoing);
    X = finalX; P = finalP;
}

// =============================================================================
// SECTION 6: 热折射，吸积盘与喷流,经纬网
// =============================================================================

float HazeNoise01(vec3 p) { return PerlinNoise(p) * 0.5 + 0.5; }
float GetBaseNoise(vec3 p) {
    float baseScale = HAZE_SCALE * 0.4; vec3 pos = p * baseScale;
    const mat3 rotNoise = mat3(0.80, 0.60, 0.00, -0.48, 0.64, 0.60, -0.36, 0.48, -0.80);
    pos = rotNoise * pos;
    return HazeNoise01(pos) * 0.6 + HazeNoise01(pos * 3.0 + vec3(13.5, -2.4, 4.1)) * 0.4; 
}
float GetDiskHazeMask(vec3 pos_Rg, float InterRadius, float OuterRadius, float Thin, float Hopper) {
    float r = length(pos_Rg.xz); float y = abs(pos_Rg.y);
    float boundaryY = max(0.2, (Thin + max(0.0, (r - 3.0) * Hopper)) * HAZE_LAYER_THICKNESS);
    return (1.0 - smoothstep(boundaryY * 0.5, boundaryY * 1.5, y)) * smoothstep(InterRadius * 0.3, InterRadius * 0.8, r) * (1.0 - smoothstep(OuterRadius * HAZE_RADIAL_EXPAND * 0.75, OuterRadius * HAZE_RADIAL_EXPAND, r));
}
float GetJetHazeMask(vec3 pos_Rg, float InterRadius, float OuterRadius) {
    float r = length(pos_Rg.xz); float y = abs(pos_Rg.y);
    float maxJetRadius = max(sqrt(2.0 * InterRadius * InterRadius + 0.03 * 0.03 * y * y), 1.3 * InterRadius + 0.25 * y) * 1.2;
    return (1.0 - smoothstep(maxJetRadius * 0.8, maxJetRadius * 1.1, r)) * (1.0 - smoothstep(OuterRadius * 0.8 * 0.75, OuterRadius * 0.8 * 1.0, y)) * smoothstep(InterRadius * 0.5, InterRadius * 1.5, y);
}
bool IsInHazeBoundingVolume(vec3 pos, float probeDist, float OuterRadius) {
    return (length(pos) <= OuterRadius * 1.2 + probeDist);
}
vec3 GetHazeForce(vec3 pos_Rg, float time, float PhysicalSpinA, float PhysicalQ, float InterRadius, float OuterRadius, float Thin, float Hopper, float AccretionRate) {
    float dFactorAbs = clamp(log(HAZE_DISK_DENSITY_REF/20.0) / 2.302585, 0.0, 1.0);
    float dFactorRel = (HAZE_JET_DENSITY_REF > 1e-20) ? clamp(log(HAZE_DISK_DENSITY_REF/HAZE_JET_DENSITY_REF) / 2.302585, 0.0, 1.0) : 1.0;
    float diskHazeStrength = dFactorAbs * dFactorRel;
    float jetHazeStrength = (AccretionRate >= 1e-2) ? clamp((log(AccretionRate) - log(1e-2)) / (log(1.0) - log(1e-2)), 0.0, 1.0) : 0.0;

    if (diskHazeStrength <= 0.001 && jetHazeStrength <= 0.001) return vec3(0.0);

    vec3 totalForce = vec3(0.0); float eps = 0.1;
    float rotSpeedBase = 100.0 * HAZE_ROT_SPEED; float jetSpeedBase = 50.0 * HAZE_FLOW_SPEED;
    float ReferenceOmega = GetKeplerianAngularVelocity(6.0, 1.0, PhysicalSpinA, PhysicalQ);
    float AdaptiveFrequency = max(abs(ReferenceOmega * rotSpeedBase) / (2.0 * kPi * 5.14), 0.1);
    float flowTime = time * AdaptiveFrequency;
    
    float phase1 = fract(flowTime); float phase2 = fract(flowTime + 0.5);
    float weight1 = 1.0 - abs(2.0 * phase1 - 1.0); float weight2 = 1.0 - abs(2.0 * phase2 - 1.0);
    bool doLayer1 = weight1 > 0.05; bool doLayer2 = weight2 > 0.05;
    float wTotal = (doLayer1 ? weight1 : 0.0) + (doLayer2 ? weight2 : 0.0);
    float w1_norm = (doLayer1 && wTotal > 0.0) ? (weight1 / wTotal) : 0.0;
    float w2_norm = (doLayer2 && wTotal > 0.0) ? (weight2 / wTotal) : 0.0;
    float t_offset1 = phase1 - 0.5; float t_offset2 = phase2 - 0.5;
    
    if (diskHazeStrength > 0.001) {
        float maskDisk = GetDiskHazeMask(pos_Rg, InterRadius, OuterRadius, Thin, Hopper);
        if (maskDisk > 0.001) {
            float omega = GetKeplerianAngularVelocity(length(pos_Rg.xz), 1.0, PhysicalSpinA, PhysicalQ);
            vec3 gradWorldCombined = vec3(0.0); float valCombined = 0.0;
            if (doLayer1) {
                float angle1 = omega * rotSpeedBase * t_offset1; float c1 = cos(angle1); float s1 = sin(angle1);
                vec3 pos1 = vec3(pos_Rg.x * c1 - pos_Rg.z * s1, pos_Rg.y, pos_Rg.x * s1 + pos_Rg.z * c1);
                float val1 = GetBaseNoise(pos1); vec3 grad1 = vec3(GetBaseNoise(pos1 + vec3(eps, 0.0, 0.0)) - val1, GetBaseNoise(pos1 + vec3(0.0, eps, 0.0)) - val1, GetBaseNoise(pos1 + vec3(0.0, 0.0, eps)) - val1);
                gradWorldCombined += vec3(grad1.x * c1 + grad1.z * s1, grad1.y, -grad1.x * s1 + grad1.z * c1) * w1_norm; valCombined += val1 * w1_norm;
            }
            if (doLayer2) {
                float angle2 = omega * rotSpeedBase * t_offset2; float c2 = cos(angle2); float s2 = sin(angle2);
                vec3 pos2 = vec3(pos_Rg.x * c2 - pos_Rg.z * s2, pos_Rg.y, pos_Rg.x * s2 + pos_Rg.z * c2);
                float val2 = GetBaseNoise(pos2); vec3 grad2 = vec3(GetBaseNoise(pos2 + vec3(eps, 0.0, 0.0)) - val2, GetBaseNoise(pos2 + vec3(0.0, eps, 0.0)) - val2, GetBaseNoise(pos2 + vec3(0.0, 0.0, eps)) - val2);
                gradWorldCombined += vec3(grad2.x * c2 + grad2.z * s2, grad2.y, -grad2.x * s2 + grad2.z * c2) * w2_norm; valCombined += val2 * w2_norm;
            }
            totalForce += gradWorldCombined * maskDisk * pow(max(0.0, valCombined - HAZE_DENSITY_THRESHOLD) / (1.0 - HAZE_DENSITY_THRESHOLD), 1.5) * diskHazeStrength;
        }
    }

    if (jetHazeStrength > 0.001) {
        float maskJet = GetJetHazeMask(pos_Rg, InterRadius, OuterRadius);
        if (maskJet > 0.001) {
            float v_jet_mag = 0.9; vec3 gradCombined = vec3(0.0); float valCombined = 0.0;
            if (doLayer1) {
                vec3 pos1 = pos_Rg; pos1.y -= sign(pos_Rg.y) * v_jet_mag * jetSpeedBase * t_offset1;
                float val1 = GetBaseNoise(pos1); vec3 grad1 = vec3(GetBaseNoise(pos1 + vec3(eps, 0.0, 0.0)) - val1, GetBaseNoise(pos1 + vec3(0.0, eps, 0.0)) - val1, GetBaseNoise(pos1 + vec3(0.0, 0.0, eps)) - val1);
                gradCombined += grad1 * w1_norm; valCombined += val1 * w1_norm;
            }
            if (doLayer2) {
                vec3 pos2 = pos_Rg; pos2.y -= sign(pos_Rg.y) * v_jet_mag * jetSpeedBase * t_offset2;
                float val2 = GetBaseNoise(pos2); vec3 grad2 = vec3(GetBaseNoise(pos2 + vec3(eps, 0.0, 0.0)) - val2, GetBaseNoise(pos2 + vec3(0.0, eps, 0.0)) - val2, GetBaseNoise(pos2 + vec3(0.0, 0.0, eps)) - val2);
                gradCombined += grad2 * w2_norm; valCombined += val2 * w2_norm;
            }
            totalForce += gradCombined * maskJet * pow(max(0.0, valCombined - 0.3 - 0.7*HAZE_DENSITY_THRESHOLD) / clamp(1.0 - 0.3 - 0.7*HAZE_DENSITY_THRESHOLD, 0.0, 1.0), 1.5) * jetHazeStrength;
        }
    }
    return totalForce;
}

vec4 DiskColor(vec4 BaseColor, vec4 RayPos, vec4 LastRayPos, vec4 iP_cov, vec4 lastiP_cov, float iE_obs,
               float InterRadius, float OuterRadius, float Thin, float Hopper, float Brightmut, float Darkmut, float Reddening, float Saturation, float DiskTemperatureArgument,
               float BlackbodyIntensityExponent, float RedShiftColorExponent, float RedShiftIntensityExponent,
               float PeakTemperature, float ShiftMax, float PhysicalSpinA, float PhysicalQ, bool isoutgoing,
               float ThetaInShell, inout float RayMarchPhase, vec2 WP_CamX, vec2 WP_CamY, inout vec2 StokesQU) 
{
    vec4 CurrentResult = BaseColor;
    float MaxDiskHalfHeight = Thin + max(0.0, Hopper * OuterRadius) + 2.0; 
    if ((LastRayPos.y > MaxDiskHalfHeight && RayPos.y > MaxDiskHalfHeight) || (LastRayPos.y < -MaxDiskHalfHeight && RayPos.y < -MaxDiskHalfHeight)) return BaseColor;

    vec2 P0 = LastRayPos.xz; vec2 P1 = RayPos.xz; vec2 V = P1 - P0; float LenSq = dot(V, V);
    vec2 ClosestPoint = P0 + V * ((LenSq > 1e-8) ? clamp(-dot(P0, V) / LenSq, 0.0, 1.0) : 0.0);
    if (dot(ClosestPoint, ClosestPoint) > (OuterRadius * 1.1) * (OuterRadius * 1.1)) return BaseColor;

    vec3 StartPos = LastRayPos.xyz; vec3 EndPos = RayPos.xyz; vec3 ChordDelta = EndPos - StartPos;
    vec3 ChordDir = length(ChordDelta) > 1e-8 ? normalize(ChordDelta) : vec3(0.0, 1.0, 0.0);

    // --- 【固有距离（Proper Distance）】 ---
    vec3 MidPos = 0.5 * (StartPos + EndPos);
    KerrGeometry geo_mid; ComputeGeometryScalars(MidPos, PhysicalSpinA, PhysicalQ, 1.0, 1.0, isoutgoing, geo_mid);
    float proper_dist = sqrt(max(1e-9, dot(ChordDelta, ChordDelta) + geo_mid.f * pow(dot(geo_mid.l_down.xyz, ChordDelta), 2.0)));

    if (max(KerrSchildRadius(StartPos, PhysicalSpinA, 1.0), KerrSchildRadius(EndPos, PhysicalSpinA, 1.0)) < InterRadius * 0.9) return BaseColor;

    float TotalDist = proper_dist; float TraveledDist = 0.0;
    int SafetyLoopCount = 0; const int MaxLoops = 114514; 

    while (TraveledDist < TotalDist && SafetyLoopCount < MaxLoops) {
        if (CurrentResult.a > 0.99) break; SafetyLoopCount++;

        vec3 CurrentPos = mix(StartPos, EndPos, clamp(TraveledDist / max(1e-9, TotalDist), 0.0, 1.0));
        float DistanceToBlackHole = length(CurrentPos); 
        float SmallStepBoundary = max(OuterRadius, 12.0); float StepSize = 1.0; 
        
        StepSize *= 0.15 + 0.25 * min(max(0.0, 0.5 * (0.5 * DistanceToBlackHole / max(10.0 , SmallStepBoundary) - 1.0)), 1.0);
        if ((DistanceToBlackHole) >= 2.0 * SmallStepBoundary) StepSize *= DistanceToBlackHole;
        else if ((DistanceToBlackHole) >= 1.0 * SmallStepBoundary) StepSize *= ((1.0 + 0.25 * max(DistanceToBlackHole - 12.0, 0.0)) * (2.0 * SmallStepBoundary - DistanceToBlackHole) + DistanceToBlackHole * (DistanceToBlackHole - SmallStepBoundary)) / SmallStepBoundary;
        else StepSize *= min(1.0 + 0.25 * max(DistanceToBlackHole - 12.0, 0.0), DistanceToBlackHole);
        StepSize = max(0.01, StepSize); 

        float DistToNextSample = RayMarchPhase * StepSize;
        float NextTarget = min(TotalDist, TraveledDist + DistToNextSample);

        vec3 PosPrev = mix(StartPos, EndPos, clamp(TraveledDist / max(1e-9, TotalDist), 0.0, 1.0));
        vec3 PosNext = mix(StartPos, EndPos, clamp(NextTarget / max(1e-9, TotalDist), 0.0, 1.0));

        bool crossed = (PosPrev.y * PosNext.y < 0.0); bool shouldSample = false; vec3 SamplePos = PosNext; crossed = false;

        if (crossed) {
            float t_cross = abs(PosPrev.y) / max(1e-9, abs(PosPrev.y) + abs(PosNext.y));
            vec3 CPoint = mix(PosPrev, PosNext, t_cross);
            SamplePos = CPoint + min(Thin, length(CPoint - PosPrev)) * ChordDir * (-1.0 + 2.0 * RandomStep(10000.0 * (CPoint.zx / OuterRadius), fract(iTime * 1.0 + 0.5)));
            shouldSample = true; RayMarchPhase = 1.0; TraveledDist = NextTarget; 
        } else {
            if (NextTarget < TotalDist) { shouldSample = true; RayMarchPhase = 1.0; TraveledDist = NextTarget; }
            else { RayMarchPhase = max(0.0, RayMarchPhase - (TotalDist - TraveledDist) / StepSize); TraveledDist = TotalDist; }
        }

        if (shouldSample) {
            float TimeInterpolant = min(1.0, TraveledDist / max(1e-9, TotalDist));
            vec4 Sample_X = vec4(SamplePos, mix(LastRayPos.w, RayPos.w, TimeInterpolant));
            vec4 Sample_P_cov = mix(lastiP_cov, iP_cov, TimeInterpolant);
            
            if (isoutgoing) transformKerrSchild_YSpin(Sample_X, 1.0, Sample_P_cov, 0.5, PhysicalSpinA, PhysicalQ, true);
            SamplePos = Sample_X.xyz; float EmissionTime = iBlackHoleTime + Sample_X.w;

            float PosR = KerrSchildRadius(SamplePos, PhysicalSpinA, 1.0); float PosY = SamplePos.y;
            float GeometricThin = Thin + max(0.0, (length(SamplePos.xz) - 3.0) * Hopper);
            float InnerCloudBound = max(GeometricThin, Thin * 1.0) * max(0.0, 1.0 - 5.0 * pow((PosR - InterRadius) / min(OuterRadius - InterRadius, 12.0), 2.0));
            
            if (abs(PosY) < max(GeometricThin * 1.5, max(0.0, InnerCloudBound)) && PosR < OuterRadius && PosR > InterRadius) {
                 KerrGeometry geo_emit; ComputeGeometryScalars(SamplePos, PhysicalSpinA, PhysicalQ, 1.0, 1.0, false, geo_emit);
                 vec3 local_Dir = normalize(RaiseIndex(Sample_P_cov, geo_emit).xyz);

                 float NoiseLevel = max(0.0, 2.0 - 0.6 * GeometricThin);
                 float x = (PosR - InterRadius) / max(1e-6, OuterRadius - InterRadius); float a_param = max(1.0, (OuterRadius - InterRadius) / 10.0);
                 float EffectiveRadius = (a_param == 1.0) ? x : (-1.0 + sqrt(max(0.0, 1.0 + 4.0 * a_param * a_param * x - 4.0 * x * a_param))) / (2.0 * a_param - 2.0);
                 float DenAndThiFactor = Shape(EffectiveRadius, 0.9, 1.5);
                 float PosLogTheta_ForThick = Vec2ToTheta(SamplePos.zx, vec2(cos(-2.0 * log(max(1e-6, PosR))), sin(-2.0 * log(max(1e-6, PosR)))));
                 float PerturbedThickness = max(1e-6, GeometricThin * DenAndThiFactor * (0.4 + 0.6 * clamp(GeometricThin - 0.5, 0.0, 2.5) / 2.5 + (1.0 - (0.4 + 0.6 * clamp(GeometricThin - 0.5, 0.0, 2.5) / 2.5)) * SoftSaturate(GenerateAccretionDiskNoise(vec3(1.5 * PosLogTheta_ForThick, PosR + 0.25 / 3.0 * EmissionTime, 0.0), -0.7 + NoiseLevel, 1.3 + NoiseLevel, 80.0))));

                 if ((abs(PosY) < PerturbedThickness) || (abs(PosY) < InnerCloudBound)) {
                     float AngularVelocity = GetKeplerianAngularVelocity(max(InterRadius, PosR), 1.0, PhysicalSpinA, PhysicalQ);
                     float u = sqrt(max(1e-6, PosR)); float k_cubed = PhysicalSpinA * 0.70710678; float SpiralTheta;
                     if (abs(k_cubed) < 0.001 * u * u * u) SpiralTheta = -16.9705627 / u * (1.0 - 0.25 * k_cubed * pow(1.0/u, 3.0) + 0.142857 * pow(k_cubed * pow(1.0/u, 3.0), 2.0));
                     else SpiralTheta = (5.6568542 / (sign(k_cubed) * pow(abs(k_cubed), 0.33333333))) * (0.5 * log(max(1e-9, (PosR - (sign(k_cubed) * pow(abs(k_cubed), 0.33333333))*u + pow(sign(k_cubed) * pow(abs(k_cubed), 0.33333333), 2.0)) / max(1e-9, pow(u+(sign(k_cubed) * pow(abs(k_cubed), 0.33333333)), 2.0)))) + 1.7320508 * (atan(2.0*u - (sign(k_cubed) * pow(abs(k_cubed), 0.33333333)), 1.7320508 * (sign(k_cubed) * pow(abs(k_cubed), 0.33333333))) - 1.5707963));
                     float PosTheta = Vec2ToTheta(SamplePos.zx, vec2(cos(-SpiralTheta), sin(-SpiralTheta)));
                     float PosLogarithmicTheta = Vec2ToTheta(SamplePos.zx, vec2(cos(-2.0 * log(max(1e-6, PosR))), sin(-2.0 * log(max(1e-6, PosR)))));
                     
                     float inv_r = 1.0 / max(1e-6, PosR);
                     float inv_r2 = inv_r * inv_r;
                     float V_pot = inv_r - (PhysicalQ * PhysicalQ) * inv_r2;
                     
                     float g_tt = -(1.0 - V_pot);
                     float g_tphi = -PhysicalSpinA * V_pot; 
                     float g_phiphi = PosR * PosR + PhysicalSpinA * PhysicalSpinA + PhysicalSpinA * PhysicalSpinA * V_pot;
                     float norm_metric = g_tt + 2.0 * AngularVelocity * g_tphi + AngularVelocity * AngularVelocity * g_phiphi;
                     
                     float min_norm = -0.01; 
                     float u_t = inversesqrt(max(abs(min_norm), -norm_metric));
                     
                     float P_phi = - SamplePos.x * Sample_P_cov.z + SamplePos.z * Sample_P_cov.x;
                     float E_emit = u_t * (iE_obs - AngularVelocity * P_phi);
                     float FreqRatio = 1.0 / max(1e-6, E_emit);
                     float VisionTemperature = pow(DiskTemperatureArgument * pow(1.0 / max(1e-6, PosR), 3.0) * max(1.0 - sqrt(InterRadius / max(1e-6, PosR)), 0.000001), 0.25) * pow(FreqRatio, RedShiftColorExponent); 
                     float BrightWithoutRedshift = (0.05 * min(OuterRadius / 1000.0, 1000.0 / OuterRadius) + 0.55 / exp(5.0 * EffectiveRadius) * mix(0.2 + 0.8 * abs(local_Dir.y), 1.0, clamp(GeometricThin - 0.8, 0.2, 1.0))) * pow(pow(DiskTemperatureArgument * pow(1.0 / max(1e-6, PosR), 3.0) * max(1.0 - sqrt(InterRadius / max(1e-6, PosR)), 0.000001), 0.25) / PeakTemperature, BlackbodyIntensityExponent); 
                     
                     float Density = DenAndThiFactor; vec4 SampleColor = vec4(0.0);

                     if (abs(PosY) < PerturbedThickness) {
                         float Levelmut = 0.91 * log(1.0 + (0.06 / 0.91 * max(0.0, min(1000.0, PosR) - 10.0))); float Conmut = 80.0 * log(1.0 + (0.1 * 0.06 * max(0.0, min(1000000.0, PosR) - 10.0)));
                         SampleColor = vec4(GenerateAccretionDiskNoise(vec3(0.1 * (PosR + 0.25 / 3.0 * EmissionTime), 0.1 * PosY, 0.02 * pow(OuterRadius, 0.7) * PosTheta), NoiseLevel + 2.0 - Levelmut, NoiseLevel + 4.0 - Levelmut, 80.0 - Conmut)); 
                         if(PosTheta + kPi < 0.1 * kPi) { SampleColor *= (PosTheta + kPi) / (0.1 * kPi); SampleColor += (1.0 - ((PosTheta + kPi) / (0.1 * kPi))) * vec4(GenerateAccretionDiskNoise(vec3(0.1 * (PosR + 0.25 / 3.0 * EmissionTime), 0.1 * PosY, 0.02 * pow(OuterRadius, 0.7) * (PosTheta + 2.0 * kPi)), NoiseLevel + 2.0 - Levelmut, NoiseLevel + 4.0 - Levelmut, 80.0 - Conmut)); }
                         if(PosR > max(0.15379 * OuterRadius, 0.15379 * 64.0)) {
                             float TimeShiftedRadiusTerm = PosR * (4.65114e-6) - 0.1 / 3.0 * EmissionTime;
                             float Spir = (GenerateAccretionDiskNoise(vec3(0.1 * (TimeShiftedRadiusTerm - 0.08 * OuterRadius * PosLogarithmicTheta), 0.1 * PosY, 0.02 * pow(OuterRadius, 0.7) * PosLogarithmicTheta), NoiseLevel + 2.0 - Levelmut, NoiseLevel + 3.0 - Levelmut, 80.0 - Conmut)); 
                             if(PosLogarithmicTheta + kPi < 0.1 * kPi) { Spir *= (PosLogarithmicTheta + kPi) / (0.1 * kPi); Spir += (1.0 - ((PosLogarithmicTheta + kPi) / (0.1 * kPi))) * (GenerateAccretionDiskNoise(vec3(0.1 * (TimeShiftedRadiusTerm - 0.08 * OuterRadius * (PosLogarithmicTheta + 2.0 * kPi)), 0.1 * PosY, 0.02 * pow(OuterRadius, 0.7) * (PosLogarithmicTheta + 2.0 * kPi)), NoiseLevel + 2.0 - Levelmut, NoiseLevel + 3.0 - Levelmut, 80.0 - Conmut)); }
                             SampleColor *= mix(1.0, clamp(0.7 * Spir * 1.5 - 0.5, 0.0, 3.0), 0.5 + 0.5 * max(-1.0, 1.0 - exp(-1.5 * 0.1 * (100.0 * PosR / max(OuterRadius, 64.0) - 20.0))));
                         }
                         Density *= 0.7 * max(0.0, 1.0 - abs(PosY) / PerturbedThickness);
                         SampleColor.xyz *= Density * 1.4 * max(0.0, (0.2 + 2.0 * sqrt(max(0.0, clamp(abs(PosY) / PerturbedThickness, 0.0, 1.0) * clamp(abs(PosY) / PerturbedThickness, 0.0, 1.0) + 0.001))));
                         SampleColor.a *= (Density * Density) / 0.3;
                     }
        
                     SampleColor.xyz *= 1.0 + clamp(iPhotonRingBoost, 0.0, 10.0) * clamp(0.3 * ThetaInShell - 0.1, 0.0, 1.0);
                     VisionTemperature *= 1.0 + clamp(iPhotonRingColorTempBoost, 0.0, 10.0) * clamp(0.3 * ThetaInShell - 0.1, 0.0, 1.0);
                     
                     float InnerCloudTimePhase = kPi / (kPi / max(1e-6, GetKeplerianAngularVelocity(max(3.0, InterRadius), 1.0, PhysicalSpinA, PhysicalQ))) * EmissionTime; 
                     if (abs(PosY) < InnerCloudBound) {
                         float DustIntensity = max(1.0 - pow(PosY / (GeometricThin * max(1.0 - 5.0 * pow((PosR - InterRadius) / min(OuterRadius - InterRadius, 12.0), 2.0), 0.0001)), 2.0), 0.0);
                         if (DustIntensity > 0.0) SampleColor += 0.02 * vec4(vec3(DustIntensity * GenerateAccretionDiskNoise(vec3(1.5 * fract((1.5 * Vec2ToTheta(SamplePos.zx, vec2(cos(0.666666 * InnerCloudTimePhase), sin(0.666666 * InnerCloudTimePhase))) + InnerCloudTimePhase) / 2.0 / kPi) * 2.0 * kPi, PosR, PosY), 0.0, 6.0, 80.0)), 0.2 * DustIntensity * GenerateAccretionDiskNoise(vec3(1.5 * fract((1.5 * Vec2ToTheta(SamplePos.zx, vec2(cos(0.666666 * InnerCloudTimePhase), sin(0.666666 * InnerCloudTimePhase))) + InnerCloudTimePhase) / 2.0 / kPi) * 2.0 * kPi, PosR, PosY), 0.0, 6.0, 80.0)) * sqrt(max(0.0, 1.0001 - local_Dir.y * local_Dir.y));
                     }

                     SampleColor.xyz *= BrightWithoutRedshift * KelvinToRgb(VisionTemperature) * min(pow(FreqRatio, RedShiftIntensityExponent), ShiftMax) * min(1.0, 1.3 * (OuterRadius - PosR) / (OuterRadius - InterRadius)); SampleColor.a *= 0.125;
                     float DilutionOuterRadius = mix(min(OuterRadius, 25.0), OuterRadius, smoothstep(6.0, max(0.05*OuterRadius,12.0), PosR));
                     SampleColor *= max(mix(vec4(5.0 / (max(Thin, 0.2) + (Hopper * 0.5) * DilutionOuterRadius)), vec4(vec3(0.3 + 0.7 * 5.0 / (Thin + (Hopper * 0.5) * DilutionOuterRadius)), 1.0), 0.0), mix(vec4(100.0 / DilutionOuterRadius), vec4(vec3(0.3 + 0.7 * 100.0 / DilutionOuterRadius), 1.0), exp(-pow(20.0 * PosR / DilutionOuterRadius, 2.0))));

                     float InnerBrightenFac = mix(3.0,2.0,clamp((OuterRadius - 50.0)/50.0,0.0,1.0));
                     float InnerBrightenRatio = 1.0-clamp(6.0*(PosR - InterRadius) / (OuterRadius - InterRadius),0.0,1.0);
                     InnerBrightenRatio *= InnerBrightenRatio;
                     SampleColor.xyz *= mix(1.0, max(1.0, abs(local_Dir.y) / 0.2), clamp(0.3 - 0.6 * (PerturbedThickness / max(1e-6, Density) - 1.0), 0.0, 0.3)) * (1.0 + 1.2 * max(0.0, max(0.0, min(1.0, 3.0 - 2.0 * Thin)) * min(0.5, 1.0 - 5.0 * Hopper))) * Brightmut * (1.0 + InnerBrightenFac*InnerBrightenRatio);
                     SampleColor.a *= Darkmut * (1.0 + (1.0+InnerBrightenFac)*InnerBrightenRatio);
                     if (u_t * (iE_obs - AngularVelocity * (-SamplePos.x * Sample_P_cov.z + SamplePos.z * Sample_P_cov.x)) < 0.0) {
                         SampleColor.rgb = vec3(max(max(SampleColor.r, SampleColor.g), SampleColor.b) + min(min(SampleColor.r, SampleColor.g), SampleColor.b)) - SampleColor.rgb;
                         if(iWhitehole == 0) SampleColor.rgba = vec4(0.0);
                     }

                     vec4 StepColor = SampleColor * StepSize;

                     if (iPolarization != 0) {
                         vec4 B_up = normalize(vec4(-SamplePos.z, 0.0, SamplePos.x, 0.0)) * cos(-0.7) + normalize(vec4(SamplePos.x, SamplePos.y, SamplePos.z, 0.0)) * sin(-0.7); B_up.w = 0.0;
                         vec4 u_up_fluid = vec4(AngularVelocity * (-SamplePos.z), 0.0, AngularVelocity * SamplePos.x, 1.0) * u_t;
                         vec4 f_down = vec4(det3(u_up_fluid.yzw, RaiseIndex(Sample_P_cov, geo_emit).yzw, B_up.yzw), -det3(u_up_fluid.xzw, RaiseIndex(Sample_P_cov, geo_emit).xzw, B_up.xzw), det3(u_up_fluid.xyw, RaiseIndex(Sample_P_cov, geo_emit).xyw, B_up.xyw), -det3(u_up_fluid.xyz, RaiseIndex(Sample_P_cov, geo_emit).xyz, B_up.xyz));
                         f_down /= sqrt(max(1e-12, abs(dot(RaiseIndex(f_down, geo_emit), f_down))));
                         vec2 ScreenAmps = SolvePolarization(GetWalkerPenrose(vec4(SamplePos, EmissionTime), Sample_P_cov, f_down, PhysicalSpinA, PhysicalQ, PosR), WP_CamX, WP_CamY);
                         float weight = (SampleColor.r + SampleColor.g + SampleColor.b) * StepSize * pow(1.0 - CurrentResult.a, 1.0);
                         StokesQU.x += (ScreenAmps.x * ScreenAmps.x - ScreenAmps.y * ScreenAmps.y) * weight; StokesQU.y += (2.0 * ScreenAmps.x * ScreenAmps.y) * weight;
                     }

                     float aR = 1.0 + Reddening * 0.0; float aG = 1.0 + Reddening * 2.0; float aB = 1.0 + Reddening * 5.0;
                     float Denominator = StepColor.r * pow(1.0 - CurrentResult.a, aR) + StepColor.g * pow(1.0 - CurrentResult.a, aG) + StepColor.b * pow(1.0 - CurrentResult.a, aB);
                     if (Denominator > 0.000001) {
                         float Sum_rgb = (StepColor.r + StepColor.g + StepColor.b) * pow(1.0 - CurrentResult.a, aG);
                         CurrentResult.r += Sum_rgb * StepColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator * pow(3.0 * (Sum_rgb * StepColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) / ((Sum_rgb * StepColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) + (Sum_rgb * StepColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) + (Sum_rgb * StepColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator)), Saturation);
                         CurrentResult.g += Sum_rgb * StepColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator * pow(3.0 * (Sum_rgb * StepColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) / ((Sum_rgb * StepColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) + (Sum_rgb * StepColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) + (Sum_rgb * StepColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator)), Saturation);
                         CurrentResult.b += Sum_rgb * StepColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator * pow(3.0 * (Sum_rgb * StepColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator) / ((Sum_rgb * StepColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) + (Sum_rgb * StepColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) + (Sum_rgb * StepColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator)), Saturation);
                     }
                     CurrentResult.a += StepColor.a * (1.0 - CurrentResult.a);
                 }
            }
        }
    }
    return CurrentResult;
}

vec4 JetColor(vec4 BaseColor, vec4 RayPos, vec4 LastRayPos, vec4 iP_cov, vec4 lastiP_cov, float iE_obs, float InterRadius, float OuterRadius, float JetRedShiftIntensityExponent, float JetBrightmut, float JetReddening, float JetSaturation, float AccretionRate, float JetShiftMax, float PhysicalSpinA, float PhysicalQ, bool isoutgoing, inout float RayMarchPhase) 
{
    vec4 CurrentResult = BaseColor;
    vec3 StartPos = LastRayPos.xyz; vec3 EndPos = RayPos.xyz;
    if (any(isnan(StartPos)) || any(isinf(StartPos))) return BaseColor;

    vec3 ChordDelta = EndPos - StartPos; vec3 MidPos = 0.5 * (StartPos + EndPos);
    KerrGeometry geo_mid; ComputeGeometryScalars(MidPos, PhysicalSpinA, PhysicalQ, 1.0, 1.0, isoutgoing, geo_mid);
    float proper_dist = sqrt(max(1e-9, dot(ChordDelta, ChordDelta) + geo_mid.f * pow(dot(geo_mid.l_down.xyz, ChordDelta), 2.0)));
    float TotalDist = proper_dist; float TraveledDist = 0.0;
    
    if (max(length(StartPos.xz), length(RayPos.xz)) > OuterRadius * 1.5 && max(abs(StartPos.y), abs(RayPos.y)) < OuterRadius) return BaseColor;

    int SafetyLoopCount = 0; const int MaxLoops = 114514; 
    while (TraveledDist < TotalDist && SafetyLoopCount < MaxLoops) {
        if (CurrentResult.a > 0.99) break; SafetyLoopCount++;

        vec3 CurrentPos = mix(StartPos, EndPos, clamp(TraveledDist / max(1e-9, TotalDist), 0.0, 1.0));
        float DistanceToBlackHole = length(CurrentPos); 
        float SmallStepBoundary = max(OuterRadius, 12.0); float StepSize = 1.0; 
        
        StepSize *= 0.15 + 0.25 * min(max(0.0, 0.5 * (0.5 * DistanceToBlackHole / max(10.0 , SmallStepBoundary) - 1.0)), 1.0);
        if ((DistanceToBlackHole) >= 2.0 * SmallStepBoundary) StepSize *= DistanceToBlackHole;
        else if ((DistanceToBlackHole) >= 1.0 * SmallStepBoundary) StepSize *= ((1.0 + 0.25 * max(DistanceToBlackHole - 12.0, 0.0)) * (2.0 * SmallStepBoundary - DistanceToBlackHole) + DistanceToBlackHole * (DistanceToBlackHole - SmallStepBoundary)) / SmallStepBoundary;
        else StepSize *= min(1.0 + 0.25 * max(DistanceToBlackHole - 12.0, 0.0), DistanceToBlackHole);
        StepSize = max(0.01, StepSize); 

        float DistToNextSample = RayMarchPhase * StepSize;
        float NextTarget = min(TotalDist, TraveledDist + DistToNextSample);

        bool shouldSample = false;
        if (NextTarget < TotalDist) { shouldSample = true; RayMarchPhase = 1.0; TraveledDist = NextTarget; }
        else { RayMarchPhase = max(0.0, RayMarchPhase - (TotalDist - TraveledDist) / StepSize); TraveledDist = TotalDist; }

        if (shouldSample) {
            float TimeInterpolant = min(1.0, TraveledDist / max(1e-9, TotalDist));
            vec4 Sample_X = vec4(mix(StartPos, EndPos, TimeInterpolant), mix(LastRayPos.w, RayPos.w, TimeInterpolant));
            vec4 Sample_P_cov = mix(lastiP_cov, iP_cov, TimeInterpolant);
            if (isoutgoing) transformKerrSchild_YSpin(Sample_X, 1.0, Sample_P_cov, 0.5, PhysicalSpinA, PhysicalQ, true);
            vec3 SamplePos = Sample_X.xyz; float EmissionTime = iBlackHoleTime + Sample_X.w;

            float PosR = KerrSchildRadius(SamplePos, PhysicalSpinA, 1.0); float PosY = SamplePos.y; float RhoSq = dot(SamplePos.xz, SamplePos.xz); float Rho = sqrt(RhoSq);
            vec4 AccumColor = vec4(0.0); bool InJet = false;

            if (RhoSq < 2.0 * InterRadius * InterRadius + 0.03 * 0.03 * PosY * PosY && PosR < sqrt(2.0) * OuterRadius) {
                InJet = true; float ShapeVal = 1.0 / sqrt(max(1e-9, InterRadius * InterRadius + 0.02 * 0.02 * PosY * PosY));
                AccumColor += vec4(1.0, 1.0, 1.0, 0.5) * max(0.0, 1.0 - 5.0 * ShapeVal * abs(1.0 - pow(Rho * ShapeVal, 2.0))) * ShapeVal * mix(0.7 + 0.3 * PerlinNoise1D(0.3 * (EmissionTime - 1.0 / 0.8 * abs(abs(PosY) + 100.0 * (RhoSq / max(0.1, PosR)))) / max(1e-6, (OuterRadius / 100.0)) / (1.0 / 0.8)), 1.0, exp(-0.01 * 0.01 * PosY * PosY)) * max(0.0, 1.0 - 1.0 * exp(-0.0001 * PosY / max(1e-6, InterRadius) * PosY / max(1e-6, InterRadius))) * exp(-4.0 / (2.0) * PosR / max(1e-6, OuterRadius) * PosR / max(1e-6, OuterRadius)) * 0.5 * StepSize; 
            }

            float Wid = abs(PosY);
            if (Rho < 1.3 * InterRadius + 0.25 * Wid && Rho > 0.7 * InterRadius + 0.15 * Wid && PosR < 30.0 * InterRadius) {
                InJet = true; float ShapeVal = 1.0 / max(1e-9, (InterRadius + 0.2 * Wid));
                vec2 TwistedPos = SamplePos.xz + 0.2 * (1.1 - exp(-0.1 * 0.1 * PosY * PosY)) * (PerlinNoise1D(0.35 * (EmissionTime - 1.0 / 0.8 * abs(PosY)) / (1.0 / 0.8)) - 0.5) * vec2(cos(0.666666 * 2.0 * GetKeplerianAngularVelocity(InterRadius, 1.0, PhysicalSpinA, PhysicalQ) * (EmissionTime - 1.0 / 0.8 * abs(PosY))), -sin(0.666666 * 2.0 * GetKeplerianAngularVelocity(InterRadius, 1.0, PhysicalSpinA, PhysicalQ) * (EmissionTime - 1.0 / 0.8 * abs(PosY))));
                AccumColor += vec4(1.0, 1.0, 1.0, 0.5) * max(0.0, 1.0 - 2.0 * abs(1.0 - pow(length(TwistedPos) * ShapeVal, 2.0))) * ShapeVal * (1.0 - exp(-PosY / max(1e-6, InterRadius) * PosY / max(1e-6, InterRadius))) * exp(-0.005 * PosY / max(1e-6, InterRadius) * PosY / max(1e-6, InterRadius)) * 0.5 * StepSize; 
            }

            if (InJet) {
                KerrGeometry geo_sample; ComputeGeometryScalars(SamplePos, PhysicalSpinA, PhysicalQ, 1.0, 1.0, false, geo_sample);
                vec3 U_spatial = vec3(0.0, sign(PosY) * 1.6666667 * 0.8, 0.0);
                float l_dot_u_sp = dot(geo_sample.l_down.xyz, U_spatial); float U_sp_sq = dot(U_spatial, U_spatial);
                float A = -1.0 + geo_sample.f; float B = 2.0 * geo_sample.f * l_dot_u_sp; float C = U_sp_sq + geo_sample.f * l_dot_u_sp * l_dot_u_sp + 1.0; 
                float Det = B * B - 4.0 * A * C;
                if (Det < 0.0) { U_spatial = (A < 0.0) ? U_spatial * 0.5 : -1.5 * geo_sample.grad_r; l_dot_u_sp = dot(geo_sample.l_down.xyz, U_spatial); U_sp_sq = dot(U_spatial, U_spatial); C = U_sp_sq + geo_sample.f * l_dot_u_sp * l_dot_u_sp + 1.0; B = 2.0 * geo_sample.f * l_dot_u_sp; Det = max(0.0, B * B - 4.0 * A * C); }
                float Ut = (abs(A) < 1e-7) ? (-C / max(1e-19, B)) : ((B < 0.0) ? (2.0 * C / (-B + sqrt(Det))) : ((-B - sqrt(Det)) / (2.0 * A)));
                
                float E_emit = -dot(Sample_P_cov, vec4(U_spatial, Ut)); float FreqRatio = 1.0 / max(1e-6, E_emit);
                AccumColor.xyz *= KelvinToRgb(min(100000.0 * FreqRatio,100000.0)) * min(pow(FreqRatio, JetRedShiftIntensityExponent), JetShiftMax) * JetBrightmut * (0.5 + 0.5 * tanh(log(max(1e-6, AccretionRate)) + 1.0)); AccumColor.a *= 0.0; 
                
                if (E_emit <= 0.0) AccumColor.rgb = vec3(max(max(AccumColor.r, AccumColor.g), AccumColor.b) + min(min(AccumColor.r, AccumColor.g), AccumColor.b)) - AccumColor.rgb;

                float aR = 1.0; float aG = 1.0 + JetReddening * 2.0; float aB = 1.0 + JetReddening * 5.0;
                float Denominator = AccumColor.r * pow(1.0 - CurrentResult.a, aR) + AccumColor.g * pow(1.0 - CurrentResult.a, aG) + AccumColor.b * pow(1.0 - CurrentResult.a, aB);
                if (Denominator > 0.000001) {
                    float Sum_rgb = (AccumColor.r + AccumColor.g + AccumColor.b) * pow(1.0 - CurrentResult.a, aG);
                    CurrentResult.r += Sum_rgb * AccumColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator * pow(3.0 * (Sum_rgb * AccumColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) / ((Sum_rgb * AccumColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) + (Sum_rgb * AccumColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) + (Sum_rgb * AccumColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator)), JetSaturation);
                    CurrentResult.g += Sum_rgb * AccumColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator * pow(3.0 * (Sum_rgb * AccumColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) / ((Sum_rgb * AccumColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) + (Sum_rgb * AccumColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) + (Sum_rgb * AccumColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator)), JetSaturation);
                    CurrentResult.b += Sum_rgb * AccumColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator * pow(3.0 * (Sum_rgb * AccumColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator) / ((Sum_rgb * AccumColor.r * pow(1.0 - CurrentResult.a, aR) / Denominator) + (Sum_rgb * AccumColor.g * pow(1.0 - CurrentResult.a, aG) / Denominator) + (Sum_rgb * AccumColor.b * pow(1.0 - CurrentResult.a, aB) / Denominator)), JetSaturation);
                }
                CurrentResult.a += AccumColor.a * (1.0 - CurrentResult.a);
            }
        }
    }
    return CurrentResult;
}

vec4 GridColorSimple(vec4 BaseColor, vec4 RayPos, vec4 LastRayPos, float PhysicalSpinA, float PhysicalQ, bool isoutgoing, float EndStepSign) {
    vec4 CurrentResult = BaseColor; if (CurrentResult.a > 0.99) return CurrentResult;
    float SignedGridRadii[5]; vec3 GridColors[5]; int GridCount = 0; float StartStepSign = EndStepSign; bool bHasCrossed = false; float t_cross = -1.0; vec3 DiskHitPos = vec3(0.0);
    if (LastRayPos.y * RayPos.y < 0.0) {
        float denom = (LastRayPos.y - RayPos.y);
        if(abs(denom) > 1e-9) { t_cross = LastRayPos.y / denom; DiskHitPos = mix(LastRayPos.xyz, RayPos.xyz, t_cross); if (length(DiskHitPos.xz) < abs(PhysicalSpinA)) { StartStepSign = -EndStepSign; bHasCrossed = true; } }
    }
    bool CheckPositive = (StartStepSign > 0.0) || (EndStepSign > 0.0); bool CheckNegative = (StartStepSign < 0.0) || (EndStepSign < 0.0);
    float HorizonDiscrim = 0.25 - PhysicalSpinA * PhysicalSpinA - PhysicalQ * PhysicalQ;
    if (CheckPositive) {
        SignedGridRadii[GridCount] = 70.0; GridColors[GridCount++] = 0.3*vec3(0.0, 1.0, 1.0); 
        if (HorizonDiscrim >= 0.0) { SignedGridRadii[GridCount] = (0.5 + sqrt(HorizonDiscrim)) * 1.06; GridColors[GridCount++] = 0.3*vec3(0.0, 1.0, 0.0); SignedGridRadii[GridCount] = (0.5 - sqrt(HorizonDiscrim)) * 0.94; GridColors[GridCount++] = 0.3*vec3(1.0, 0.0, 0.0); }
    }
    if (CheckNegative) { SignedGridRadii[GridCount] = -70.0; GridColors[GridCount++] = 0.3*vec3(1.0, 0.0, 1.0); }
    vec3 O = LastRayPos.xyz; vec3 D_vec = RayPos.xyz - LastRayPos.xyz;
    for (int i = 0; i < GridCount; i++) {
        if (CurrentResult.a > 0.99) break;
        float TargetSignedR = SignedGridRadii[i]; float TargetGeoR = abs(TargetSignedR); 
        vec2 roots = IntersectKerrEllipsoid(O, D_vec, TargetGeoR, PhysicalSpinA);
        float t_hits[2] = float[](roots.x, roots.y); if (t_hits[0] > t_hits[1]) { float temp = t_hits[0]; t_hits[0] = t_hits[1]; t_hits[1] = temp; }
        for (int j = 0; j < 2; j++) {
            float t = t_hits[j];
            if (t >= 0.0 && t <= 1.0) {
                float HitPointSign = bHasCrossed ? ((t > t_cross) ? EndStepSign : StartStepSign) : StartStepSign;
                if (HitPointSign * TargetSignedR < 0.0) continue;
                vec3 HitPos = O + D_vec * t; if (abs(KerrSchildRadius(HitPos, PhysicalSpinA, HitPointSign) - TargetSignedR) > 0.1 * TargetGeoR + 0.1) continue; 
                vec3 PatternPos = HitPos; float PatternTime = mix(LastRayPos.w, RayPos.w, t);
                if (isoutgoing) { vec4 tempX = vec4(HitPos, PatternTime); vec4 dummyP = vec4(0.0); transformKerrSchild_YSpin(tempX, HitPointSign, dummyP, 0.5, PhysicalSpinA, PhysicalQ, true); PatternPos = tempX.xyz; PatternTime = tempX.w; }
                float Omega = GetZamoOmega(TargetSignedR, PhysicalSpinA, PhysicalQ, PatternPos.y);
                float Phi = Vec2ToTheta(normalize(PatternPos.zx), vec2(0.0, 1.0)) + Omega * PatternTime + iBlackHoleTime*GetZamoOmega(TargetSignedR, PhysicalSpinA, PhysicalQ, 0.0);
                float CosTheta = clamp(PatternPos.y / TargetGeoR, -1.0, 1.0);
                if (max(smoothstep(clamp(0.002 * min(20.0,length(PatternPos)), 0.01, 0.15) / max(0.005, sqrt(max(0.0, 1.0 - CosTheta * CosTheta))), 0.0, abs(fract(Phi / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.002 * min(20.0,length(PatternPos)), 0.01, 0.15), 0.0, abs(fract(acos(CosTheta) / kPi * 13.0) - 0.5))) > 0.01) {
                    CurrentResult.rgb += vec4(GridColors[i] * 2.0, 1.0).rgb * (max(smoothstep(clamp(0.002 * min(20.0,length(PatternPos)), 0.01, 0.15) / max(0.005, sqrt(max(0.0, 1.0 - CosTheta * CosTheta))), 0.0, abs(fract(Phi / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.002 * min(20.0,length(PatternPos)), 0.01, 0.15), 0.0, abs(fract(acos(CosTheta) / kPi * 13.0) - 0.5))) * 0.8) * (1.0 - CurrentResult.a);
                    CurrentResult.a += (max(smoothstep(clamp(0.002 * min(20.0,length(PatternPos)), 0.01, 0.15) / max(0.005, sqrt(max(0.0, 1.0 - CosTheta * CosTheta))), 0.0, abs(fract(Phi / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.002 * min(20.0,length(PatternPos)), 0.01, 0.15), 0.0, abs(fract(acos(CosTheta) / kPi * 13.0) - 0.5))) * 0.8) * (1.0 - CurrentResult.a);
                }
            }
        }
    }
    if (bHasCrossed && CurrentResult.a < 0.99) {
        vec3 PatternPosDisk = DiskHitPos;
        if (isoutgoing) { vec4 tempX = vec4(DiskHitPos, mix(LastRayPos.w, RayPos.w, t_cross)); vec4 dummyP = vec4(0.0); transformKerrSchild_YSpin(tempX, (length(DiskHitPos.xz) < abs(PhysicalSpinA)) ? -StartStepSign : StartStepSign, dummyP, 0.5, PhysicalSpinA, PhysicalQ, true); PatternPosDisk = tempX.xyz; }
        if (max(smoothstep(clamp(0.002 * length(DiskHitPos), 0.01, 0.1) / max(0.1, length(DiskHitPos.xz) / abs(PhysicalSpinA)), 0.0, abs(fract(Vec2ToTheta(normalize(PatternPosDisk.zx), vec2(0.0, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.002 * length(DiskHitPos), 0.01, 0.1), 0.0, abs(fract((length(DiskHitPos.xz) / max(1e-6, abs(PhysicalSpinA))) * 5.0) - 0.5))) > 0.01) {
            CurrentResult.rgb += vec4(0.3*vec3(1.0, 1.0, 1.0) * 5.0, 1.0).rgb * (max(smoothstep(clamp(0.002 * length(DiskHitPos), 0.01, 0.1) / max(0.1, length(DiskHitPos.xz) / abs(PhysicalSpinA)), 0.0, abs(fract(Vec2ToTheta(normalize(PatternPosDisk.zx), vec2(0.0, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.002 * length(DiskHitPos), 0.01, 0.1), 0.0, abs(fract((length(DiskHitPos.xz) / max(1e-6, abs(PhysicalSpinA))) * 5.0) - 0.5))) * 0.8) * (1.0 - CurrentResult.a);
            CurrentResult.a += (max(smoothstep(clamp(0.002 * length(DiskHitPos), 0.01, 0.1) / max(0.1, length(DiskHitPos.xz) / abs(PhysicalSpinA)), 0.0, abs(fract(Vec2ToTheta(normalize(PatternPosDisk.zx), vec2(0.0, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.002 * length(DiskHitPos), 0.01, 0.1), 0.0, abs(fract((length(DiskHitPos.xz) / max(1e-6, abs(PhysicalSpinA))) * 5.0) - 0.5))) * 0.8) * (1.0 - CurrentResult.a);
        }
    }
    return CurrentResult;
}

vec4 GridColor(vec4 BaseColor, vec4 RayPos, vec4 LastRayPos, vec4 iP_cov, float iE_obs, float PhysicalSpinA, float PhysicalQ, bool isoutgoing, float EndStepSign) {
    vec4 CurrentResult = BaseColor; if (CurrentResult.a > 0.99) return CurrentResult;
    float SignedGridRadii[12]; int GridCount = 0; float StartStepSign = EndStepSign; bool bHasCrossed = false; float t_cross = -1.0; vec3 DiskHitPos = vec3(0.0);
    if (LastRayPos.y * RayPos.y < 0.0) {
        float denom = (LastRayPos.y - RayPos.y);
        if(abs(denom) > 1e-9) { t_cross = LastRayPos.y / denom; DiskHitPos = mix(LastRayPos.xyz, RayPos.xyz, t_cross); if (length(DiskHitPos.xz) < abs(PhysicalSpinA)) { StartStepSign = -EndStepSign; bHasCrossed = true; } }
    }
    float HorizonDiscrim = 0.25 - PhysicalSpinA * PhysicalSpinA - PhysicalQ * PhysicalQ;
    if (StartStepSign > 0.0 || EndStepSign > 0.0) { SignedGridRadii[GridCount++] = (0.5 + sqrt(max(0.0, HorizonDiscrim))) * 1.06; SignedGridRadii[GridCount++] = 20.0; if (HorizonDiscrim >= 0.0) { SignedGridRadii[GridCount++] = (0.5 - sqrt(HorizonDiscrim)) * 0.94; } }
    if (StartStepSign < 0.0 || EndStepSign < 0.0) { SignedGridRadii[GridCount++] = -3.0; SignedGridRadii[GridCount++] = -10.0; }
    vec3 O = LastRayPos.xyz; vec3 D_vec = RayPos.xyz - LastRayPos.xyz;
    for (int i = 0; i < GridCount; i++) {
        if (CurrentResult.a > 0.99) break;
        float TargetSignedR = SignedGridRadii[i]; float TargetGeoR = abs(TargetSignedR); 
        vec2 roots = IntersectKerrEllipsoid(O, D_vec, TargetGeoR, PhysicalSpinA);
        float t_hits[2] = float[](roots.x, roots.y); if (t_hits[0] > t_hits[1]) { float temp = t_hits[0]; t_hits[0] = t_hits[1]; t_hits[1] = temp; }
        for (int j = 0; j < 2; j++) {
            float t = t_hits[j];
            if (t >= 0.0 && t <= 1.0) {
                float HitPointSign = bHasCrossed ? ((t > t_cross) ? EndStepSign : StartStepSign) : StartStepSign;
                if (HitPointSign * TargetSignedR < 0.0) continue;
                vec3 HitPos = O + D_vec * t; if (abs(KerrSchildRadius(HitPos, PhysicalSpinA, HitPointSign) - TargetSignedR) > 0.1 * TargetGeoR + 0.1) continue; 
                float HitTime = mix(LastRayPos.w, RayPos.w, t);
                float Omega = GetZamoOmega(TargetSignedR, PhysicalSpinA, PhysicalQ, HitPos.y);
                KerrGeometry geo_hit; ComputeGeometryScalars(HitPos, PhysicalSpinA, PhysicalQ, 1.0, HitPointSign, isoutgoing, geo_hit);
                vec4 U_zamo_unnorm = vec4(Omega * vec3(HitPos.z, 0.0, -HitPos.x), 1.0); 
                float Shift = 1.0 / max(1e-6, abs(-dot(iP_cov, U_zamo_unnorm / sqrt(max(1e-9, abs(dot(U_zamo_unnorm, LowerIndex(U_zamo_unnorm, geo_hit)))))))); 
                vec3 PatternPos = HitPos; float PatternTime = HitTime;
                if (isoutgoing) { vec4 tempX = vec4(HitPos, HitTime); vec4 dummyP = vec4(0.0); transformKerrSchild_YSpin(tempX, HitPointSign, dummyP, 0.5, PhysicalSpinA, PhysicalQ, true); PatternPos = tempX.xyz; PatternTime = tempX.w; }
                float CosTheta = clamp(PatternPos.y / TargetGeoR, -1.0, 1.0);
                if (max(smoothstep(clamp(0.001 * length(PatternPos), 0.01, 0.1) / max(0.005, sqrt(max(0.0, 1.0 - CosTheta * CosTheta))), 0.0, abs(fract((Vec2ToTheta(normalize(PatternPos.zx), vec2(0.0, 1.0)) + Omega * PatternTime + iBlackHoleTime*GetZamoOmega(TargetSignedR, PhysicalSpinA, PhysicalQ, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.001 * length(PatternPos), 0.01, 0.1), 0.0, abs(fract(acos(CosTheta) / kPi * 12.0) - 0.5))) > 0.01) {
                    CurrentResult.rgb += vec4(KelvinToRgb(6500.0 * Shift) * min(1.5 * pow(Shift, 4.0), 20.0), 1.0).rgb * (max(smoothstep(clamp(0.001 * length(PatternPos), 0.01, 0.1) / max(0.005, sqrt(max(0.0, 1.0 - CosTheta * CosTheta))), 0.0, abs(fract((Vec2ToTheta(normalize(PatternPos.zx), vec2(0.0, 1.0)) + Omega * PatternTime + iBlackHoleTime*GetZamoOmega(TargetSignedR, PhysicalSpinA, PhysicalQ, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.001 * length(PatternPos), 0.01, 0.1), 0.0, abs(fract(acos(CosTheta) / kPi * 12.0) - 0.5))) * 0.5) * (1.0 - CurrentResult.a);
                    CurrentResult.a += (max(smoothstep(clamp(0.001 * length(PatternPos), 0.01, 0.1) / max(0.005, sqrt(max(0.0, 1.0 - CosTheta * CosTheta))), 0.0, abs(fract((Vec2ToTheta(normalize(PatternPos.zx), vec2(0.0, 1.0)) + Omega * PatternTime + iBlackHoleTime*GetZamoOmega(TargetSignedR, PhysicalSpinA, PhysicalQ, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.001 * length(PatternPos), 0.01, 0.1), 0.0, abs(fract(acos(CosTheta) / kPi * 12.0) - 0.5))) * 0.5) * (1.0 - CurrentResult.a);
                }
            }
        }
    }
    if (bHasCrossed && CurrentResult.a < 0.99) {
        vec3 PatternPosDisk = DiskHitPos;
        if (isoutgoing) { vec4 tempX = vec4(DiskHitPos, mix(LastRayPos.w, RayPos.w, t_cross)); vec4 dummyP = vec4(0.0); transformKerrSchild_YSpin(tempX, (length(DiskHitPos.xz) < abs(PhysicalSpinA)) ? -StartStepSign : StartStepSign, dummyP, 0.5, PhysicalSpinA, PhysicalQ, true); PatternPosDisk = tempX.xyz; }
        if (max(smoothstep(clamp(0.001 * length(DiskHitPos), 0.01, 0.1) / max(0.1, length(DiskHitPos.xz) / abs(PhysicalSpinA)), 0.0, abs(fract(Vec2ToTheta(normalize(PatternPosDisk.zx), vec2(0.0, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.001 * length(DiskHitPos), 0.01, 0.1), 0.0, abs(fract((length(DiskHitPos.xz) / max(1e-6, abs(PhysicalSpinA))) * 5.0) - 0.5))) > 0.01) {
            float Shift = 1.0 / max(1e-6, abs(-dot(iP_cov, vec4(0.0, 0.0, 0.0, 1.0))));
            CurrentResult.rgb += vec4(KelvinToRgb(6500.0 * Shift) * min(2.0 * pow(Shift, 4.0), 30.0), 1.0).rgb * (max(smoothstep(clamp(0.001 * length(DiskHitPos), 0.01, 0.1) / max(0.1, length(DiskHitPos.xz) / abs(PhysicalSpinA)), 0.0, abs(fract(Vec2ToTheta(normalize(PatternPosDisk.zx), vec2(0.0, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.001 * length(DiskHitPos), 0.01, 0.1), 0.0, abs(fract((length(DiskHitPos.xz) / max(1e-6, abs(PhysicalSpinA))) * 5.0) - 0.5))) * 0.5) * (1.0 - CurrentResult.a);
            CurrentResult.a += (max(smoothstep(clamp(0.001 * length(DiskHitPos), 0.01, 0.1) / max(0.1, length(DiskHitPos.xz) / abs(PhysicalSpinA)), 0.0, abs(fract(Vec2ToTheta(normalize(PatternPosDisk.zx), vec2(0.0, 1.0)) / k2Pi * 24.0) - 0.5)), smoothstep(clamp(0.001 * length(DiskHitPos), 0.01, 0.1), 0.0, abs(fract((length(DiskHitPos.xz) / max(1e-6, abs(PhysicalSpinA))) * 5.0) - 0.5))) * 0.5) * (1.0 - CurrentResult.a);
        }
    }
    return CurrentResult;
}

// =============================================================================
// SECTION 7: KN阴影计算
// =============================================================================

bool IsAccretionDiskVisible(float InterR, float OuterR, float Thin, float Hopper, float Bright, float Dark) {
    if (InterR >= OuterR) return false;
    if (Thin <= 0.0 && Hopper == 0.0) return false;
    if (Bright <= 0.0 && Dark < 0.0) return false;
    return true;
}

bool IsJetVisible(float AccretionRate, float JetBright) {
    if (AccretionRate < 1e-2) return false;
    if (JetBright <= 0.0) return false;
    return true;
}

float SolveCubicMaxReal(float P, float K) {
    if (P >= 0.0) return 0.0;
    return 2.0 * sqrt(-P / 3.0) * cos(acos(clamp((3.0 * K) / (2.0 * P) * sqrt(-3.0 / P), -1.0, 1.0)) / 3.0);
}

float SolveQuarticU(float M, float Q, float a, float sign_term, bool is_max_root) {
    float M2 = M * M; float Q2 = Q * Q;
    float c2 = 2.0 * Q2 - 3.0 * M2; float c1 = sign_term * (-2.0 * a * M2); float c0 = Q2 * Q2 - M2 * Q2;
    float u = is_max_root ? 2.2 * M : 0.8 * M; 
    for(int i=0; i<8; i++) {
        float f  = u*u*u*u + c2*u*u + c1*u + c0;
        float df = 4.0*u*u*u + 2.0*c2*u + c1;
        if (abs(df) < 1e-6) break;
        u = u - f / df;
    }
    return abs(u); 
}

float GetDropFrameAngle(float SinThetaStat, float CosThetaStat, float r, float M, float Q, float a, int ObserverMode) {
    if (ObserverMode == 0) return atan(SinThetaStat, CosThetaStat);
    float v_sq = (1.0+0.05*a)*min(0.9999, max(0.0, (2.0 * M * r - Q * Q) * (r*r + a*a) / max(1e-9, r*r * (r*r + a*a) + a*a * (2.0 * M * r - Q * Q)))); 
    return atan(SinThetaStat * sqrt(max(0.0, 1.0 - v_sq)), CosThetaStat + sqrt(v_sq));
}

float GetShadowHalfAngleRN(float r, float M, float Q, int ObserverMode) {
    float r_ps = 0.5 * (3.0 * M + sqrt(max(0.0, 9.0 * M*M - 8.0 * Q*Q)));
    float sin_theta_stat = (r_ps / sqrt(max(1e-6, 1.0 - 2.0 * M / r_ps + Q*Q / (r_ps * r_ps))) / r) * sqrt(max(0.0, 1.0 - 2.0 * M / r + Q*Q / (r*r)));
    return GetDropFrameAngle(sin_theta_stat, ((r >= r_ps - 1e-4) ? 1.0 : -1.0) * sqrt(max(0.0, 1.0 - sin_theta_stat * sin_theta_stat)), r, M, Q, 0.0, ObserverMode);
}

// =============================================================================
// SECTION 8: main
// =============================================================================

struct TraceResult {
    vec3  EscapeDir;      
    float FreqShift;      
    float Status;         
    vec4  AccumColor;     
    float CurrentSign;    
};

TraceResult TraceRay(vec2 FragUv, vec2 Resolution, mat4 iInverseCamRot, vec4 iBlackHoleRelativePosRs, vec4 iBlackHoleRelativeDiskNormal, vec4 iBlackHoleRelativeDiskTangen, float iUniverseSign)
{
    TraceResult res; res.EscapeDir = vec3(0.0); res.FreqShift = 0.0; res.Status = 0.0; res.AccumColor = vec4(0.0);
    bool bDeferredShadowCulling = false;

    float Fov = tan(iFovRadians / 2.0);
    vec2 Jitter = vec2(RandomStep(FragUv, fract(iTime * 1.0 + 0.5)), RandomStep(FragUv, fract(iTime * 1.0))) / Resolution;
    vec3 ViewDirLocal = FragUvToDir(FragUv + 0.25 * Jitter, Fov, Resolution); 

    float iSpinclamp = clamp(iSpin, -0.99, 0.99); float a2 = iSpinclamp * iSpinclamp; float abs_a = abs(iSpinclamp);
    float common_term = pow(1.0 - a2, 1.0/3.0); float Z1 = 1.0 + common_term * (pow(1.0 + abs_a, 1.0/3.0) + pow(1.0 - abs_a, 1.0/3.0));
    float Rms_M = 3.0 + sqrt(3.0 * a2 + Z1 * Z1) - (sign(iSpinclamp) * sqrt(max(0.0, (3.0 - Z1) * (3.0 + Z1 + 2.0 * sqrt(3.0 * a2 + Z1 * Z1))))); 
    float AccretionEffective = sqrt(max(0.001, 1.0 - (2.0 / 3.0) / Rms_M));

    float DiskArgument = 1.52491e30 / iBlackHoleMassSol * (iMu / AccretionEffective) * (iAccretionRate);
    float PeakTemperature = pow(DiskArgument * 0.05665278, 0.25);

    float PhysicalSpinA = iSpin * CONST_M; float PhysicalQ = iQ * CONST_M; 
    float HorizonDiscrim = 0.25 - PhysicalSpinA * PhysicalSpinA - PhysicalQ * PhysicalQ;
    float EventHorizonR = 0.5 + sqrt(max(0.0, HorizonDiscrim));
    float InnerHorizonR = 0.5 - sqrt(max(0.0, HorizonDiscrim));
    bool bIsNakedSingularity = HorizonDiscrim < 0.0;

    float RaymarchingBoundary = max(max(iOuterRadiusRs + 1.0, 501.0), iSpin * 2.0);
    float ShiftMax = 1.0; 
    float CurrentUniverseSign = iUniverseSign;
    if (iBlackHoleMassSol < 0.0) CurrentUniverseSign = -CurrentUniverseSign;

    vec3 CamToBHVecVisual = (iInverseCamRot * vec4(iBlackHoleRelativePosRs.xyz, 0.0)).xyz;
    vec3 RayPosWorld = -CamToBHVecVisual; 
    vec3 DiskNormalWorld = normalize((iInverseCamRot * vec4(iBlackHoleRelativeDiskNormal.xyz, 0.0)).xyz);
    vec3 DiskTangentWorld = normalize((iInverseCamRot * vec4(iBlackHoleRelativeDiskTangen.xyz, 0.0)).xyz);
    
    vec3 BH_Y = normalize(DiskNormalWorld); vec3 BH_X = normalize(DiskTangentWorld); BH_X = normalize(BH_X - dot(BH_X, BH_Y) * BH_Y); vec3 BH_Z = normalize(cross(BH_X, BH_Y));           
    mat3 LocalToWorldRot = mat3(BH_X, BH_Y, BH_Z); mat3 WorldToLocalRot = transpose(LocalToWorldRot);
    
    vec3 RayPosLocal = WorldToLocalRot * RayPosWorld;
    vec3 RayDirWorld_Geo = WorldToLocalRot * normalize((iInverseCamRot * vec4(ViewDirLocal, 0.0)).xyz);

    vec4 Result = vec4(0.0); bool bShouldContinueMarchRay = true; bool bWaitCalBack = false;
    float DistanceToBlackHole = length(RayPosLocal);
    
    if (DistanceToBlackHole > RaymarchingBoundary) {
        vec3 O = RayPosLocal; vec3 D = RayDirWorld_Geo; float r = RaymarchingBoundary - 1.0; 
        float b = dot(O, D); float c = dot(O, O) - r * r; float delta = b * b - c; 
        if (delta < 0.0) { bShouldContinueMarchRay = false; bWaitCalBack = true; } 
        else {
            float tEnter = -b - sqrt(delta); 
            if (tEnter > 0.0) RayPosLocal = O + D * tEnter;
            else if (-b + sqrt(delta) <= 0.0) { bShouldContinueMarchRay = false; bWaitCalBack = true; }
        }
    }

    vec4 X = vec4(RayPosLocal, 0.0); vec4 P_cov = vec4(0.0,0.0,0.0,-1.0);
    float E_conserved = 1.0; vec3 RayDir = RayDirWorld_Geo; vec3 LastDir = RayDir; vec3 LastPos = RayPosLocal;
    float GravityFade = CubicInterpolate(max(min(1.0 - (length(RayPosLocal) - 100.0) / (RaymarchingBoundary - 100.0), 1.0), 0.0));
    
    if (iEnableHeatHaze == 1 && iInWhichUniverse==0 && CurrentUniverseSign>0.0) {
        vec3 pos_Rg_Start = X.xyz; vec3 rayDirNorm = normalize(RayDir);
        float totalProbeDist = float(HAZE_PROBE_STEPS) * HAZE_STEP_SIZE;
        float hazeTime = mod(iBlackHoleTime, 1000.0); 

        if (IsInHazeBoundingVolume(pos_Rg_Start, totalProbeDist, iOuterRadiusRs)) {
            vec3 accumulatedForce = vec3(0.0); float totalWeight = 0.0;
            for (int i = 0; i < HAZE_PROBE_STEPS; i++) {
                float t = float(i+1) / float(HAZE_PROBE_STEPS);
                float weight = min(min(3.0*t, 1.0), 3.05 - 3.0*t);
                accumulatedForce += GetHazeForce(pos_Rg_Start + rayDirNorm * (float(i + 1) * HAZE_STEP_SIZE), hazeTime, PhysicalSpinA, PhysicalQ, iInterRadiusRs, iOuterRadiusRs, iThinRs, iHopper, iAccretionRate) * weight;
                totalWeight += weight;
            }
            vec3 avgHazeForce = accumulatedForce / max(0.001, totalWeight);
            if (dot(avgHazeForce, avgHazeForce) > 1e-10) {
                RayDir = normalize(RayDir + (avgHazeForce - dot(avgHazeForce, rayDirNorm) * rayDirNorm) * HAZE_STRENGTH * 25.0 * 0.1); 
                LastDir = RayDir;
            }
        }
    }
    
    bool isoutgoing = false; 
    if (bShouldContinueMarchRay) {
       P_cov = GetInitialMomentum(RayDir, X, iObserverMode, iUniverseSign, PhysicalSpinA, PhysicalQ, GravityFade, isoutgoing);
       if (P_cov == vec4(114514.0) && iWhitehole == 1) {
           isoutgoing = true; 
           P_cov = GetInitialMomentum(RayDir, X, iObserverMode, iUniverseSign, PhysicalSpinA, PhysicalQ, GravityFade, isoutgoing);
       }
    }
    if (P_cov == vec4(114514.0)) { bShouldContinueMarchRay = false; bWaitCalBack = false; Result = vec4(0.0,0.0,0.0,1.0); }

    vec2 WP_CamX = vec2(0.0); vec2 WP_CamY = vec2(0.0); vec2 StokesQU = vec2(0.0);

    if (bShouldContinueMarchRay && iPolarization != 0) {
        vec4 X_wp = X; vec4 P_cov_wp = P_cov;
        if (isoutgoing) transformKerrSchild_YSpin(X_wp, CurrentUniverseSign, P_cov_wp, CONST_M, PhysicalSpinA, PhysicalQ, true);
        KerrGeometry geo_wp; ComputeGeometryScalars(X_wp.xyz, PhysicalSpinA, PhysicalQ, GravityFade, CurrentUniverseSign, false, geo_wp);
        vec4 P_up_wp = RaiseIndex(P_cov_wp, geo_wp);

        vec4 U_up; float g_tt = -1.0 + geo_wp.f; U_up = vec4(0.0, 0.0, 0.0, 1.0 / sqrt(max(1e-9, -g_tt)));
        if (iObserverMode == 1) { 
            float r = geo_wp.r; float a = PhysicalSpinA; float y_phys = X_wp.y; 
            float rho2 = geo_wp.r2 + geo_wp.a2 * (y_phys * y_phys) / (geo_wp.r2 + 1e-9);
            float MassChargeTerm = 2.0 * CONST_M * r - PhysicalQ * PhysicalQ;
            float Xi = sqrt(max(0.0, MassChargeTerm * (geo_wp.r2 + geo_wp.a2)));
            float DenomPhi = rho2 * (MassChargeTerm + Xi);
            float U_phi_KS = (abs(DenomPhi) > 1e-9) ? (-MassChargeTerm * a / DenomPhi) : 0.0;
            float U_r_KS = -Xi / max(1e-9, rho2);
            float inv_r2_a2 = 1.0 / (geo_wp.r2 + geo_wp.a2);
            vec3 U_spatial = vec3((r * X_wp.x - a * X_wp.z) * inv_r2_a2 * U_r_KS + X_wp.z * U_phi_KS, (X_wp.y / r) * U_r_KS, (r * X_wp.z + a * X_wp.x) * inv_r2_a2 * U_r_KS - X_wp.x * U_phi_KS);
            float l_dot_u_spatial = dot(geo_wp.l_down.xyz, U_spatial); float U_spatial_sq = dot(U_spatial, U_spatial);
            float A = -1.0 + geo_wp.f; float B = 2.0 * geo_wp.f * l_dot_u_spatial; float C = U_spatial_sq + geo_wp.f * (l_dot_u_spatial * l_dot_u_spatial) + 1.0; 
            float Det = max(0.0, B*B - 4.0 * A * C);
            U_up = mix(vec4(0.0, 0.0, 0.0, 1.0 / sqrt(max(1e-9, -g_tt))), vec4(U_spatial, (abs(A) < 1e-7) ? (-C / max(1e-19, B)) : ((B < 0.0) ? (2.0 * C / (-B + sqrt(Det))) : ((-B - sqrt(Det)) / (2.0 * A)))), GravityFade);
        } else if (iObserverMode == 2 || iObserverMode == 3) { 
            vec3 v_in = (iObserverMode == 2) ? iCameraVelocity : -iCameraVelocity; if (any(isnan(v_in)) || any(isinf(v_in))) v_in = vec3(0.0);
            vec4 V_up = vec4(v_in, 1.0); vec4 V_down = LowerIndex(V_up, geo_wp);
            if (dot(V_up, V_down) < 0.0) U_up = V_up * inversesqrt(-dot(V_up, V_down));
        }
        vec4 U_down = LowerIndex(U_up, geo_wp); float E_obs = -dot(P_up_wp, U_down); 

        vec4 R_up = vec4(WorldToLocalRot * (iInverseCamRot * vec4(vec3(1.0, 0.0, 0.0), 0.0)).xyz, 0.0);
        vec4 Y_up = vec4(WorldToLocalRot * (iInverseCamRot * vec4(vec3(0.0, 1.0, 0.0), 0.0)).xyz, 0.0);
        
        R_up += dot(R_up, U_down) * U_up; Y_up += dot(Y_up, U_down) * U_up;
        vec4 D_down = LowerIndex(P_up_wp - E_obs * U_up, geo_wp);
        float invE2 = 1.0 / max(1e-12, E_obs * E_obs); 

        vec4 FX_up = R_up - (dot(R_up, D_down) * invE2) * (P_up_wp - E_obs * U_up);
        vec4 FY_up = Y_up - (dot(Y_up, D_down) * invE2) * (P_up_wp - E_obs * U_up);
        vec4 FX_down = LowerIndex(FX_up, geo_wp); vec4 FY_down = LowerIndex(FY_up, geo_wp);

        FX_down /= sqrt(max(1e-12, dot(FX_up, FX_down))); FY_down /= sqrt(max(1e-12, dot(FY_up, FY_down)));
        float r_start = KerrSchildRadius(X_wp.xyz, PhysicalSpinA, CurrentUniverseSign);
        WP_CamX = GetWalkerPenrose(X_wp, P_cov_wp, FX_down, PhysicalSpinA, PhysicalQ, r_start);
        WP_CamY = GetWalkerPenrose(X_wp, P_cov_wp, FY_down, PhysicalSpinA, PhysicalQ, r_start);
    }

    E_conserved = -P_cov.w;
    float TerminationR = -1.0; float CameraStartR = KerrSchildRadius(RayPosLocal, PhysicalSpinA, CurrentUniverseSign);
    
    if (CurrentUniverseSign > 0.0) {
        if (iObserverMode == 0) {
            float CosThetaSq = (RayPosLocal.y * RayPosLocal.y) / (CameraStartR * CameraStartR + 1e-20);
            float SL_Discrim = 0.25 - PhysicalQ * PhysicalQ - PhysicalSpinA * PhysicalSpinA * CosThetaSq;
            if (SL_Discrim >= 0.0 && CameraStartR < 0.5 + sqrt(SL_Discrim) && CameraStartR > 0.5 - sqrt(SL_Discrim)) { bShouldContinueMarchRay = false; bWaitCalBack = false; Result = vec4(0.0, 0.0, 0.0, 1.0); }
        }
        if (!bIsNakedSingularity && CurrentUniverseSign > 0.0 && iWhitehole==0) {
            if (CameraStartR > EventHorizonR) TerminationR = EventHorizonR; 
            else if (CameraStartR > InnerHorizonR) TerminationR = InnerHorizonR;
        }
    }
    
    float AbsSpin = abs(CONST_M * iSpin); float Q2 = iQ * iQ * CONST_M * CONST_M; 
    float r = 2.0 * CONST_M * (1.0 + cos(0.66666667 * acos(clamp(-abs(iSpin), -1.0, 1.0)))); 
    for(int k=0; k<3; k++) {
        float sqrt_term = sqrt(max(0.0001, CONST_M * r - Q2)); 
        float f = r*r - 3.0*CONST_M*r + 2.0*Q2 + 2.0 * AbsSpin * sqrt_term;
        float df = 2.0*r - 3.0*CONST_M + AbsSpin * CONST_M / sqrt_term;
        if(abs(df) < 0.00001) break; r = r - f / df;
    }
    float ProgradePhotonRadius = r;

    // Shadow Culling
    float AbsSpinA = abs(CONST_M * iSpin); bool bIsRot = AbsSpinA > 1e-5;
    if (!bIsNakedSingularity && CurrentUniverseSign > 0.0 && bShouldContinueMarchRay && iGrid==0 && iWhitehole==0 && (iObserverMode==0 || iObserverMode==1) && iEnableShadowCulling==1) {
        float CullingStartRadius = bIsRot ? ((SolveQuarticU(CONST_M, PhysicalQ, AbsSpinA, 1.0, true)*SolveQuarticU(CONST_M, PhysicalQ, AbsSpinA, 1.0, true) + PhysicalQ * PhysicalQ) / CONST_M + 0.05) : (1.005 * EventHorizonR);
        if (CameraStartR > CullingStartRadius) {
            vec3 ToCenterDir = -normalize(RayPosLocal); float CosAlpha = dot(normalize(RayDir), ToCenterDir); float RayAngle = acos(clamp(CosAlpha, -1.0, 1.0));
            if (RayAngle < (2.5 + 1.1 * abs(iSpin) - 0.5*iQ) * (2.0 * CONST_M) / max(1e-6, CameraStartR) || CameraStartR < 3.0*EventHorizonR) {
                bool bHitShadow = false; 
                if (!bIsRot) {
                    if (RayAngle < GetShadowHalfAngleRN(CameraStartR, CONST_M, PhysicalQ, iObserverMode) * SHADOW_SIZE_MULTIPLIER) bHitShadow = true;
                } else {
                    float M = CONST_M; float Q = PhysicalQ; float a = PhysicalSpinA; float a_abs = AbsSpinA; float r_p = M + SolveCubicMaxReal(a_abs*a_abs + 2.0*Q*Q - 3.0*M*M, 2.0*Q*Q*M + 2.0*M*a_abs*a_abs - 2.0*M*M*M);
                    float SinOF_Stat = sqrt(max(0.0, (2.0*r_p*(r_p*r_p + a_abs*a_abs))/(r_p - M))) * sqrt(max(0.0, CameraStartR*CameraStartR - 2.0*M*CameraStartR + a_abs*a_abs + Q*Q)) / (CameraStartR*CameraStartR + a_abs*a_abs);
                    float AngleOF = GetDropFrameAngle(SinOF_Stat, sqrt(max(0.0, 1.0 - SinOF_Stat * SinOF_Stat)), CameraStartR, M, Q, a_abs, iObserverMode);
                    float LatFactor = abs(X.y) / length(X.xyz);
                    if (LatFactor > 0.99999) { if (RayAngle < AngleOF * SHADOW_SIZE_MULTIPLIER) bHitShadow = true; }
                    else {
                        float u_A = SolveQuarticU(M, Q, a_abs, -1.0, true); float r_A_rad = (u_A * u_A + Q*Q) / M;
                        float u_B = SolveQuarticU(M, Q, a_abs, 1.0, true); float r_B_rad = (u_B * u_B + Q*Q) / M;
                        float safe_a = max(1e-5, a_abs);
                        float xi_A = (r_A_rad * r_A_rad * (3.0 * M - r_A_rad) - a_abs*a_abs * (M + r_A_rad) - 2.0 * Q*Q * r_A_rad) / max(1e-9, safe_a * (r_A_rad - M));
                        float xi_B = (r_B_rad * r_B_rad * (3.0 * M - r_B_rad) - a_abs*a_abs * (M + r_B_rad) - 2.0 * Q*Q * r_B_rad) / max(1e-9, safe_a * (r_B_rad - M));
                        float g_tt_stat = -(1.0 - (2.0 * M * CameraStartR - Q*Q) / (CameraStartR * CameraStartR));
                        float gtphi_stat = -a_abs * (2.0 * M * CameraStartR - Q*Q) / (CameraStartR * CameraStartR); 
                        float InvSqrtD = 1.0 / sqrt(max(1e-9, gtphi_stat * gtphi_stat - g_tt_stat * ((CameraStartR * CameraStartR) + a_abs*a_abs + (2.0 * M * CameraStartR - Q*Q) * a_abs*a_abs / (CameraStartR * CameraStartR))));
                        float TwistCorrection = safe_a * CameraStartR / max(1e-5, CameraStartR*CameraStartR - 2.0*M*CameraStartR + a_abs*a_abs + Q*Q);
                        
                        float AngleOA0 = GetDropFrameAngle(abs((xi_A + TwistCorrection) * g_tt_stat + gtphi_stat) * InvSqrtD, sqrt(max(0.0, 1.0 - pow(abs((xi_A + TwistCorrection) * g_tt_stat + gtphi_stat) * InvSqrtD, 2.0))), CameraStartR, M, Q, a_abs, iObserverMode);
                        float AngleOB0 = GetDropFrameAngle(abs((xi_B + TwistCorrection) * g_tt_stat + gtphi_stat) * InvSqrtD, sqrt(max(0.0, 1.0 - pow(abs((xi_B + TwistCorrection) * g_tt_stat + gtphi_stat) * InvSqrtD, 2.0))), CameraStartR, M, Q, a_abs, iObserverMode);
                        float AngleOE0 = GetDropFrameAngle(abs(((1.6666-2.0/CameraStartR) * safe_a + TwistCorrection) * g_tt_stat + gtphi_stat) * InvSqrtD, sqrt(max(0.0, 1.0 - pow(abs(((1.6666-2.0/CameraStartR) * safe_a + TwistCorrection) * g_tt_stat + gtphi_stat) * InvSqrtD, 2.0))), CameraStartR, M, Q, a_abs, iObserverMode);
                        
                        float AngleOA = mix(AngleOA0, AngleOF, clamp(tan(LatFactor*1.48)/10.98338,0.0,1.0));
                        float AngleOB = mix(AngleOB0, AngleOF, pow(LatFactor, 2.5));
                        float AngleEC = mix(GetShadowHalfAngleRN(CameraStartR, M, Q, iObserverMode), AngleOF, pow(LatFactor, 0.75));
                        float AngleOE = mix(AngleOE0, 0.0, pow(LatFactor, 6.0));

                        float AberrationShift = (iObserverMode == 1) ? 2.0*0.6666*a_abs*GetDropFrameAngle(abs(gtphi_stat) * InvSqrtD * sqrt(max(0.0, 1.0 - LatFactor * LatFactor)), sqrt(max(0.0, 1.0 - pow(abs(gtphi_stat) * InvSqrtD * sqrt(max(0.0, 1.0 - LatFactor * LatFactor)), 2.0))), CameraStartR, M, Q, a_abs, iObserverMode) : 0.0;
                        
                        vec3 ScreenUp = normalize(vec3(0.0, 1.0, 0.0) - dot(vec3(0.0, 1.0, 0.0), ToCenterDir) * ToCenterDir);
                        float dx = dot(normalize(RayDir - dot(RayDir, ToCenterDir) * ToCenterDir), cross(ToCenterDir, ScreenUp)) * RayAngle - ((abs(a) < 1e-9 ? 1.0 : sign(a)) * (AngleOE + AberrationShift));
                        
                        float CurrentHRadius = (dx * (abs(a) < 1e-9 ? 1.0 : sign(a)) > 0.0) ? max(1e-5, AngleOB - AngleOE) : (0.99*(AngleOA + AngleOE) * (1.0+25.0*clamp(1.0-((CameraStartR-30.0)/(80.0-30.0)),0.0,1.0)*(1.0-2.0*LatFactor)*(1.0-pow(abs(iQ),0.1))*pow(clamp(a_abs / CONST_M - 0.98,0.0,0.02),2.0)));
                        float CurrentVRadius = AngleEC * ((dx * (abs(a) < 1e-9 ? 1.0 : sign(a)) > 0.0) ? 1.0 : (1.0 + 0.36 * pow(clamp(abs(dx) / (0.99*(AngleOA + AngleOE)), 0.0, 1.0), 3.5) * pow(1.0 - LatFactor, 1.0) * clamp((a_abs / CONST_M - 0.9) / 0.1, 0.0, 1.0) * clamp(1.0-((CameraStartR-30.0)/(80.0-30.0)),0.0,1.0)));
                        
                        if ((dx*dx) / (CurrentHRadius*CurrentHRadius) + (dot(normalize(RayDir - dot(RayDir, ToCenterDir) * ToCenterDir), ScreenUp) * RayAngle * dot(normalize(RayDir - dot(RayDir, ToCenterDir) * ToCenterDir), ScreenUp) * RayAngle) / (CurrentVRadius*CurrentVRadius) < SHADOW_SIZE_MULTIPLIER * SHADOW_SIZE_MULTIPLIER) bHitShadow = true;
                    }
                }
                if (bHitShadow) {
                    if (!IsAccretionDiskVisible(iInterRadiusRs, iOuterRadiusRs, iThinRs, iHopper, iBrightmut, iDarkmut) && !IsJetVisible(iAccretionRate, iJetBrightmut)) {
                        res.AccumColor = vec4(0.0, 0.0, 0.0, 1.0); res.Status = 3.0; res.CurrentSign = CurrentUniverseSign; res.EscapeDir = vec3(0.0); res.FreqShift = 1.0; return res; 
                    } else if (max(iInterRadiusRs, 1.05 * EventHorizonR) > TerminationR) {
                        TerminationR = max(iInterRadiusRs, 1.05 * EventHorizonR); bDeferredShadowCulling = true; 
                    }
                }
            }
        }
    }

    float MaxStep = (iWhitehole==1) ? 1145.0 : ((bIsNakedSingularity) ? 450.0 : (150.0+300.0/(1.0+1000.0*pow(1.0-iSpin*iSpin-iQ*iQ, 2.0))));
    int Count = 0; float lastR = 0.0; bool bIntoOutHorizon = false; bool bIntoInHorizon = false; bool bEscapeOutHorizon = false; bool bEscapeInHorizon = false; int universeoffset=0;
    if(iWhitehole == 1 && !bIsNakedSingularity && CameraStartR < InnerHorizonR) universeoffset++;
    float LastDr = 0.0; int RadialTurningCounts = 0; float RayMarchPhase = RandomStep(FragUv, iTime); float ThetaInShell=0.0; bool shiftinout=false;
    vec4 LastX_ingoing; vec4 X_ingoing = X; vec4 P_cov_ingoing = P_cov; vec4 LastP_cov_ingoing = P_cov_ingoing;
    if(P_cov == vec4(0.0,0.0,0.0,-1.0)) { P_cov_ingoing.xyz = -RayDir; } else {
        if (isoutgoing) transformKerrSchild_YSpin(X_ingoing, CurrentUniverseSign, P_cov_ingoing, CONST_M, PhysicalSpinA, PhysicalQ, isoutgoing);
        LastX_ingoing = X_ingoing; LastP_cov_ingoing = P_cov_ingoing;
    }

    while (bShouldContinueMarchRay) {
        DistanceToBlackHole = length(X.xyz);
        if (DistanceToBlackHole > RaymarchingBoundary) { bShouldContinueMarchRay = false; bWaitCalBack = true; break; }
        
        KerrGeometry geo; ComputeGeometryScalars(X.xyz, PhysicalSpinA, PhysicalQ, GravityFade, CurrentUniverseSign, isoutgoing, geo);
        if (CurrentUniverseSign > 0.0 && geo.r < TerminationR && !bIsNakedSingularity && TerminationR != -1.0 && iWhitehole==0) { bShouldContinueMarchRay = false; bWaitCalBack = false; break; }
        if (Count > int(float(MaxStep)*iQuality*(1.0+0.3*iQuality))) { bShouldContinueMarchRay = false; bWaitCalBack = false; if(bIsNakedSingularity&&RadialTurningCounts <= 2) bWaitCalBack = true; break; }

        State s0; s0.X = X; s0.P = P_cov; State k1 = GetDerivativesAnalytic(s0, PhysicalSpinA, PhysicalQ, GravityFade, isoutgoing, geo);
        float CurrentDr = dot(geo.grad_r, k1.X.xyz); shiftinout = false;
        if (Count > 0 && CurrentDr * LastDr < 0.0) RadialTurningCounts++;
        if (Count==0) lastR = geo.r;

        {
            vec4 P_contra = RaiseIndex(P_cov, geo); float current_Sum = dot(abs(P_contra), vec4(1.0));
            vec4 test_X = X; vec4 test_P = P_cov; transformKerrSchild_YSpin(test_X, CurrentUniverseSign, test_P, CONST_M, PhysicalSpinA, PhysicalQ, isoutgoing);
            KerrGeometry test_geo; ComputeGeometryScalars(test_X.xyz, PhysicalSpinA, PhysicalQ, GravityFade, CurrentUniverseSign, !isoutgoing, test_geo);
            if (current_Sum > 2.0 * dot(abs(RaiseIndex(test_P, test_geo)), vec4(1.0))) {
                X = test_X; P_cov = test_P; isoutgoing = !isoutgoing; geo = test_geo;
                s0.X = X; s0.P = P_cov; k1 = GetDerivativesAnalytic(s0, PhysicalSpinA, PhysicalQ, GravityFade, isoutgoing, geo); CurrentDr = dot(geo.grad_r, k1.X.xyz); shiftinout = true;
            }
        }
        LastDr = CurrentDr;
        if(geo.r < InnerHorizonR && lastR > InnerHorizonR) bEscapeInHorizon = true;    
        if(geo.r < EventHorizonR && lastR > EventHorizonR) bEscapeOutHorizon = true;   
        if(iWhitehole == 1 && !bIsNakedSingularity && geo.r < InnerHorizonR && lastR > InnerHorizonR) universeoffset++;    

        if (iWhitehole==0 && RadialTurningCounts > 2 ) { bShouldContinueMarchRay = false; bWaitCalBack = false; break;}
        else if((bIsNakedSingularity&&RadialTurningCounts > 4) || (!bIsNakedSingularity&&universeoffset>3)) { bShouldContinueMarchRay = false; bWaitCalBack = false; break; }

        if(iGrid==0 && iWhitehole==0) {
            if(geo.r > InnerHorizonR && lastR < InnerHorizonR) bIntoInHorizon = true;   
            if(geo.r > EventHorizonR && lastR < EventHorizonR) bIntoOutHorizon = true;  
            if (CurrentUniverseSign > 0.0 && !bIsNakedSingularity) {
                if (geo.r < min(iInterRadiusRs, min(min(CameraStartR - 0.001, TerminationR + 0.2), ProgradePhotonRadius - 0.001))) {
                    if (dot(geo.grad_r, k1.X.xyz) > 1e-4) { bShouldContinueMarchRay = false; bWaitCalBack = false; break; }
                }
            }
        }
        
        float rho = length(X.xz); float DistRing = sqrt(X.y * X.y + pow(rho - abs(PhysicalSpinA), 2.0));
        float dLambda = max(0.5 * (bIsNakedSingularity ? 1.0 : mix(max(abs(iSpin),0.1), 1.0, clamp((geo.r - InnerHorizonR) / (0.5 - InnerHorizonR), 0.0, 1.0))) * (1.0 / (1.0 + 1.0 * ((PhysicalQ * PhysicalQ) / (geo.r2 + 0.01)))) * min(DistRing / (length(k1.X) + 1e-9), length(P_cov) / (length(k1.P) + 1e-15)), 1e-7); 

        vec4 LastX = X; vec4 LastP_cov=P_cov; LastX_ingoing = X_ingoing; LastP_cov_ingoing = P_cov_ingoing; 
        GravityFade = CubicInterpolate(max(min(1.0 - ( DistanceToBlackHole - 100.0) / (RaymarchingBoundary - 100.0), 1.0), 0.0));
        
        if (RaiseIndex(P_cov, geo).w > 10000.0*iQuality && !bIsNakedSingularity && CurrentUniverseSign > 0.0) { bShouldContinueMarchRay = false; bWaitCalBack = false; break; }
        StepGeodesicRK4_Optimized(X, P_cov, E_conserved, -dLambda/iQuality, PhysicalSpinA, PhysicalQ, GravityFade, CurrentUniverseSign, isoutgoing, geo, k1);
        float deltar = geo.r - lastR;

        X_ingoing = X; P_cov_ingoing = P_cov;
        if (isoutgoing) transformKerrSchild_YSpin(X_ingoing, CurrentUniverseSign, P_cov_ingoing, CONST_M, PhysicalSpinA, PhysicalQ, isoutgoing);
        
        vec3 StepVec_ingoing = X_ingoing.xyz - LastX_ingoing.xyz; float ActualStepLength_ingoing = length(StepVec_ingoing);
        if( geo.r < 1.6 + pow(abs(iSpin), 0.666666)) ThetaInShell += ActualStepLength_ingoing / (0.5*lastR + 0.5*geo.r) / (1.0 + 1000.0*pow(deltar / max(ActualStepLength_ingoing, 1e-9), 2.0)) * clamp(1.0 + iBoostRot * dot(-StepVec_ingoing, vec3(X_ingoing.z, 0.0, -X_ingoing.x)) / ActualStepLength_ingoing / length(X_ingoing.xz) * clamp(iSpin, -1.0, 1.0), 0.0, 2.0) * clamp(11.0 - 10.0*(iSpin*iSpin + iQ*iQ), 0.0, 1.0);
        lastR = geo.r;
        
        if (LastX.y * X.y < 0.0) { if (length(mix(LastX.xz, X.xz, LastX.y / (LastX.y - X.y))) < abs(PhysicalSpinA)) CurrentUniverseSign *= -1.0; }

        if (CurrentUniverseSign > 0.0 && iBlackHoleMassSol > 0.0 && int(33+iInWhichUniverse-universeoffset)%3==0) {
           if(IsAccretionDiskVisible(iInterRadiusRs, iOuterRadiusRs, iThinRs, iHopper, iBrightmut, iDarkmut)) Result = DiskColor(Result, X, LastX, P_cov, LastP_cov, E_conserved, iInterRadiusRs, iOuterRadiusRs, iThinRs, iHopper, iBrightmut, iDarkmut, iReddening, iSaturation, DiskArgument, iBlackbodyIntensityExponent, iRedShiftColorExponent, iRedShiftIntensityExponent, PeakTemperature, ShiftMax, PhysicalSpinA, PhysicalQ, isoutgoing, ThetaInShell, RayMarchPhase, WP_CamX, WP_CamY, StokesQU); 
           if(IsJetVisible(iAccretionRate, iJetBrightmut)) Result = JetColor(Result, X, LastX, P_cov, LastP_cov, E_conserved, iInterRadiusRs, iOuterRadiusRs, iJetRedShiftIntensityExponent, iJetBrightmut, iReddening, iJetSaturation, iAccretionRate, iJetShiftMax, clamp(PhysicalSpinA, -0.049, 0.049), PhysicalQ, isoutgoing, RayMarchPhase); 
        }

        if(iGrid==1) Result = GridColor(Result, X, LastX, P_cov, E_conserved, PhysicalSpinA, PhysicalQ, isoutgoing, CurrentUniverseSign);
        else if(iGrid==2) Result = GridColorSimple(Result, X, LastX, PhysicalSpinA, PhysicalQ, isoutgoing, CurrentUniverseSign);

        if (Result.a > 0.99) { bShouldContinueMarchRay = false; bWaitCalBack = false; break; }
        Count++;
    }

    res.CurrentSign = CurrentUniverseSign; res.AccumColor = Result;
    
    float PolIntensity = length(StokesQU);
    if (iPolarization==1) {
        res.AccumColor = vec4(clamp(abs(fract((0.5 * atan(StokesQU.y, StokesQU.x) + kPi/2.0) / kPi + vec3(3.0, 2.0, 1.0)/3.0) * 6.0 - 3.0) - 1.0, 0.0, 1.0) * PolIntensity, Result.a);
    } else if (iPolarization==2) {
        res.AccumColor.rgb *= 0.5 + 0.5 * (StokesQU.x * cos(2.0 * iPolarizationAngle) + StokesQU.y * sin(2.0 * iPolarizationAngle)) / max(PolIntensity, 1e-12);
    }

    if (bDeferredShadowCulling && !bIsNakedSingularity) {
        if (length(X.xyz) <= TerminationR + 0.1 || !bShouldContinueMarchRay) { res.AccumColor.a = 1.0; res.Status = 3.0; return res; }
    }

    if (Result.a > 0.99) { res.Status = 3.0; res.EscapeDir = vec3(0.0); res.FreqShift = 0.0; } 
    else if (bWaitCalBack) {
        KerrGeometry geo_sky; ComputeGeometryScalars(X_ingoing.xyz, PhysicalSpinA, PhysicalQ, GravityFade, CurrentUniverseSign, false, geo_sky);
        res.EscapeDir = LocalToWorldRot * normalize(-RaiseIndex(P_cov_ingoing, geo_sky).xyz);
        if (DistanceToBlackHole > RaymarchingBoundary ) res.EscapeDir = LocalToWorldRot * normalize(-P_cov_ingoing.xyz);
        if (any(isnan(res.EscapeDir)) || any(isinf(res.EscapeDir))) res.EscapeDir = LocalToWorldRot * normalize(RayDir);
        res.FreqShift = clamp(1.0 / max(1e-14, abs(E_conserved)), 1.0/iBackShiftMax, iBackShiftMax);
        
        if (CurrentUniverseSign > 0.0) res.Status = 1.0; else res.Status = 2.0; 
        if (iWhitehole == 1 && !bIsNakedSingularity && universeoffset>0) {
            if (res.Status == 1.0) res.Status = 1.0+3.0*float(universeoffset);
            else if (res.Status == 2.0) res.Status = 2.0+3.0*float(universeoffset);
        }
    } else { res.Status = 0.0; res.EscapeDir = vec3(0.0); res.FreqShift = 0.0; }
    res.Status += (E_conserved >= 0.0) ? 0.0 : 0.2;    

    return res;
}
float sdLineTopMap(vec2 p, vec2 a, vec2 b) {
    vec2 pa = p - a, ba = b - a;
    float h = clamp(dot(pa, ba)/max(dot(ba, ba), 1e-6), 0.0, 1.0);
    return length(pa - ba * h);
}
vec4 RenderTopologyMap(vec2 uv, vec3 camPos, vec3 camDir) {
    // 画布设置：使用屏幕相对 UV 进行映射，确保无视实际渲染分辨率正确停靠在右上角
    vec2 canvas_rel_sz = vec2(0.28, 0.25) * 1.0; // 占用屏幕长宽的比例，受配置项控制
    vec2 canvas_rel_p0 = vec2(0.98 - canvas_rel_sz.x, 0.98 - canvas_rel_sz.y);
    
    if (uv.x < canvas_rel_p0.x || uv.x > canvas_rel_p0.x + canvas_rel_sz.x ||
        uv.y < canvas_rel_p0.y || uv.y > canvas_rel_p0.y + canvas_rel_sz.y) {
        return vec4(0.0);
    }

    // 映射到一个固定大小的虚拟内部坐标系进行计算
    vec2 canvas_sz = vec2(600.0, 300.0);
    vec2 fragCoord = (uv - canvas_rel_p0) / canvas_rel_sz * canvas_sz;
    vec2 canvas_p0 = vec2(0.0);

    // 提取物理参数
    float M = CONST_M;
    float a = iSpin*0.5;
    float Q = iQ*0.5;
    float a2 = a * a;
    float Q2 = Q * Q;

    float camDist = length(camPos);
    float displayRadius = max(1.5 * M, camDist * 1.2);
    float halfWidth = canvas_sz.x * 0.5;
    float scale = min(halfWidth, canvas_sz.y) / (2.0 * displayRadius);

    vec2 centerSide = canvas_p0 + vec2(0.0, canvas_sz.y * 0.5); // 左侧图左边不留空位，中轴直接对齐左边界
    vec2 centerTop = canvas_p0 + vec2(halfWidth * 1.5, canvas_sz.y * 0.5);

    // 背景颜色
    vec4 color = vec4(15.0/255.0, 20.0/255.0, 30.0/255.0, 0.9);

    // 辅助网格和标题 (简单直线)
    vec4 axisColor = vec4(100.0/255.0, 100.0/255.0, 100.0/255.0, 0.6);
    if (abs(fragCoord.y - centerSide.y) < 1.0 && fragCoord.x > centerSide.x && fragCoord.x < canvas_p0.x + halfWidth) color = mix(color, axisColor, axisColor.a);
    if (abs(fragCoord.x - centerSide.x) < 1.0) color = mix(color, axisColor, axisColor.a);
    if (abs(fragCoord.y - centerTop.y) < 1.0 && fragCoord.x > canvas_p0.x + halfWidth) color = mix(color, axisColor, axisColor.a);
    if (abs(fragCoord.x - centerTop.x) < 1.0) color = mix(color, axisColor, axisColor.a);
    if (abs(fragCoord.x - (canvas_p0.x + halfWidth)) < 1.5) color = mix(color, vec4(0.8, 0.8, 0.8, 1.0), 1.0); // 视图分隔线

    // 几何边界求解
    float delta_disc = M*M - a2 - Q2;
    bool isNaked = (delta_disc < 0.0);
    float r_out = isNaked ? 0.0 : M + sqrt(delta_disc);
    float r_in  = isNaked ? 0.0 : M - sqrt(delta_disc);

    // 1. 左图 Side View (Y-X Plane)
    if (fragCoord.x > centerSide.x && fragCoord.x < canvas_p0.x + halfWidth) {
        vec2 localP = (vec2(fragCoord.x, fragCoord.y) - centerSide) / vec2(scale, -scale);
        float X = localP.x; // rho
        float Y = localP.y; // z
        
        // 求该像素在 Kerr-Schild 坐标中的解析 r
        float rho2 = X * X;
        float y2 = Y * Y;
        float b_ks = rho2 + y2 - a2;
        float det = sqrt(b_ks * b_ks + 4.0 * a2 * y2);
        float r2 = (b_ks >= 0.0) ? 0.5 * (b_ks + det) : (2.0 * a2 * y2) / max(1e-20, det - b_ks);
        float r_px = sqrt(max(0.0, r2));

        float cosT = Y / max(1e-9, r_px);
        float cosT2 = cosT * cosT;
        float sinT2 = max(0.0, 1.0 - cosT2);

        // 静界 (Ergosphere)
        float ergo_disc = M*M - a2*cosT2 - Q2;
        if (ergo_disc >= 0.0) {
            float r_ergo_out = M + sqrt(ergo_disc);
            float r_ergo_in  = M - sqrt(ergo_disc);
            float dr_out = abs(r_px - r_ergo_out) * scale;
            float dr_in  = abs(r_px - r_ergo_in) * scale;
            if (dr_out < 1.5) color = mix(color, vec4(150.0/255.0, 1.0, 150.0/255.0, 0.8), 1.0 - smoothstep(0.5, 1.5, dr_out));
            if (dr_in < 1.5)  color = mix(color, vec4(1.0, 200.0/255.0, 50.0/255.0, 0.8), 1.0 - smoothstep(0.5, 1.5, dr_in));
        }

        // 视界 (Horizons)
        if (!isNaked) {
            float dr_out = abs(r_px - r_out) * scale;
            float dr_in  = abs(r_px - r_in) * scale;
            if (dr_out < 2.0) color = mix(color, vec4(1.0, 80.0/255.0, 80.0/255.0, 1.0), 1.0 - smoothstep(1.0, 2.0, dr_out));
            if (dr_in < 2.0)  color = mix(color, vec4(80.0/255.0, 125.0/255.0, 1.0, 1.0), 1.0 - smoothstep(1.0, 2.0, dr_in));
        }

        // CTC 边界 (以多项式隐函数零点逼近)
        float G = (r2 + a2) * (r2 + a2*cosT2) + a2*sinT2*(2.0*M*r_px - Q2);
        float dGdr = 2.0*r_px*(r2+a2*cosT2) + (r2+a2)*2.0*r_px + a2*sinT2*2.0*M; // 近似导数
        float dr_ctc = abs(G / max(1e-5, dGdr)) * scale;
        if (dr_ctc < 1.5) color = mix(color, vec4(200.0/255.0, 100.0/255.0, 1.0, 0.1), 1.0 - smoothstep(0.5, 1.5, dr_ctc));

        // 奇环 (Singularity)
        if (length(localP - vec2(abs(a), 0.0)) * scale < 4.0) color = mix(color, vec4(1.0, 0.0, 1.0, 1.0), 1.0);
    }

    // 2. 右图 Top View (X-Z Plane)
    if (fragCoord.x > canvas_p0.x + halfWidth) {
        vec2 localP = (vec2(fragCoord.x, fragCoord.y) - centerTop) / vec2(scale, -scale);
        float rho = length(localP);
        
        float eq_disc = M*M - Q2;
        if (eq_disc >= 0.0) {
            float R_ergo_out = sqrt(pow(M + sqrt(eq_disc), 2.0) + a2);
            float R_ergo_in  = sqrt(pow(M - sqrt(eq_disc), 2.0) + a2);
            if (abs(rho - R_ergo_out)*scale < 1.5) color = mix(color, vec4(150.0/255.0, 1.0, 150.0/255.0, 0.8), 1.0 - smoothstep(0.5, 1.5, abs(rho - R_ergo_out)*scale));
            if (abs(rho - R_ergo_in)*scale < 1.5)  color = mix(color, vec4(1.0, 200.0/255.0, 50.0/255.0, 0.8), 1.0 - smoothstep(0.5, 1.5, abs(rho - R_ergo_in)*scale));
        }

        if (!isNaked) {
            float R_out = sqrt(r_out*r_out + a2);
            float R_in  = sqrt(r_in*r_in + a2);
            if (abs(rho - R_out)*scale < 2.0) color = mix(color, vec4(1.0, 100.0/255.0, 100.0/255.0, 1.0), 1.0 - smoothstep(1.0, 2.0, abs(rho - R_out)*scale));
            if (abs(rho - R_in)*scale < 2.0)  color = mix(color, vec4(100.0/255.0, 150.0/255.0, 1.0, 1.0), 1.0 - smoothstep(1.0, 2.0, abs(rho - R_in)*scale));
        }

        // 奇环 (Singularity)
        if (abs(rho - abs(a))*scale < 2.0) color = mix(color, vec4(1.0, 0.0, 1.0, 1.0), 1.0 - smoothstep(1.0, 2.0, abs(rho - abs(a))*scale));
    }

    // 3. 相机坐标与朝向点绘制
    vec4 camColor = vec4(1.0, 1.0, 1.0, 1.0);
    float rho_cam = sqrt(camPos.x*camPos.x + camPos.z*camPos.z);
    float drho = (rho_cam > 1e-6) ? ((camPos.x*camDir.x + camPos.z*camDir.z)/rho_cam) : length(camDir.xz);
    float camLineLen = max(1.5 * M, camDist * 0.15);

    // 左图的线与点
    vec2 ptCamS = centerSide + vec2(rho_cam, -camPos.y) * scale;
    vec2 ptDirS = centerSide + vec2(rho_cam + drho*camLineLen, -(camPos.y + camDir.y*camLineLen)) * scale;
    if (fragCoord.x < canvas_p0.x + halfWidth && fragCoord.x > centerSide.x) {
        if (sdLineTopMap(fragCoord, ptCamS, ptDirS) < 2.0) color = mix(color, camColor, 1.0);
        if (length(fragCoord - ptCamS) < 4.0) color = mix(color, camColor, 1.0);
    }

    // 右图的线与点
    vec2 ptCamT = centerTop + vec2(camPos.x, camPos.z) * scale;
    vec2 ptDirT = centerTop + vec2(camPos.x + camDir.x*camLineLen, camPos.z + camDir.z*camLineLen) * scale;
    if (fragCoord.x > canvas_p0.x + halfWidth) {
        if (sdLineTopMap(fragCoord, ptCamT, ptDirT) < 2.0) color = mix(color, camColor, 1.0);
        if (length(fragCoord - ptCamT) < 4.0) color = mix(color, camColor, 1.0);
    }

    return color;
}
// =============================================================================
// SECTION 9: mainImage (Shadertoy 入口)
// =============================================================================

void mainImage( out vec4 FragColor, in vec2 FragCoord )
{
    vec2 iResolution = iResolution.xy;
    vec2 Uv = FragCoord.xy / iResolution.xy;
    
    // 读取 Buffer B 相机数据
    int iBufWidth = int(iChannelResolution[2].x);
    // 注意：假设 Buffer B 绑定在 iChannel2（视你的 Shadertoy 纹理槽设置而定）
    vec3 CamPosWorld   = texelFetch(iChannel2, ivec2(iBufWidth - 3, 0), 0).xyz;
    vec3 CamRightWorld = texelFetch(iChannel2, ivec2(iBufWidth - 2, 0), 0).xyz;
    vec3 CamUpWorld    = texelFetch(iChannel2, ivec2(iBufWidth - 1, 0), 0).xyz;
    float iUniverseSign = texelFetch(iChannel2, ivec2(iBufWidth - 6, 0), 0).y;
    
    if (iUniverseSign == 0.0) iUniverseSign = 1.0;
    if (iFrame <= 5 || length(CamRightWorld) < 0.01) {
        CamPosWorld = vec3(-2.0, -3.6, 22.0); 
        vec3 fwd = vec3(0.0, 0.15, -1.0);
        CamRightWorld = normalize(cross(fwd, vec3(-0.5, 1.0, 0.0)));
        CamUpWorld    = normalize(cross(CamRightWorld, fwd));
    }
    vec3 CamBackWorld = normalize(cross(CamRightWorld, CamUpWorld));
    
    mat3 CamRotMat = mat3(CamRightWorld, CamUpWorld, CamBackWorld);
    mat4 iInverseCamRot = mat4(CamRotMat); 

    vec3 RelPos = transpose(CamRotMat) * (-CamPosWorld);
    vec4 iBlackHoleRelativePosRs = vec4(RelPos, 0.0);
    
    vec3 DiskNormalWorld = vec3(0.0, 1.0, 0.0);
    vec3 DiskTangentWorld = vec3(1.0, 0.0, 0.0);
    vec4 iBlackHoleRelativeDiskNormal = vec4(transpose(CamRotMat) * DiskNormalWorld, 0.0);
    vec4 iBlackHoleRelativeDiskTangen = vec4(transpose(CamRotMat) * DiskTangentWorld, 0.0);

    vec2 Jitter = vec2(RandomStep(Uv, fract(iTime * 1.0 + 0.5)), RandomStep(Uv, fract(iTime * 1.0))) / iResolution;

    TraceResult res = TraceRay(Uv + 0.5 * Jitter, iResolution,
                               iInverseCamRot, iBlackHoleRelativePosRs,
                               iBlackHoleRelativeDiskNormal, iBlackHoleRelativeDiskTangen,
                               iUniverseSign);

    vec4 FinalColor = res.AccumColor;
    float CurrentStatus = res.Status;
    vec3 CurrentDir = res.EscapeDir;
    float CurrentShift = res.FreqShift;

    // 对背景进行采样（使用 Shadertoy 星空或雨矩阵）
    if (CurrentStatus > 0.5 && CurrentStatus < 20.0 && CurrentStatus != 3.0) 
    {
        vec4 Bg = SampleBackground(CurrentDir, CurrentShift, CurrentStatus);
        FinalColor += 0.9999 * Bg * vec4(pow((1.0 - FinalColor.a),1.0+0.3*(1.0-1.0)),pow((1.0 - FinalColor.a),1.0+0.3*(3.0-1.0)),pow((1.0 - FinalColor.a),1.0+0.3*(6.0-1.0)),1.0);
    }

    FinalColor = ApplyToneMapping(FinalColor, CurrentShift);
    vec3 mapCamDir = normalize((iInverseCamRot * vec4(0.0, 0.0, -1.0, 0.0)).xyz);
    // 渲染拓扑UI并覆盖 (使用基于真实像素分辨率归一化的屏幕空间坐标)
    vec2 screenUV = Uv;
    vec4 mapCol = RenderTopologyMap(screenUV, vec3(CamPosWorld.x,-CamPosWorld.y,-CamPosWorld.z), vec3(mapCamDir.x,-mapCamDir.y,-mapCamDir.z));
    FinalColor.rgb = mix(FinalColor.rgb, mapCol.rgb, mapCol.a);
    FinalColor.a = mix(FinalColor.a, 1.0, mapCol.a);
    vec4 PrevColor = vec4(0.0);
    if(iFrame > 0) {
        PrevColor = texelFetch(iHistoryTex, ivec2(FragCoord.xy), 0);
    }
    
    FragColor = (iBlendWeight) * FinalColor + (1.0 - iBlendWeight) * PrevColor;
}