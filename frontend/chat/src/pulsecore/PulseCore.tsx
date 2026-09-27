// PulseCore：对外的 React 组件。挂载多 Pass 管线并桥接 AI 状态。
// 说明：管线是手动编排的多 pass 渲染（Shadertoy 语义），不走 R3F 的
// 声明式场景图——这里直接持有 three renderer，React 只负责生命周期。
import { useEffect, useRef } from 'react';
import { PulseCorePipeline, type PulseCoreState } from './PulseCorePipeline';

export interface PulseCoreProps {
    state?: PulseCoreState;
    audioEnergy?: number;
    /** 内部渲染分辨率系数（0.5 默认；低端设备可 0.35） */
    scale?: number;
    className?: string;
}

export function PulseCore({ state = 'idle', audioEnergy = 0, scale, className }: PulseCoreProps) {
    const hostRef = useRef<HTMLDivElement>(null);
    const pipelineRef = useRef<PulseCorePipeline | null>(null);

    useEffect(() => {
        const host = hostRef.current;
        if (!host) return;
        const canvas = document.createElement('canvas');
        canvas.style.cssText = 'position:absolute;inset:0;width:100%;height:100%;display:block;touch-action:none;';
        host.appendChild(canvas);

        let pipeline: PulseCorePipeline | null = null;
        try {
            pipeline = new PulseCorePipeline(canvas, scale !== undefined ? { scale } : undefined);
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
    }, [scale]);

    useEffect(() => { pipelineRef.current?.setState(state); }, [state]);
    useEffect(() => { pipelineRef.current?.setAudioEnergy(audioEnergy); }, [audioEnergy]);

    return <div ref={hostRef} className={className} style={{ position: 'absolute', inset: 0, overflow: 'hidden' }} aria-hidden="true" />;
}
