/**
 * Persona onboarding API（C9）。与 auth.ts 同一安全边界：
 * 凭 HttpOnly Cookie（credentials: same-origin），不读写任何本地存储。
 */

export type PersonaTemplateSummary = {
  id: string;
  name: string;
};

export type PersonaStatus = {
  onboarding_required: boolean;
  templates: PersonaTemplateSummary[];
};

export type PersonaTemplate = PersonaTemplateSummary & {
  identity: string;
  personality_rules: string;
  self_model: string;
};

export type OnboardingPayload =
  | { source: "template"; template_id: string }
  | {
      source: "custom";
      identity: string;
      personality_rules: string;
      self_model: string;
    };

export async function fetchPersonaStatus(): Promise<PersonaStatus> {
  const resp = await fetch("/api/persona/status", {
    credentials: "same-origin",
    headers: { accept: "application/json" },
  });
  if (!resp.ok) throw new Error(`persona status failed: ${resp.status}`);
  return (await resp.json()) as PersonaStatus;
}

export async function fetchPersonaTemplates(): Promise<PersonaTemplate[]> {
  const resp = await fetch("/api/persona/templates", {
    credentials: "same-origin",
    headers: { accept: "application/json" },
  });
  if (resp.status === 404) return []; // 已完成 onboarding：模板正文不再可见
  if (!resp.ok) throw new Error(`persona templates failed: ${resp.status}`);
  const body = (await resp.json()) as { items: PersonaTemplate[] };
  return body.items;
}

export async function submitOnboarding(
  payload: OnboardingPayload,
): Promise<void> {
  const resp = await fetch("/api/persona/onboarding", {
    method: "POST",
    credentials: "same-origin",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!resp.ok) throw new Error(`onboarding failed: ${resp.status}`);
}
