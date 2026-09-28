// PulseCore：对外的 React 组件。挂载黑洞渲染管线并桥接 AI 状态。
// 说明：管线是手动编排的 pass 渲染（Shadertoy 语义），不走 R3F 的
// 声明式场景图——这里直接持有 three renderer，React 只负责生命周期。
import { useEffect, useRef } from 'react';
import { PulseCorePipeline, type PulseCoreState } from './PulseCorePipeline';
import { AbstractPipeline } from './abstract/AbstractPipeline';

export type PulseCoreVariant = 'abstract' | 'kerr';

/** 两种管线的公共接口（demo 与 React 组件都只依赖这一组方法） */
export interface PulseCoreInstance {
    setState(s: PulseCoreState): void;
    setAudioEnergy(e: number): void;
    setHoleSize(v: number): void;
    start(): void;
    dispose(): void;
}

export interface PulseCoreProps {
    /** 渲染变体：abstract=fXVGDm 轻量单 pass（默认），kerr=fXV3Wm 完整 5-pass */
    variant?: PulseCoreVariant;
    state?: PulseCoreState;
    audioEnergy?: number;
    /** 内部渲染分辨率系数：默认 1.25（超采样抗锯齿）；低端设备可降到 0.5–0.85 */
    scale?: number;
    /** 黑洞基础尺寸倍率：默认 0.6（用户定稿）；abstract 范围 0.3–2.0，kerr 0.2–1.3 */
    holeSize?: number;
    className?: string;
}

export function PulseCore({ variant = 'abstract', state = 'idle', audioEnergy = 0, scale, holeSize, className }: PulseCoreProps) {
    const hostRef = useRef<HTMLDivElement>(null);
    const pipelineRef = useRef<PulseCoreInstance | null>(null);

    useEffect(() => {
        const host = hostRef.current;
        if (!host) return;
        const canvas = document.createElement('canvas');
        canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;';
        host.appendChild(canvas);

        const opts = scale !== undefined ? { scale } : undefined;
        let pipeline: PulseCoreInstance | null = null;
        try {
            pipeline = variant === 'kerr' ? new PulseCorePipeline(canvas, opts) : new AbstractPipeline(canvas, opts);
            if (holeSize !== undefined) pipeline.setHoleSize(holeSize);
            pipeline.setState(state);
            pipeline.setAudioEnergy(audioEnergy);
            pipeline.start();
            pipelineRef.current = pipeline;
        } catch (e) {
            // WebGL2 不可用：保留纯色深空背景，不影响聊天功能
            canvas.style.background = 'radial-gradient(ellipse at center, #10121f 0%, #05060d 70%)';
            console.warn('[PulseCore] WebGL2 unavailable, fallback background applied.', e);
        }

        return () => {
            pipeline?.dispose();
            pipelineRef.current = null;
            canvas.remove();
        };
        // state/audioEnergy/holeSize 仅作初值，后续变化由下方 effect 增量应用
        // eslint-disable-next-line react-hooks/exhaustive-deps
    }, [scale, variant]);

    useEffect(() => { pipelineRef.current?.setState(state); }, [state, variant]);
    useEffect(() => { pipelineRef.current?.setAudioEnergy(audioEnergy); }, [audioEnergy, variant]);
    useEffect(() => { if (holeSize !== undefined) pipelineRef.current?.setHoleSize(holeSize); }, [holeSize, variant]);

    return <div ref={hostRef} className={className} style={{ position: 'absolute', inset: 0, overflow: 'hidden' }} aria-hidden="true" />;
}
