# MCP Support in Breeze Buddy

## Overview

MCP (Model Context Protocol) support allows Breeze Buddy templates to connect to external MCP servers, discover tools dynamically, and expose those tools to the conversation flow as global functions.

The staged implementation uses:

- template-level MCP configuration under `configurations.mcp_config`
- streamable HTTP transport to connect to MCP servers
- Pipecat's public MCP client API for tool registration
- direct MCP SDK calls for tool execution

## Architecture

```text
┌────────────────────┐     ┌────────────────────────┐     ┌──────────────────┐
│ Template Config    │────▶│ configurations.mcp_config │──▶│ MCP Server       │
│ (JSON)             │     │ server_url, timeout    │     │ (external)       │
└────────────────────┘     └────────────────────────┘     └──────────────────┘
                                                                │
                                                                ▼
┌─────────────┐     ┌────────────────────┐     ┌────────────────────────────┐
│ User        │◀───▶│ Breeze Buddy Agent │◀───▶│ BreezeBuddyMCPClient       │
│ (voice)     │     │ FlowManager        │     │ register + execute tools   │
└─────────────┘     └────────────────────┘     └────────────────────────────┘
```

## Files Added or Modified

| File | Purpose |
|------|---------|
| `app/ai/voice/agents/breeze_buddy/mcp/client.py` | MCP client implementation |
| `app/ai/voice/agents/breeze_buddy/mcp/__init__.py` | MCP client exports |
| `app/ai/voice/agents/breeze_buddy/mcp/config.py` | Default MCP timeout config |
| `app/ai/voice/agents/breeze_buddy/agent/__init__.py` | MCP initialization and cleanup |
| `app/ai/voice/agents/breeze_buddy/agent/flow.py` | Merge MCP tools into FlowManager global functions |
| `app/ai/voice/agents/breeze_buddy/template/types.py` | MCP template schema and validation |
| `app/core/config/dynamic.py` | `BB_MCP_MAX_TIMEOUT` dynamic config |
| `app/ai/voice/agents/breeze_buddy/examples/templates/shopify-storefront-mcp.json` | Example Shopify MCP template |
| `docs/MCP_SUPPORT.md` | Documentation |

## Configuration

### Dynamic Config

| Variable | Default | Description |
|----------|---------|-------------|
| `BB_MCP_MAX_TIMEOUT` | `300` | Maximum timeout in seconds allowed for MCP connect/register/execute operations |

Note:

- This value is read via the dynamic config layer.
- The current implementation fetches it through `get_config(...)`.

### Template Configuration

MCP is configured per template under `configurations.mcp_config`.

Example:

```json
{
  "configurations": {
    "mcp_config": [
      {
        "enabled": true,
        "server_url": "https://your-store.myshopify.com/api/mcp",
        "timeout": 30,
        "headers": {
          "Authorization": "Bearer token"
        }
      }
    ]
  }
}
```

The schema supports:

- multiple MCP servers via a list
- optional request headers
- a per-server timeout
- backward compatibility for a single object, which is normalized into a list

### Template Types

The staged code adds `MCPConfiguration` and `mcp_config` under `ConfigurationModel`.

```python
class MCPConfiguration(BaseModel):
    enabled: bool = False
    server_url: HttpUrl
    timeout: int = 30
    headers: Optional[Dict[str, SecretStr]] = None

class ConfigurationModel(BaseModel):
    mcp_config: Optional[List[MCPConfiguration]] = None
```

Validation enforced by the code:

- `server_url` is required when enabled
- `server_url` must use HTTPS
- private, localhost, loopback, and reserved destinations are blocked for SSRF protection

## MCP Client

### Transport

The implementation uses streamable HTTP transport, not SSE.

Relevant implementation details:

- `StreamableHttpParameters`
- `streamable_http_client`
- `server_url` as the endpoint field

### Initialization

```python
from app.ai.voice.agents.breeze_buddy.mcp import BreezeBuddyMCPClient

client = BreezeBuddyMCPClient()

connected = await client.connect(
    server_url="https://your-store.myshopify.com/api/mcp",
    headers={"Authorization": "Bearer token"},
    timeout=30,
)

if connected:
    tools = await client.register_tools(llm, timeout=30)
    flow_functions = client.get_flow_global_functions()
```

### Tool Execution

```python
result = await client.execute_tool(
    tool_name="search_shop_catalog",
    args={"query": "running shoes"},
    timeout=30,
)
```

### Cleanup

```python
await client.disconnect()
```

## Agent Integration

### Initialization Flow

1. Agent starts and loads template configuration.
2. If `configurations.mcp_config` is present, the agent iterates over the configured MCP servers.
3. For each enabled config:
   - create a `BreezeBuddyMCPClient`
   - decrypt header values if they are `SecretStr`
   - connect to the server using `server_url`
   - register discovered tools with the LLM
   - convert registered tools into `FlowsFunctionSchema`
4. All discovered MCP tools are aggregated and passed to `FlowManager` as `global_functions`.

### FlowManager Integration

The staged implementation does not add a separate `handle_mcp_tool_execution()` router. Instead:

1. MCP tools are converted to `FlowsFunctionSchema`
2. They are merged with template-defined global functions
3. If a tool name collides with a template function name, the template function wins and the colliding MCP tool is skipped
4. The merged list is passed into `FlowManager`

This design keeps MCP tools available across node transitions like other global functions.

### Cleanup Flow

1. Call ends or agent exits
2. Agent calls `_cleanup_mcp_clients()`
3. Each MCP client is disconnected with timeout protection
4. Errors are logged but do not block remaining cleanup

## Example: Shopify Storefront

See:

- `app/ai/voice/agents/breeze_buddy/examples/templates/shopify-storefront-mcp.json`

This example demonstrates:

- MCP enabled through template configuration
- a Shopify MCP endpoint via `server_url`
- product and cart related tools exposed dynamically from the MCP server

## Key Features

- Multiple MCP servers supported through `mcp_config` list
- Automatic tool discovery from each connected MCP server
- MCP tools exposed as FlowManager global functions
- Collision handling between MCP tools and template-defined functions
- Configurable timeout with dynamic upper bound via `BB_MCP_MAX_TIMEOUT`
- Graceful degradation when MCP connections or tool registration fail
- SSRF-oriented validation for configured MCP server URLs

## Error Handling

| Scenario | Behavior |
|----------|----------|
| MCP server unreachable | Log error and continue without MCP tools |
| MCP connection timeout | Connection fails cleanly and resources are cleaned up |
| Tool registration timeout | Registration fails and the call continues without those tools |
| MCP tool execution error | Handler returns a friendly error result to the LLM |
| MCP name collision with template function | MCP tool is skipped, template function takes precedence |
| Disconnect timeout during cleanup | Warning logged, remaining cleanup continues |

## Redis Impact

MCP does not create any MCP-specific Redis keys.

What is true:

- no Redis keys are created for MCP connections or MCP tool state
- MCP client state is maintained in memory during the call
- MCP connections are cleaned up on call end

Important nuance:

- the timeout cap `BB_MCP_MAX_TIMEOUT` is read via the dynamic config layer
- so the feature has no MCP-specific Redis storage, but it can still depend on the existing config system

## Summary

The staged implementation adds template-driven MCP support to Breeze Buddy using streamable HTTP connections, dynamic tool registration, and FlowManager global-function integration. It is multi-server capable, resilient to failures, and does not persist MCP session state in Redis.
