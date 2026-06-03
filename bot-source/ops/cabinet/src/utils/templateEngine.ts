import { createHappCryptoLink } from '@kastov/cryptohapp';

const TEMPLATE_RE = /\{\{[A-Z0-9_]+\}\}/;

export function hasTemplates(url: string): boolean {
  return TEMPLATE_RE.test(url);
}

interface ResolveContext {
  subscriptionUrl: string;
  username?: string;
}

export function resolveTemplate(template: string, ctx: ResolveContext): string {
  let result = template;

  result = result.replace(/\{\{SUBSCRIPTION_LINK\}\}/g, ctx.subscriptionUrl);

  if (ctx.username) {
    result = result.replace(/\{\{USERNAME\}\}/g, ctx.username);
  }

  result = result.replace(/\{\{HAPP_CRYPT3_LINK\}\}/g, () => {
    return createHappCryptoLink(ctx.subscriptionUrl, 'v3', true) ?? ctx.subscriptionUrl;
  });

  result = result.replace(/\{\{HAPP_CRYPT4_LINK\}\}/g, () => {
    return createHappCryptoLink(ctx.subscriptionUrl, 'v4', true) ?? ctx.subscriptionUrl;
  });

  return result;
}
