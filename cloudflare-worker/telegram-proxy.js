/**
 * Telegram Bot API 反代 Worker
 *
 * 部署方式：
 * 1. Cloudflare Dashboard → Workers & Pages → 创建 Worker
 * 2. 粘贴本文件代码 → 保存
 * 3. 设置 → 触发器 → 自定义域名 → 添加 api.il7510n.dpdns.org
 *
 * 路由：api.il7510n.dpdns.org/*
 */

export default {
  async fetch(request, env, ctx) {
    const url = new URL(request.url);
    const path = url.pathname;

    // 只代理 /bot 开头的请求（Bot API 请求）
    if (!path.startsWith('/bot')) {
      return new Response('Not found', { status: 404 });
    }

    const tgUrl = 'https://api.telegram.org' + path + url.search;

    const tgRequest = new Request(tgUrl, {
      method: request.method,
      headers: request.headers,
      body: request.body,
    });

    const response = await fetch(tgRequest);

    // 透传响应
    const newHeaders = new Headers(response.headers);
    newHeaders.set('Access-Control-Allow-Origin', '*');

    return new Response(response.body, {
      status: response.status,
      statusText: response.statusText,
      headers: newHeaders,
    });
  },
};
