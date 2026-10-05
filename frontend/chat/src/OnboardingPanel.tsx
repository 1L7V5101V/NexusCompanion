/**
 * 一次性人设设置面板（C9 onboarding）。
 *
 * 交互极简：两种来源——选择管理员 Persona 模板（展开正文预览后一键采用），
 * 或切换到自由文本编辑（identity / personality_rules / self_model 三块，
 * 沿用当前单体语义）。提交成功后服务端保存 tenant 快照并锁定，
 * 本面板随后不再出现（spec「提交后用户侧无修改入口」——应用侧也不提供
 * 任何再次进入 onboarding 的入口）。
 */

import { useEffect, useState, type FormEvent } from "react";
import {
  fetchPersonaStatus,
  fetchPersonaTemplates,
  submitOnboarding,
  type PersonaStatus,
  type PersonaTemplate,
} from "./persona";

export function OnboardingPanel({ onCompleted }: { onCompleted: () => void }) {
  const [status, setStatus] = useState<PersonaStatus | null>(null);
  const [templates, setTemplates] = useState<PersonaTemplate[]>([]);
  const [selected, setSelected] = useState<string | null>(null);
  const [mode, setMode] = useState<"template" | "custom">("template");
  const [identity, setIdentity] = useState("");
  const [rules, setRules] = useState("");
  const [selfModel, setSelfModel] = useState("");
  const [submitting, setSubmitting] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    void (async () => {
      try {
        const st = await fetchPersonaStatus();
        if (cancelled) return;
        setStatus(st);
        if (st.onboarding_required) {
          setTemplates(await fetchPersonaTemplates());
        }
      } catch {
        if (!cancelled) setError("加载人设设置失败，请刷新重试。");
      }
    })();
    return () => {
      cancelled = true;
    };
  }, []);

  const pickTemplate = (id: string) => setSelected(id === selected ? null : id);

  const submit = async (e: FormEvent) => {
    e.preventDefault();
    setSubmitting(true);
    setError(null);
    try {
      if (mode === "template") {
        if (!selected) throw new Error("请先选择一个 Persona。");
        await submitOnboarding({ source: "template", template_id: selected });
      } else {
        if (!identity.trim() || !rules.trim() || !selfModel.trim()) {
          throw new Error("三块内容都需要填写。");
        }
        await submitOnboarding({
          source: "custom",
          identity,
          personality_rules: rules,
          self_model: selfModel,
        });
      }
      onCompleted();
    } catch (exc) {
      setError(exc instanceof Error ? exc.message : "提交失败，请稍后重试。");
    } finally {
      setSubmitting(false);
    }
  };

  if (!status) {
    return (
      <div className="flex h-full items-center justify-center bg-bg text-sm text-muted">
        {error ?? "正在准备人设设置…"}
      </div>
    );
  }

  return (
    <div className="flex h-full items-center justify-center overflow-y-auto bg-bg px-4 py-10">
      <form
        onSubmit={submit}
        className="w-full max-w-2xl rounded-2xl border border-white/10 bg-white/5 p-6 backdrop-blur-md sm:p-8"
      >
        <h1 className="text-lg font-medium text-fg">初次见面，设置一下「我是谁」</h1>
        <p className="mt-1 text-sm text-muted">
          选择一个 Persona，或写下你希望的样子。提交后这份设定会固定下来（之后不能在
          这里修改）；你们的相处方式会随后续对话自然演化。
        </p>

        {status.templates.length > 0 && (
          <div className="mt-5">
            <div className="flex items-center gap-3">
              <button
                type="button"
                onClick={() => setMode("template")}
                className={`text-sm ${mode === "template" ? "font-medium text-fg" : "text-muted"}`}
              >
                选择 Persona
              </button>
              <span className="text-muted">·</span>
              <button
                type="button"
                onClick={() => setMode("custom")}
                className={`text-sm ${mode === "custom" ? "font-medium text-fg" : "text-muted"}`}
              >
                自由文本
              </button>
            </div>

            {mode === "template" && (
              <div className="mt-3 space-y-2">
                {status.templates.map((t) => (
                  <div key={t.id} className="rounded-xl border border-white/10">
                    <button
                      type="button"
                      onClick={() => pickTemplate(t.id)}
                      className="flex w-full items-center justify-between px-4 py-3 text-left text-sm text-fg"
                    >
                      <span>{t.name}</span>
                      <span className="text-xs text-muted">
                        {selected === t.id ? "已选择" : "查看并采用"}
                      </span>
                    </button>
                    {selected === t.id && (
                      <div className="border-t border-white/10 px-4 py-3">
                        <TemplateDetail id={t.id} templates={templates} />
                      </div>
                    )}
                  </div>
                ))}
              </div>
            )}
          </div>
        )}

        {mode === "custom" && (
          <div className="mt-4 space-y-3">
            <TextField label="身份（TA 是谁）" value={identity} onChange={setIdentity} />
            <TextField
              label="行为规则（说话方式、分寸）"
              value={rules}
              onChange={setRules}
              multiline
            />
            <TextField
              label="自我模型（关系与自我认知的初始状态）"
              value={selfModel}
              onChange={setSelfModel}
              multiline
            />
          </div>
        )}

        {error && <p className="mt-3 text-sm text-red-400">{error}</p>}

        <div className="mt-6 flex justify-end">
          <button
            type="submit"
            disabled={submitting}
            className="rounded-full border border-white/15 bg-white/10 px-5 py-2 text-sm text-fg transition hover:bg-white/15 disabled:opacity-50"
          >
            {submitting ? "提交中…" : "就这样，开始吧"}
          </button>
        </div>
      </form>
    </div>
  );
}

function TemplateDetail({
  id,
  templates,
}: {
  id: string;
  templates: PersonaTemplate[];
}) {
  const tpl = templates.find((t) => t.id === id);
  if (!tpl) return <p className="text-sm text-muted">加载中…</p>;
  return (
    <div className="space-y-2 text-xs leading-relaxed text-muted">
      <p className="whitespace-pre-wrap">{tpl.identity}</p>
      <p className="whitespace-pre-wrap">{tpl.personality_rules}</p>
    </div>
  );
}

function TextField({
  label,
  value,
  onChange,
  multiline = false,
}: {
  label: string;
  value: string;
  onChange: (v: string) => void;
  multiline?: boolean;
}) {
  return (
    <label className="block">
      <span className="text-xs text-muted">{label}</span>
      {multiline ? (
        <textarea
          value={value}
          onChange={(e) => onChange(e.target.value)}
          rows={4}
          className="mt-1 w-full resize-y rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-fg outline-none focus:border-white/25"
        />
      ) : (
        <input
          value={value}
          onChange={(e) => onChange(e.target.value)}
          className="mt-1 w-full rounded-xl border border-white/10 bg-white/5 px-3 py-2 text-sm text-fg outline-none focus:border-white/25"
        />
      )}
    </label>
  );
}
