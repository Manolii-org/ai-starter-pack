---
name: mcp-builder
version: 1.0.0
description: "Building MCP servers for integrations. Use when creating Model Context Protocol servers, building tool providers, or extending agent capabilities."
type: skill
data_sensitivity: internal
safety_tier: green
requires_mcp: []
required_entities: []
allowed-tools:
  - Read
  - Grep
  - Glob
  - Edit
  - Write
  - Bash
tags:
  - mcp
  - agents
---

# MCP Builder Skill

Guide for building Model Context Protocol (MCP) servers that provide tools and resources to AI agents.
---

## Quick Reference

```bash
MCP Server → Exposes Tools → Agent Calls Tools → Server Returns Results
```

---

## 1. MCP Fundamentals

### 1.1 What is MCP?

```markdown

## Model Context Protocol

MCP is a standard protocol for AI agents to interact with external tools:
- **Server:** Provides tools and resources
- **Client:** AI agent that uses tools
- **Transport:** Communication layer (stdio, SSE, etc.)

### Key Components

- Tools: Functions agents can call
- Resources: Data sources agents can read
- Prompts: Pre-defined prompt templates
```

### 1.2 When to Build MCP Server

| Scenario | Build MCP? |
|----------|------------|
| Integrate with external API | Yes |
| Access internal services | Yes |
| Custom database operations | Yes |
| Simple file operations | No (use built-in) |
| One-off script | No (use Bash) |

---

## 2. Server Structure

### 2.1 Basic MCP Server

```typescript
import { Server } from '@modelcontextprotocol/sdk/server/index.js';
import { StdioServerTransport } from '@modelcontextprotocol/sdk/server/stdio.js';
const server = new Server({
  name: 'my-mcp-server',
  version: '1.0.0',
}, {
  capabilities: {
    tools: {},
    resources: {},
  },
});
// Define tools and handlers here
async function main() {
  const transport = new StdioServerTransport();
  await server.connect(transport);
}
main().catch(console.error);
```

### 2.2 Project Structure

```text
my-mcp-server/
├── src/
│   ├── index.ts          # Entry point
│   ├── tools/            # Tool implementations
│   │   ├── search.ts
│   │   └── create.ts
│   └── utils/            # Shared utilities
│       └── api.ts
├── package.json
├── tsconfig.json
└── README.md
```

---

## 3. Defining Tools

### 3.1 Tool Schema

```typescript
import { Tool } from '@modelcontextprotocol/sdk/types.js';
const searchUsersTool: Tool = {
  name: 'search_users',
  description: 'Search for users by name or email. Returns matching users.',
  inputSchema: {
    type: 'object',
    properties: {
      query: {
        type: 'string',
        description: 'Search term (name or email)',
      },
      limit: {
        type: 'number',
        description: 'Maximum results to return',
        default: 10,
      },
    },
    required: ['query'],
  },
};
```

### 3.2 Tool Handler

```typescript
server.setRequestHandler('tools/call', async (request) => {
  const { name, arguments: args } = request.params;
  switch (name) {
    case 'search_users':
      return handleSearchUsers(args);
    case 'create_user':
      return handleCreateUser(args);
    default:
      throw new McpError(ErrorCode.MethodNotFound, `Unknown tool: ${name}`);
  }
});
async function handleSearchUsers(args: { query: string; limit?: number }) {
  const { query, limit = 10 } = args;
  const users = await db.searchUsers(query, limit);
  return {
    content: [{
      type: 'text',
      text: JSON.stringify(users, null, 2),
    }],
  };
}
```

### 3.3 Registering Tools

```typescript
server.setRequestHandler('tools/list', async () => {
  return {
    tools: [
      searchUsersTool,
      createUserTool,
      deleteUserTool,
    ],
  };
});
```

---

## 4. Error Handling

### 4.1 Structured Errors

```typescript
import { McpError, ErrorCode } from '@modelcontextprotocol/sdk/types.js';
async function handleCreateUser(args: CreateUserArgs) {
  // Validation error
  if (!args.email) {
    throw new McpError(
      ErrorCode.InvalidParams,
      'Email is required'
    );
  }
  try {
    const user = await db.createUser(args);
    return { content: [{ type: 'text', text: JSON.stringify(user) }] };
  } catch (error) {
    if (error.code === 'UNIQUE_VIOLATION') {
      throw new McpError(
        ErrorCode.InvalidParams,
        'User with this email already exists'
      );
    }
    throw new McpError(
      ErrorCode.InternalError,
      'Failed to create user'
    );
  }
}
```

### 4.2 Error Response Format

```typescript
// Good error messages help agents understand what went wrong
{
  error: {
    code: ErrorCode.InvalidParams,
    message: 'Email is required',
    data: {
      field: 'email',
      received: undefined,
    },
  },
}
```

---

## 5. Resources

### 5.1 Defining Resources

```typescript
server.setRequestHandler('resources/list', async () => {
  return {
    resources: [
      {
        uri: 'db://users/schema',
        name: 'User Schema',
        description: 'Database schema for users table',
        mimeType: 'application/json',
      },
      {
        uri: 'config://app/settings',
        name: 'App Settings',
        description: 'Current application configuration',
        mimeType: 'application/json',
      },
    ],
  };
});
```

### 5.2 Resource Handler

```typescript
server.setRequestHandler('resources/read', async (request) => {
  const { uri } = request.params;
  if (uri === 'db://users/schema') {
    const schema = await db.getTableSchema('users');
    return {
      contents: [{
        uri,
        mimeType: 'application/json',
        text: JSON.stringify(schema, null, 2),
      }],
    };
  }
  throw new McpError(ErrorCode.NotFound, `Resource not found: ${uri}`);
});
```

---

## 6. Authentication

### 6.1 API Key Authentication

```typescript
const apiKey = process.env.SERVICE_API_KEY;
if (!apiKey) {
  throw new Error('SERVICE_API_KEY is required');
}
async function makeAuthenticatedRequest(endpoint: string, options: RequestInit) {
  return fetch(`${baseUrl}${endpoint}`, {
    ...options,
    headers: {
      ...options.headers,
      'Authorization': `Bearer ${apiKey}`,
    },
  });
}
```

### 6.2 Scoped Permissions

```typescript
// Define permission levels
const PERMISSIONS = {
  read: ['search_users', 'get_user'],
  write: ['create_user', 'update_user'],
  admin: ['delete_user', 'manage_roles'],
};
function checkPermission(tool: string, userRole: string): boolean {
  if (userRole === 'admin') return true;
  if (userRole === 'write') {
    return [...PERMISSIONS.read, ...PERMISSIONS.write].includes(tool);
  }
  if (userRole === 'read') {
    return PERMISSIONS.read.includes(tool);
  }
  return false; // Deny unknown roles
}
```

---

## 7. Configuration

### 7.1 Package.json

```json
{
  "name": "@company/my-mcp-server",
  "version": "1.0.0",
  "type": "module",
  "main": "dist/index.js",
  "bin": {
    "my-mcp-server": "dist/index.js"
  },
  "scripts": {
    "build": "tsc",
    "start": "node dist/index.js",
    "dev": "tsx watch src/index.ts"
  },
  "dependencies": {
    "@modelcontextprotocol/sdk": "^1.0.0"
  }
}
```

### 7.2 Claude Desktop Config

```json
{
  "mcpServers": {
    "my-server": {
      "command": "npx",
      "args": ["-y", "@company/my-mcp-server"],
      "env": {
        "SERVICE_API_KEY": "your-api-key"
      }
    }
  }
}
```

---

## 8. Best Practices

| Practice | Reason |
|----------|--------|
| Clear descriptions | Agents rely on them |
| Specific input schemas | Validation and hints |
| Predictable output | Consistent parsing |
| Informative errors | Helps agents recover |

---

## MCP Builder Checklist

### Server Setup

- [ ] Entry point configured
- [ ] Transport selected (stdio, SSE)
- [ ] Capabilities declared

### Tools

- [ ] Each tool has clear description
- [ ] Input schemas fully defined
- [ ] Handlers implemented
- [ ] Errors properly thrown

### Resources

- [ ] Resources listed if needed
- [ ] URIs are logical
- [ ] Read handlers work

### Quality

- [ ] Authentication handled
- [ ] Tests written
- [ ] Error messages helpful
- [ ] Documentation complete

## Attribution

Ported from a retired internal skill library (first-party).
