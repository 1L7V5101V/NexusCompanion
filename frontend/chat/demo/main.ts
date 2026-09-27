// PulseCore demo：完整移植的 fXV3Wm + 四状态切换，供验收用
import { PulseCorePipeline, type PulseCoreState } from '../src/pulsecore/PulseCorePipeline';

const host = document.getElementById('host')!;
const fpsEl = document.getElementById('fps')!;
const canvas = document.createElement('canvas');
canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;';
host.appendChild(canvas);

let pipeline: PulseCorePipeline;
try {
    pipeline = new PulseCorePipeline(canvas, { scale: 0.5 });
    pipeline.start();
} catch (e) {
    canvas.style.background = 'radial-gradient(ellipse at center, #10121f 0%, #05060d 70%)';
    console.error('WebGL2 unavailable', e);
    throw e;
}

// 状态切换按钮
document.querySelectorAll<HTMLButtonElement>('#panel button').forEach(btn => {
    btn.addEventListener('click', () => {
        document.querySelectorAll<HTMLButtonElement>('#panel button').forEach(b => b.classList.remove('active'));
        btn.classList.add('active');
        pipeline.setState(btn.dataset.state as PulseCoreState);
    });
});

// FPS 计
let frames = 0, last = performance.now();
function tick(now: number) {
    frames++;
    if (now - last >= 1000) {
        fpsEl.textContent = `${frames} fps · scale 0.4 · 30fps cap`;
        frames = 0; last = now;
    }
    requestAnimationFrame(tick);
}
requestAnimationFrame(tick);

// demo 用的音频能量模拟（真实场景由 props 传入）：
// streaming 状态下让呼吸泵随时间起伏，方便观察状态差异
setInterval(() => {
    const active = document.querySelector<HTMLButtonElement>('#panel button.active')?.dataset.state;
    pipeline.setAudioEnergy(active === 'streaming' ? 0.5 + 0.5 * Math.sin(performance.now() / 700) : 0);
}, 100);

// 黑洞基础大小滑杆
const sizeInput = document.getElementById('size') as HTMLInputElement;
const sizeVal = document.getElementById('sizeVal')!;
sizeInput.addEventListener('input', () => {
    const v = parseFloat(sizeInput.value);
    pipeline.setHoleSize(v);
    sizeVal.textContent = v.toFixed(2);
});

// 供自动化验收截图用
import * as THREE from 'three';
(window as unknown as Record<string, unknown>).__pulsecore = pipeline;
(window as unknown as Record<string, unknown>).THREE = THREE;
