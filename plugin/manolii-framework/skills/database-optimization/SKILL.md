# Skill: Database Optimization

## Description

Query patterns and database best practices for performance and safety.

## Activation

- **Trigger:** When writing or reviewing database queries, ORM code, migrations, or data access layers
- **Always active:** No — activated by context

## Allowed Tools

Read, Glob, Grep

## Instructions

### Explicit Column Selection

```typescript
// GOOD — only fetch what you need
const users = await db.select({ id: users.id, name: users.name }).from(users);

// BAD — fetches all columns including blobs, unused fields
const users = await db.select().from(users);
```

Exception: `SELECT *` is acceptable in migrations, admin tools, and when you genuinely need all columns.

### Limit on User-Facing Queries

```typescript
// GOOD — bounded result set
const items = await db.select().from(products).limit(50).offset(page * 50);

// BAD — could return millions of rows
const items = await db.select().from(products);
```

Every user-facing query MUST have a `.limit()`. Internal batch processing may use cursor-based pagination instead.

### Transactions for Multi-Table Mutations

```typescript
// GOOD — atomic operation
await db.transaction(async (tx) => {
  await tx.insert(orders).values(order);
  await tx.update(inventory).set({ stock: sql`stock - ${quantity}` }).where(eq(inventory.productId, productId));
});

// BAD — partial failure leaves inconsistent state
await db.insert(orders).values(order);
await db.update(inventory).set({ stock: sql`stock - ${quantity}` }).where(eq(inventory.productId, productId));
```

### Connection Pool Awareness

- Serverless: use connection poolers (PgBouncer, Neon pooler, Supabase pooler)
- Max connections: respect pool limits (typically 10-20 for serverless)
- Always release connections — use `try/finally` or framework-managed pools
- Prefer short-lived queries over long-running transactions

### Index Usage

- Add indexes for columns used in `WHERE`, `JOIN`, `ORDER BY` clauses
- Composite indexes: put high-cardinality columns first
- Check query plans with `EXPLAIN ANALYZE` for slow queries
- Don't over-index: each index slows writes

### Common Anti-Patterns

- **N+1 queries:** Fetching a list then querying each item individually. Use JOINs or `WHERE IN`.
- **Missing pagination:** Always paginate list endpoints.
- **Unbounded IN clauses:** `WHERE id IN (...)` with thousands of values. Use temp tables or batch.
- **Implicit type coercion:** Ensure query parameter types match column types.
- **Missing null handling:** `WHERE col = NULL` never matches. Use `IS NULL`.
