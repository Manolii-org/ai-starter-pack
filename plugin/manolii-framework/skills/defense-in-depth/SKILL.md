# Skill: Defense in Depth

## Description

Security review patterns for hardening application code at every layer.

## Activation

- **Trigger:** When editing API routes, auth flows, webhook handlers, database queries, or any code handling user input
- **Always active:** No — activated by context

## Allowed Tools

Read, Glob, Grep

## Instructions

Apply these security patterns when reviewing or writing code:

### Input Validation at Boundaries

- Every API route must validate input with Zod schemas
- Validate at the boundary, trust internally
- Never pass raw `req.body` or `req.query` to business logic
- Parse, don't validate: `const data = schema.parse(input)` not `if (isValid(input))`

```typescript
// GOOD
const schema = z.object({ email: z.string().email(), name: z.string().min(1).max(100) });
const data = schema.parse(await req.json());

// BAD
const { email, name } = await req.json();
```

### Parameterized Queries

- Never interpolate user input into SQL strings
- Use parameterized queries or ORM methods exclusively
- If using raw SQL, use `sql` tagged template literals

```typescript
// GOOD
const user = await db.select().from(users).where(eq(users.id, userId));

// BAD
const user = await db.execute(`SELECT * FROM users WHERE id = '${userId}'`);
```

### Authentication Checks

- Every API route must have a security marker comment at the top:
  - `// PUBLIC:` — no auth required
  - `// USER:` — requires authenticated user
  - `// ADMIN:` — requires admin role
  - `// WEBHOOK:` — requires signature verification
- Check auth BEFORE any business logic
- Use middleware for repeated auth patterns

### CSRF Protection

- State-mutating endpoints (POST, PUT, DELETE) must verify origin
- Use framework-provided CSRF tokens where available
- Webhooks: verify signature headers (Stripe, GitHub, etc.)

### Rate Limiting

- All public endpoints should have rate limiting
- Use progressive limits: auth endpoints stricter than read endpoints
- Return `429 Too Many Requests` with `Retry-After` header

### Secret Management

- Never hardcode secrets — all credentials from environment variables
- Never log secrets, tokens, or API keys
- Mask PII in logs (emails, phone numbers)
- Use `AbortSignal.timeout()` on all external HTTP calls

### External Calls

```typescript
// GOOD — timeout prevents serverless function hanging
const response = await fetch(url, { signal: AbortSignal.timeout(5000) });

// BAD — no timeout, could hang until serverless limit
const response = await fetch(url);
```
