import { test } from '@e2e-dev/web';
import { expect } from 'e2e';

// Supabase example: sign in by magic-link token exchange (no password fill) so
// screenshots stay usable. Isolated stacks only — BB_SUPABASE_SECRET_KEY must be
// the local/disposable project's key. Adapt the exchange route and the
// signed-in assertion to the app under test.
const SUPABASE_URL = process.env.BB_SUPABASE_URL ?? 'http://localhost:54321';
const KEY = process.env.BB_SUPABASE_SECRET_KEY ?? '';

// session name -> synthetic seed account
const ACCOUNTS: Record<string, string> = JSON.parse(process.env.BB_ACCOUNTS ?? '{}');

async function tokenHash(email: string): Promise<string> {
  const res = await fetch(`${SUPABASE_URL}/auth/v1/admin/generate_link`, {
    method: 'POST',
    headers: { apikey: KEY, Authorization: `Bearer ${KEY}`, 'Content-Type': 'application/json' },
    body: JSON.stringify({ type: 'magiclink', email }),
  });
  if (!res.ok) throw new Error(`generate_link ${res.status}`);
  const body = (await res.json()) as { hashed_token?: string; properties?: { hashed_token?: string } };
  const hash = body.hashed_token ?? body.properties?.hashed_token;
  if (!hash) throw new Error('no hashed_token');
  return hash;
}

for (const [name, email] of Object.entries(ACCOUNTS)) {
  test.setup(`sign in ${name}`, { sessions: [name] }, async ({ app, browser, screen, session }) => {
    await app.open(`/auth/confirm/exchange?token_hash=${await tokenHash(email)}&type=magiclink&next=/home`);
    await browser.waitForURL(/\/home/, { timeout: 30000 });
    await expect(screen.getByRole('button', /sign out/i)).toBeVisible({ timeout: 15000 });
    await session.save(name);
  });
}
