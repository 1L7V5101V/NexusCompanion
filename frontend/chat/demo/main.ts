// PulseCore demo：Abstract（fXVGDm 新移植）与 Kerr-Newman（fXV3Wm 旧移植）双变体 + 四状态切换
import { PulseCorePipeline, type PulseCoreState } from '../src/pulsecore/PulseCorePipeline';
import { AbstractPipeline } from '../src/pulsecore/abstract/AbstractPipeline';
import type { PulseCoreInstance, PulseCoreVariant } from '../src/pulsecore/PulseCore';

const host = document.getElementById('host')!;
const fpsEl = document.getElementById('fps')!;
const canvas = document.createElement('canvas');
canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;';
host.appendChild(canvas);

let variant: PulseCoreVariant = 'abstract';
let curState: PulseCoreState = 'idle';
let holeSize = 1;
let audio = 0;
let fpsLabel = '';
let pipeline: PulseCoreInstance;

function createPipeline(v: PulseCoreVariant) {
    pipeline?.dispose();
    pipeline = v === 'kerr' ? new PulseCorePipeline(canvas, { scale: 0.5 }) : new AbstractPipeline(canvas, { scale: 0.5 });
    pipeline.setState(curState);
    pipeline.setAudioEnergy(audio);
    pipeline.setHoleSize(holeSize);
    pipeline.start();
    fpsLabel = v === 'kerr' ? 'kerr · scale 0.5 · 30fps cap' : 'abstract · scale 0.5 · 60fps cap';
}

try {
    createPipeline(variant);
} catch (e) {
    canvas.style.background = 'radial-gradient(ellipse at center, #10121f 0%, #05060d 70%)';
    console.error('WebGL2 unavailable', e);
    throw e;
}

// 变体切换
document.querySelectorAll<HTMLButtonElement>('#panel button[data-variant]').forEach(btn => {
    btn.addEventListener('click', () => {
        if (btn.dataset.variant === variant) return;
        document.querySelectorAll<HTMLButtonElement>('#panel button[data-variant]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        variant = btn.dataset.variant as PulseCoreVariant;
        createPipeline(variant);
    });
});

// 状态切换按钮
document.querySelectorAll<HTMLButtonElement>('#panel button[data-state]').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll<HTMLButtonElement>('#panel button[data-state]').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        curState = btn.dataset.state as PulseCoreState;
        pipeline.setState(curState);
    });
});

// FPS 计
let frames = 0, last = performance.now();
function tick(now: number) {
    frames++;
    if (now - last >= 1000) {
        fpsEl.textContent = `${frames} fps · ${fpsLabel}`;
        frames = 0; last = now;
    }
    requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

// demo 用的音频能量模拟（真实场景由 props 传入）：
// streaming 状态下让呼吸泵随时间起伏，方便观察状态差异
setInterval(() => {
    audio = curState === 'streaming' ? 0.5 + 0.5 * Math.sin(performance.now() / 700) : 0;
    pipeline.setAudioEnergy(audio);
}, 100);

// 黑洞基础大小滑杆
const sizeInput = document.getElementById('size') as HTMLInputElement;
const sizeVal = document.getElementById('sizeVal')!;
sizeInput.addEventListener('input', () => {
    holeSize = parseFloat(sizeInput.value);
    pipeline.setHoleSize(holeSize);
    sizeVal.textContent = holeSize.toFixed(2);
});

// 供自动化验收截图用
(window as unknown as Record<string, unknown>).__pulsecore = pipeline;
(window as unknown as Record<string, unknown>).THREE = THREE;
