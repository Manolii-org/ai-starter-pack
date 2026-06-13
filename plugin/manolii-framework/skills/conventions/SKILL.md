# Skill: Conventions

## Description

Project-specific coding conventions. This is a template — fill in the sections below for your project.

## Activation

- **Trigger:** Always active — loaded into context for every session
- **Always active:** Yes

## Allowed Tools

Read, Glob, Grep

## Instructions

Follow these project conventions at all times.

### Naming Conventions

<!-- Fill in for your project -->
- **Files:** {kebab-case | camelCase | PascalCase}
- **Components:** {PascalCase}
- **Functions:** {camelCase}
- **Constants:** {UPPER_SNAKE_CASE}
- **Types/Interfaces:** {PascalCase, prefix with I for interfaces? T for types?}
- **Database tables:** {snake_case}
- **API routes:** {kebab-case}

### File Structure

<!-- Fill in for your project -->
```text
src/
  app/          # {Next.js app router | pages | etc.}
  lib/          # {shared utilities}
  components/   # {React components}
  types/        # {TypeScript types}
```

### Import Ordering

<!-- Fill in for your project -->
1. Node.js built-ins
2. External packages
3. Internal aliases (`@/`)
4. Relative imports
5. Type imports (separate block)

### Error Handling Patterns

<!-- Fill in for your project -->
- API routes: {try/catch with structured error responses}
- Server actions: {return { error: string } | { data: T }}
- Client: {error boundaries + toast notifications}

### Logging Standards

<!-- Fill in for your project -->
- Logger: {console | pino | winston}
- Structured format: {yes/no}
- Levels: {error, warn, info, debug}
- Never log: {secrets, tokens, PII, full request bodies}

### Code Style

- Max function length: 50 lines (suggest extraction above this)
- Max file length: 300 lines (suggest splitting above this)
- Prefer `const` over `let`, never use `var`
- Prefer named exports over default exports
- Prefer early returns over nested conditionals
- Use strict equality (`===` / `!==`), never loose equality
- Avoid `any` — use `unknown` and narrow with Zod or type guards
- Destructure parameters when accessing 3+ properties
- Use template literals over string concatenation
- Prefer `async/await` over `.then()` chains
