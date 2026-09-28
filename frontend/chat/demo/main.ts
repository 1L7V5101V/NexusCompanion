// PulseCore demo：Abstract（fXVGDm 新移植）与 Kerr-Newman（fXV3Wm 旧移植）双变体
// + 四状态切换 + 全参数实时调参面板（仅 Abstract）
import { PulseCorePipeline, type PulseCoreState } from '../src/pulsecore/PulseCorePipeline';
import { AbstractPipeline, type StateProfile, type WaveShape } from '../src/pulsecore/abstract/AbstractPipeline';
import type { PulseCoreInstance, PulseCoreVariant } from '../src/pulsecore/PulseCore';
import * as THREE from 'three';

const host = document.getElementById('host')!;
const fpsEl = document.getElementById('fps')!;
const advEl = document.getElementById('adv')!;
const canvas = document.createElement('canvas');
canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;';
host.appendChild(canvas);

let variant: PulseCoreVariant = 'abstract';
let curState: PulseCoreState = 'idle';
let holeSize = 0.6;
let audio = 0;
let pipeline: PulseCoreInstance;

// ---- 调参面板状态：全局参数默认值（用户定稿，2026-09-28）+ 每状态剖面覆盖 ----
const adj = {
    dynamics: { oscGainK: 0.2, audioGainK: 0.15, audioScaleK: 0.012 },
    pert: { lam: 1.45, lag: 0.33, horizonAmp: 0 },
    mouse: { offsetK: 0.01, boost: 0.35, near: 0.12, far: 0.4 },
    scale: 1.25,
    fpsCap: 85,
    feedback: 0.05,
    starGain: 1,
    starLens: 0,
    wave: 'lubdub' as WaveShape,
};
const profileOverrides: Record<PulseCoreState, Partial<StateProfile>> = {
    idle: {}, thinking: {}, streaming: {}, error: {},
};

const abstractOrNull = (): AbstractPipeline | null =>
    variant === 'abstract' && pipeline instanceof AbstractPipeline ? pipeline : null;

function createPipeline(v: PulseCoreVariant) {
    pipeline?.dispose();
    pipeline = v === 'kerr' ? new PulseCorePipeline(canvas, { scale: 0.5 }) : new AbstractPipeline(canvas, { scale: adj.scale, fpsCap: adj.fpsCap });
    const a = abstractOrNull();
    if (a) {
        (['idle', 'thinking', 'streaming', 'error'] as const).forEach(s => {
            if (Object.keys(profileOverrides[s]).length) a.setProfile(s, profileOverrides[s]);
        });
        a.setWaveShape(adj.wave);
        a.setDynamics(adj.dynamics);
        a.setPert({ lam: adj.pert.lam, lag: adj.pert.lag });
        a.setHorizonAmp(adj.pert.horizonAmp);
        a.setMouseParams(adj.mouse);
        a.setFeedback(adj.feedback);
        a.setStarGain(adj.starGain);
        a.setStarLens(adj.starLens);
    }
    pipeline.setState(curState);
    pipeline.setAudioEnergy(audio);
    pipeline.setHoleSize(holeSize);
    pipeline.start();
    updateFpsLabel();
}

function updateFpsLabel() {
    const prefix = variant === 'kerr' ? 'kerr' : 'abstract';
    fpsEl.dataset.prefix = `${prefix} · scale ${variant === 'kerr' ? 0.5 : adj.scale.toFixed(2)} · ${variant === 'kerr' ? 30 : adj.fpsCap}fps cap`;
}
fpsEl.textContent = '';

try {
    createPipeline(variant);
} catch (e) {
    canvas.style.background = 'radial-gradient(ellipse at center, #10121f 0%, #05060d 70%)';
    console.error('WebGL2 unavailable', e);
    throw e;
}

// ---- 变体切换 ----
document.querySelectorAll<HTMLButtonElement>('#panel button[data-variant]').forEach(btn => {
    btn.addEventListener('click', () => {
        if (btn.dataset.variant === variant) return;
        document.querySelectorAll<HTMLButtonElement>('#panel button[data-variant]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        variant = btn.dataset.variant as PulseCoreVariant;
        createPipeline(variant);
        advEl.classList.remove('open');
        (document.getElementById('advToggle') as HTMLButtonElement).style.display = variant === 'abstract' ? '' : 'none';
    });
});

// ---- 状态切换（顺带刷新剖面滑杆位置）----
document.querySelectorAll<HTMLButtonElement>('#panel button[data-state]').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll<HTMLButtonElement>('#panel button[data-state]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        curState = btn.dataset.state as PulseCoreState;
        pipeline.setState(curState);
        refreshProfileRows();
    });
});

// ---- FPS 计 ----
let frames = 0, last = performance.now();
function tick(now: number) {
    frames++;
    if (now - last >= 1000) {
        fpsEl.textContent = `${frames} fps · ${fpsEl.dataset.prefix ?? ''}`;
        frames = 0; last = now;
    }
    requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

// ---- 音频能量：streaming 自动模拟 或 手动滑杆 ----
let autoAudio = true;
let manualAudio = 0;
setInterval(() => {
    audio = autoAudio
        ? (curState === 'streaming' ? 0.5 + 0.5 * Math.sin(performance.now() / 700) : 0)
        : manualAudio;
    pipeline.setAudioEnergy(audio);
}, 100);

// ---- 黑洞基础大小滑杆 ----
const sizeInput = document.getElementById('size') as HTMLInputElement;
const sizeVal = document.getElementById('sizeVal')!;
sizeInput.addEventListener('input', () => {
    holeSize = parseFloat(sizeInput.value);
    pipeline.setHoleSize(holeSize);
    sizeVal.textContent = holeSize.toFixed(2);
});

// ================= 调参面板 =================
const mkRow = (parent: HTMLElement, label: string, min: number, max: number, step: number, value: number, oninput: (v: number) => void) => {
    const row = document.createElement('div');
    row.className = 'row';
    const k = document.createElement('span');
    k.className = 'k';
    k.textContent = label;
    const input = document.createElement('input');
    input.type = 'range';
    input.min = String(min);
    input.max = String(max);
    input.step = String(step);
    input.value = String(value);
    const v = document.createElement('span');
    v.className = 'v';
    const fmt = (x: number) => (step >= 1 ? String(Math.round(x)) : x.toFixed(step >= 0.01 ? 2 : 3));
    v.textContent = fmt(value);
    input.addEventListener('input', () => {
        const x = parseFloat(input.value);
        v.textContent = fmt(x);
        oninput(x);
    });
    row.append(k, input, v);
    parent.appendChild(row);
    return { input, v, fmt };
};

const mkH4 = (parent: HTMLElement, text: string, tag?: string) => {
    const h = document.createElement('h4');
    h.textContent = text;
    if (tag) {
        const t = document.createElement('span');
        t.className = 'tag';
        t.textContent = ` ${tag}`;
        h.appendChild(t);
    }
    parent.appendChild(h);
};

// —— 状态参数（编辑当前选中状态的剖面）——
mkH4(advEl, '状态参数', '· 编辑当前状态');
const profileRowKeys: (keyof StateProfile)[] = ['bpm', 'holeAmp', 'gain', 'swirl', 'flicker', 'wobble'];
const profileRowSpec: Record<keyof StateProfile, { label: string; min: number; max: number; step: number }> = {
    bpm:     { label: '脉搏 bpm', min: 0, max: 160, step: 1 },
    holeAmp: { label: '呼吸幅度', min: 0, max: 0.1, step: 0.002 },
    gain:    { label: '亮度', min: 0.3, max: 1.6, step: 0.01 },
    swirl:   { label: '盘转速', min: 0, max: 3, step: 0.05 },
    flicker: { label: '闪烁', min: 0, max: 1, step: 0.02 },
    wobble:  { label: '心律不齐', min: 0, max: 1, step: 0.05 },
};
const profileRows = {} as Record<keyof StateProfile, ReturnType<typeof mkRow>>;
for (const key of profileRowKeys) {
    const spec = profileRowSpec[key];
    profileRows[key] = mkRow(advEl, spec.label, spec.min, spec.max, spec.step, 0, x => {
        profileOverrides[curState][key] = x;
        abstractOrNull()?.setProfile(curState, { [key]: x } as Partial<StateProfile>);
    });
}
function refreshProfileRows() {
    const a = abstractOrNull();
    if (!a) return;
    const prof = a.getProfile(curState);
    for (const key of profileRowKeys) {
        const row = profileRows[key];
        row.input.value = String(prof[key]);
        row.v.textContent = row.fmt(prof[key]);
    }
}
refreshProfileRows();

// —— 心跳波形 ——
mkH4(advEl, '心跳波形');
const waveSel = document.createElement('select');
for (const [value, text] of [['continuous', '连续正弦（默认）'], ['lubdub', '双峰 lub-dub'], ['pulse', '快脉冲']] as const) {
    const opt = document.createElement('option');
    opt.value = value;
    opt.textContent = text;
    waveSel.appendChild(opt);
}
waveSel.addEventListener('change', () => {
    adj.wave = waveSel.value as WaveShape;
    abstractOrNull()?.setWaveShape(adj.wave);
});
waveSel.value = adj.wave;
advEl.appendChild(waveSel);

// —— 耦合强度 ——
mkH4(advEl, '耦合强度');
mkRow(advEl, '心跳→亮度', 0, 0.2, 0.005, adj.dynamics.oscGainK, x => { adj.dynamics.oscGainK = x; abstractOrNull()?.setDynamics({ oscGainK: x }); });
mkRow(advEl, '音频→亮度', 0, 0.5, 0.01, adj.dynamics.audioGainK, x => { adj.dynamics.audioGainK = x; abstractOrNull()?.setDynamics({ audioGainK: x }); });
mkRow(advEl, '音频→脉动', 0, 0.05, 0.001, adj.dynamics.audioScaleK, x => { adj.dynamics.audioScaleK = x; abstractOrNull()?.setDynamics({ audioScaleK: x }); });

// —— 盘响应（心跳涟漪如何向外传播）——
mkH4(advEl, '盘响应', '· 只影响心跳，不影响大小');
mkRow(advEl, '衰减长度', 0.3, 3, 0.05, adj.pert.lam, x => { adj.pert.lam = x; abstractOrNull()?.setPert({ lam: x }); });
mkRow(advEl, '传播延迟', 0, 0.5, 0.01, adj.pert.lag, x => { adj.pert.lag = x; abstractOrNull()?.setPert({ lag: x }); });
mkRow(advEl, '本体脉动', 0, 1, 0.05, adj.pert.horizonAmp, x => { adj.pert.horizonAmp = x; abstractOrNull()?.setHorizonAmp(x); });

// —— 鼠标交互 ——
mkH4(advEl, '鼠标交互');
mkRow(advEl, '视差幅度', 0, 0.4, 0.01, adj.mouse.offsetK, x => { adj.mouse.offsetK = x; abstractOrNull()?.setMouseParams({ offsetK: x }); });
mkRow(advEl, '能量增强', 0, 1, 0.05, adj.mouse.boost, x => { adj.mouse.boost = x; abstractOrNull()?.setMouseParams({ boost: x }); });
mkRow(advEl, '近距半径', 0, 0.4, 0.02, adj.mouse.near, x => { adj.mouse.near = x; abstractOrNull()?.setMouseParams({ near: x }); });
mkRow(advEl, '远距半径', 0.2, 1.5, 0.05, adj.mouse.far, x => { adj.mouse.far = x; abstractOrNull()?.setMouseParams({ far: x }); });

// —— 渲染 ——
mkH4(advEl, '渲染', '· >1 超采样抗锯齿 · scale 改动会重收敛一瞬');
mkRow(advEl, '分辨率系数', 0.25, 1.5, 0.05, adj.scale, x => { adj.scale = x; abstractOrNull()?.setScale(x); updateFpsLabel(); });
mkRow(advEl, 'FPS 上限', 15, 120, 5, adj.fpsCap, x => { adj.fpsCap = x; abstractOrNull()?.setFpsCap(x); updateFpsLabel(); });
mkRow(advEl, '时间累积', 0, 0.95, 0.05, adj.feedback, x => { adj.feedback = x; abstractOrNull()?.setFeedback(x); });
mkRow(advEl, '星空亮度', 0, 2, 0.05, adj.starGain, x => { adj.starGain = x; abstractOrNull()?.setStarGain(x); });
mkRow(advEl, '星空透镜', 0, 1, 0.05, adj.starLens, x => { adj.starLens = x; abstractOrNull()?.setStarLens(x); });

// —— 音频能量 ——
mkH4(advEl, '音频能量');
const chk = document.createElement('label');
chk.className = 'chk';
const chkBox = document.createElement('input');
chkBox.type = 'checkbox';
chkBox.checked = true;
chkBox.addEventListener('change', () => { autoAudio = chkBox.checked; });
chk.append(chkBox, document.createTextNode(' streaming 时自动模拟'));
advEl.appendChild(chk);
mkRow(advEl, '手动能量', 0, 1, 0.05, 0, x => {
    manualAudio = x;
    autoAudio = false;
    chkBox.checked = false;
});

// —— 面板开关 ——
const advToggle = document.getElementById('advToggle') as HTMLButtonElement;
advToggle.addEventListener('click', () => advEl.classList.toggle('open'));

// 供自动化验收截图用
(window as unknown as Record<string, unknown>).__pulsecore = pipeline;
(window as unknown as Record<string, unknown>).THREE = THREE;
(window as unknown as Record<string, unknown>).__adv = { adj, profileOverrides };
