# Breeze Buddy Architecture Documentation

## Table of Contents
1. [Overview](#overview)
2. [Architecture Diagram](#architecture-diagram)
3. [Lead Insertion and Processing](#lead-insertion-and-processing)
4. [Template System](#template-system)
5. [Component Reference](#component-reference)
6. [Data Flow](#data-flow)
7. [Example: Order Confirmation Workflow](#example-order-confirmation-workflow)

---

## Overview

Breeze Buddy is a template-based voice agent system built on top of Pipecat for handling automated voice calls. The system uses a dynamic template engine that allows non-technical users to design complex conversation flows through JSON configurations while maintaining programmatic extensibility through hooks and internal handlers.

### Key Features
- **Template-Driven Architecture**: Conversation flows defined as JSON templates stored in database
- **Dynamic Variable Substitution**: Lead payload data automatically injected into conversation prompts
- **Node-Based Flow Control**: Graph-based conversation navigation with LLM-triggered transitions
- **Hook System**: Asynchronous side-effect handlers for database updates and external integrations
- **Multi-Provider Support**: Compatible with Twilio and Exotel telephony providers
- **Schema Validation**: Payload and callback response validation against expected schemas

### Technology Stack
- **Pipecat**: Voice pipeline framework for STT/TTS/LLM integration
- **FastAPI**: API layer for lead ingestion and template management
- **PostgreSQL**: Template and lead data storage
- **Pydantic**: Data validation and modeling
- **OpenTelemetry**: Distributed tracing and observability

---

## Architecture Diagram

```
┌─────────────────────────────────────────────────────────────────────────┐
│                           API Layer (FastAPI)                            │
├─────────────────────────────────────────────────────────────────────────┤
│  /push/lead/v2         │  /template (GET/POST)  │  /websocket           │
│  - Lead insertion      │  - Template CRUD       │  - Call handling      │
│  - Payload validation  │  - Schema validation   │  - WebSocket connect  │
└────────────┬───────────────────────┬──────────────────────┬─────────────┘
             │                       │                      │
             ▼                       ▼                      ▼
┌────────────────────────┐  ┌──────────────────┐  ┌────────────────────────┐
│   Database Layer       │  │  Template Store  │  │   Agent Layer          │
├────────────────────────┤  ├──────────────────┤  ├────────────────────────┤
│ - Lead Call Tracker    │  │ - Template CRUD  │  │ - Agent.py             │
│ - Call Config          │  │ - Schema mgmt    │  │ - Pipecat pipeline     │
│ - Outbound Numbers     │  │ - Flow storage   │  │ - FlowManager          │
└────────────────────────┘  └──────────────────┘  └───────┬────────────────┘
                                                           │
                                    ┌──────────────────────┴──────────────┐
                                    │                                     │
                           ┌────────▼────────┐              ┌─────────────▼────────┐
                           │ Template Engine │              │  Service Layer       │
                           ├─────────────────┤              ├──────────────────────┤
                           │ - Loader        │              │ - Twilio/Exotel     │
                           │ - Builder       │              │ - Audio streaming    │
                           │ - Transition    │              │ - Call recording     │
                           │ - Hooks         │              │ - Callbacks          │
                           │ - Context       │              └──────────────────────┘
                           └─────────────────┘
```

---

## Lead Insertion and Processing

### 1. Lead Insertion Flow

Leads are inserted through the `/push/lead/v2` endpoint:

**Request Model** ([types/models.py:15-20](app/ai/voice/agents/breeze_buddy/types/models.py#L15-L20)):
```python
class PushLeadRequest(BaseModel):
    payload: Dict[str, Any]          # Order/lead data
    template: str                     # Template name to use
    merchant: str                     # Merchant identifier
    identifier: Optional[str] = None  # Shop-specific identifier
    reporting_webhook_url: str | None = None  # Callback URL
```

**Insertion Process** ([leads.py:153-275](app/api/routers/breeze_buddy/leads.py#L153-L275)):

1. **Template Retrieval**: Fetch template by merchant, identifier, and name
   ```python
   template = await get_template_by_merchant(
       req.merchant, req.identifier, req.template
   )
   ```

2. **Payload Validation**: Validate against `expected_payload_schema`
   ```python
   if template.expected_payload_schema:
       is_valid, validation_errors = validate_payload(
           req.payload, template.expected_payload_schema
       )
   ```

3. **Call Config Retrieval**: Get execution configuration
   ```python
   call_execution_configs = await get_call_execution_config_by_merchant_id(
       req.merchant, req.identifier
   )
   ```

4. **Lead Creation**: Insert into `lead_call_tracker` table
   ```python
   lead_call_tracker = await create_lead_call_tracker(
       id=uuid,
       merchant_id=req.merchant,
       template=req.template,
       shop_identifier=req.identifier,
       next_attempt_at=next_attempt_at,  # Scheduled time
       payload=lead_payload,
       attempt_count=0,
       meta_data={"use_template_flow": True}
   )
   ```

### 2. Lead Processing During Call

When a call is initiated via WebSocket ([agent.py](app/ai/voice/agents/breeze_buddy/agent.py)):

**Step 1: Call Initialization**
```python
await update_lead_call_initiated_time(self.call_sid, call_initiated_time)
lead = await get_lead_by_call_id(self.call_sid)
```

**Step 2: Template Loading**
```python
template = await get_template_by_merchant(
    merchant_id=merchant_id,
    shop_identifier=self.lead.shop_identifier,
    name=self.lead.template,
)
```

**Step 3: Variable Building**
Template variables are dynamically constructed from the lead payload based on the template's `expected_payload_schema`:

```python
self.template_vars = {}
for field_name in template.expected_payload_schema.keys():
    if field_name in call_payload:
        self.template_vars[field_name] = call_payload[field_name]
    else:
        logger.warning(f"Field '{field_name}' not found in payload")
        self.template_vars[field_name] = ""
```

**Step 4: Template Rendering**
The loader renders all task messages by substituting `{variable}` placeholders:

```python
self.template_config = await self.flow_loader.load_template(
    merchant_id=merchant_id,
    template=self.lead.template,
    template_vars=self.template_vars,
)
```

**Step 5: Flow Configuration**
The builder converts the database template into Pipecat-compatible format:

```python
self.flow_config = self.flow_builder.build_flow_config(self.template_config)
```

---

## Template System

The template system is the core of Breeze Buddy's architecture. It consists of five main components:

### 1. Template Data Model

**Location**: [template/types.py](app/ai/voice/agents/breeze_buddy/template/types.py)

#### TemplateModel
```python
class TemplateModel(BaseModel):
    id: str
    merchant_id: str
    shop_identifier: Optional[str] = None
    name: str
    flow: Dict[str, Any]  # The complete flow configuration
    expected_payload_schema: Optional[Dict[str, Any]] = None
    expected_callback_response_schema: Optional[Dict[str, Any]] = None
    is_active: bool = True
    rendered_system_prompt: str = ""
```

**Fields**:
- `id`: Unique template identifier
- `merchant_id`: Merchant this template belongs to
- `shop_identifier`: Optional shop-specific override
- `name`: Template name (e.g., "order-confirmation")
- `flow`: Complete flow configuration with nodes and transitions
- `expected_payload_schema`: Schema for validating incoming lead data
- `expected_callback_response_schema`: Schema for callback responses
- `is_active`: Whether template is currently active

#### FlowNodeModel
```python
class FlowNodeModel(BaseModel):
    node_name: str                         # Unique node identifier
    task_messages: List[TaskMessage]       # LLM instructions for this node
    role_messages: List[TaskMessage] = []  # System role context
    pre_actions: List[FlowAction] = []     # Actions before LLM response
    post_actions: List[FlowAction] = []    # Actions after LLM response
    functions: List[FlowFunction] = []     # Available functions for transitions
```

**Node Components**:
- `task_messages`: Instructions for the LLM (e.g., "Verify order details")
- `role_messages`: Persistent role context (e.g., "You are a friendly agent")
- `pre_actions`: Execute before node starts (e.g., play audio, mute STT)
- `post_actions`: Execute after node completes (e.g., unmute STT, end call)
- `functions`: Available functions that trigger transitions

#### FlowFunction
```python
class FlowFunction(BaseModel):
    name: str                              # Function name
    description: str                       # Description for LLM
    properties: Dict[str, Any] = {}        # Function parameters
    required: List[str] = []               # Required parameters
    transition_to: Optional[str] = None    # Target node
    hooks: List[HookConfig] = []           # Side-effect hooks
```

**Function Components**:
- `name`: Function identifier (e.g., "confirm_order")
- `description`: Helps LLM decide when to call this function
- `properties`: JSON schema for function parameters
- `required`: List of required parameter names
- `transition_to`: Next node to transition to
- `hooks`: List of hooks to execute asynchronously

#### HookConfig
```python
class HookConfig(BaseModel):
    name: str                                      # Hook identifier
    expected_fields: Dict[str, HookFieldConfig] = {}  # Field mappings
```

**Hook Field Sources**:
```python
class HookFieldConfigSource(str, Enum):
    STATIC = "static"  # Use enforced value from config
    LLM = "llm"        # Use value inferred by LLM
```

**Example Hook Configuration**:
```json
{
  "name": "update_outcome_in_database",
  "expected_fields": {
    "outcome": {
      "source": "static",
      "value": "confirmed"
    },
    "cancellation_reason": {
      "source": "llm"
    }
  }
}
```

### 2. FlowConfigLoader

**Location**: [template/loader.py](app/ai/voice/agents/breeze_buddy/template/loader.py)

**Purpose**: Loads templates from database and renders them with runtime variables.

**Key Methods**:

#### `load_template(merchant_id, template, template_vars, shop_identifier)`
Main entry point for loading and rendering templates.

**Process**:
1. Load template from database via `get_template_by_merchant`
2. Filter out inactive nodes
3. Render task messages with variable substitution
4. Render role messages with variable substitution

**Variable Substitution** ([loader.py:56-84](app/ai/voice/agents/breeze_buddy/template/loader.py#L56-L84)):
```python
def render_task_messages(self, task_messages: list, variables: Dict[str, str]) -> list:
    """Replace {variable} placeholders with actual values"""
    rendered_messages = []
    for message in task_messages:
        if isinstance(message, dict) and "content" in message:
            content = message["content"]
            # Replace variables in content
            for key, value in variables.items():
                placeholder = f"{{{key}}}"
                content = content.replace(placeholder, str(value))

            rendered_message = message.copy()
            rendered_message["content"] = content
            rendered_messages.append(rendered_message)
    return rendered_messages
```

**Example**:
```python
# Template: "Hi {customer_name}, your order {order_id} is ready"
# Variables: {"customer_name": "John", "order_id": "12345"}
# Result: "Hi John, your order 12345 is ready"
```

### 3. FlowConfigBuilder

**Location**: [template/builder.py](app/ai/voice/agents/breeze_buddy/template/builder.py)

**Purpose**: Converts database templates into Pipecat-compatible flow configurations.

**Key Methods**:

#### `build_flow_config(template: TemplateModel) -> Dict[str, Any]`
Main builder method ([builder.py:45-128](app/ai/voice/agents/breeze_buddy/template/builder.py#L45-L128)):

**Process**:
1. Extract initial node and nodes list from template.flow
2. Validate template structure
3. Convert each node data to FlowNodeModel
4. Build Pipecat NodeConfig for each node
5. Return flow configuration

**Output Structure**:
```python
{
    "initial_node": "initial",
    "nodes": {
        "initial": NodeConfig(...),
        "verify_order": NodeConfig(...),
        # ... more nodes
    },
    "end_conversation_callbacks": ["service_callback"],
    "expected_callback_response_schema": {...}
}
```

#### `_build_node(node: FlowNodeModel) -> NodeConfig`
Converts FlowNodeModel to Pipecat NodeConfig ([builder.py:130-190](app/ai/voice/agents/breeze_buddy/template/builder.py#L130-L190)):

**Components Built**:
1. **Task Messages**: Convert to Pipecat format
2. **Role Messages**: Convert to Pipecat format
3. **Functions**: Build function schemas with handlers
4. **Pre-actions**: Build action configurations
5. **Post-actions**: Build action configurations

#### `_build_function_schema(func: FlowFunction) -> FlowsFunctionSchema`
Creates function schemas with unified transition handler ([builder.py:192-239](app/ai/voice/agents/breeze_buddy/template/builder.py#L192-L239)):

**Key Implementation**:
```python
def _build_function_schema(self, func: FlowFunction) -> FlowsFunctionSchema:
    wrapped_unified_handler = self.handler_map.get("transition_handler")

    # Convert HookConfig objects to dicts for serialization
    hooks = [hook.model_dump() for hook in func.hooks] if func.hooks else []

    # Create wrapper that calls transition handler with all params
    async def wrapper_handler(flow_manager, llm_args):
        result = await wrapped_unified_handler(
            flow_manager,
            llm_args,
            transition_to=func.transition_to,
            hooks=hooks,
            function_name=func.name,
        )
        return result

    return FlowsFunctionSchema(
        name=func.name,
        description=func.description,
        handler=wrapper_handler,
        properties=func.properties,
        required=func.required,
    )
```

**Handler Map** ([builder.py:37-43](app/ai/voice/agents/breeze_buddy/template/builder.py#L37-L43)):
Maps handler names to actual functions:
```python
self.handler_map = {
    "mute_stt": mute_stt,
    "unmute_stt": unmute_stt,
    "play_audio_sound": play_audio_sound,
    "end_conversation": end_conversation,
    "transition_handler": transition_handler,
}
```

#### `_build_action(action: FlowAction) -> Dict[str, Any]`
Builds action configurations for pre/post actions ([builder.py:241-273](app/ai/voice/agents/breeze_buddy/template/builder.py#L241-L273)):

**Action Types**:
1. **TTS_SAY**: Speak text via TTS
   ```python
   {"type": "tts_say", "text": "Thank you for confirming"}
   ```

2. **FUNCTION**: Execute internal handler
   ```python
   {"type": "function", "handler": mute_stt}
   ```

### 4. Transition Handler

**Location**: [template/transition.py](app/ai/voice/agents/breeze_buddy/template/transition.py)

**Purpose**: Unified handler for all workflow transitions.

#### `transition_handler(context, args, transition_to, hooks, function_name)`
Main transition logic ([transition.py:19-70](app/ai/voice/agents/breeze_buddy/template/transition.py#L19-L70)):

**Process**:
1. **Async Hook Execution**: Schedule hooks to run in background
   ```python
   if hooks:
       asyncio.create_task(
           _execute_hooks_async(context, args, hooks, function_name)
       )
   ```

2. **Immediate Transition**: Transition to next node without blocking
   ```python
   if transition_to:
       next_node = context.create_node_from_template(transition_to)
       return {}, next_node
   ```

**Key Design**: Transitions are **synchronous** while hooks are **asynchronous** (fire-and-forget). This ensures conversation flow isn't blocked by database operations.

#### `_execute_hooks_async(context, args, hook_configs, function_name)`
Executes hooks in background ([transition.py:73-117](app/ai/voice/agents/breeze_buddy/template/transition.py#L73-L117)):

**Process**:
1. Iterate through hook configurations
2. Convert dict to HookConfig object
3. Retrieve hook from HookRegistry
4. Execute hook with error handling via `safe_execute`

### 5. Hook System

**Location**: [template/hooks.py](app/ai/voice/agents/breeze_buddy/template/hooks.py)

**Purpose**: Asynchronous side-effect handlers that execute independently of workflow transitions.

#### Base Hook Class
```python
class Hook(ABC):
    @abstractmethod
    async def execute(
        self,
        context: TemplateContext,
        args: Dict[str, Any],
        function_name: str,
        expected_fields: Optional[Dict[str, HookFieldConfig]] = None
    ) -> None:
        """Execute hook logic"""

    async def safe_execute(self, ...) -> None:
        """Wrapper with error handling"""
```

#### UpdateOutcomeInDatabaseHook
Main hook implementation ([hooks.py:90-249](app/ai/voice/agents/breeze_buddy/template/hooks.py#L90-L249)):

**Purpose**: Updates lead call outcome in database.

**Process**:

1. **Build Final Data** from expected_fields:
   ```python
   final_data: Dict[str, Any] = {}
   for field_name, field_config in expected_fields.items():
       if field_config.source == HookFieldConfigSource.STATIC:
           # Use enforced value from config
           final_data[field_name] = field_config.value
       elif field_config.source == HookFieldConfigSource.LLM:
           # Use value from LLM arguments
           final_data[field_name] = args.get(field_name)
   ```

2. **Extract Outcome**:
   ```python
   outcome = final_data.get("outcome")
   call_outcome = OUTCOME_TO_ENUM.get(outcome, LeadCallOutcome.UNKNOWN)
   ```

3. **Build Metadata**: Add all non-outcome fields to metadata
   ```python
   meta_data = context.lead.metaData or {}
   for key, value in final_data.items():
       if key != "outcome" and value is not None:
           meta_data[key] = value
   ```

4. **Update Database**:
   ```python
   updated_lead = await update_lead_call_completion_details(
       id=context.lead.id,
       status=None,
       outcome=call_outcome,
       meta_data=meta_data,
       call_end_time=None,
   )
   ```

5. **Update Context**: Refresh lead in context for subsequent hooks
   ```python
   if updated_lead:
       context.bot.lead = updated_lead
   ```

#### HookRegistry
Central registry for all hooks ([hooks.py:251-296](app/ai/voice/agents/breeze_buddy/template/hooks.py#L251-L296)):

```python
class HookRegistry:
    _hooks: Dict[str, Hook] = {}

    @classmethod
    def register(cls, name: str, hook: Hook):
        """Register a hook"""

    @classmethod
    def get(cls, name: str) -> Optional[Hook]:
        """Get hook by name"""

# Register available hooks
HookRegistry.register("update_outcome_in_database", UpdateOutcomeInDatabaseHook())
```

**Adding New Hooks**:
1. Create class extending `Hook`
2. Implement `execute` method
3. Register in HookRegistry

### 6. Template Context

**Location**: [template/context.py](app/ai/voice/agents/breeze_buddy/template/context.py)

**Purpose**: Provides handlers access to bot instance state.

#### TemplateContext Class
```python
class TemplateContext:
    def __init__(self, bot_instance):
        self.bot = bot_instance

    @property
    def lead(self):
        """Access lead information"""
        return self.bot.lead

    @property
    def call_sid(self):
        """Access call SID"""
        return self.bot.call_sid

    def create_node_from_template(self, node_name: str) -> Optional[NodeConfig]:
        """Create NodeConfig from template"""
```

**Available Properties**:
- `conversation_ended`: Call end status
- `vad_analyzer`: Voice activity detector
- `transport`: WebSocket transport
- `task`: Pipeline task
- `context`: LLM context
- `lead`: Lead data
- `call_sid`: Call identifier
- `order_id`: Order identifier
- `reporting_webhook_url`: Callback URL
- `root_span`: OpenTelemetry span
- `provider`: Telephony provider
- `end_conversation_callbacks`: Callback list
- `expected_callback_response_schema`: Response schema

#### `with_context` Decorator
Injects context into handler functions ([context.py:145-215](app/ai/voice/agents/breeze_buddy/template/context.py#L145-L215)):

```python
@with_context(bot)
async def my_handler(context, flow_manager, args):
    # Access bot state via context
    context.outcome = "confirmed"
```

---

## Component Reference

### API Layer

#### 1. Leads Router
**Location**: [api/routers/breeze_buddy/leads.py](app/api/routers/breeze_buddy/leads.py)

**Endpoints**:

##### `POST /push/lead/v2`
Inserts new lead for processing ([leads.py:153-275](app/api/routers/breeze_buddy/leads.py#L153-L275))

**Request**:
```json
{
  "payload": {
    "order_id": "12345",
    "customer_name": "John Doe",
    "customer_mobile_number": "+919876543210",
    "total_price": 1500
  },
  "template": "order-confirmation",
  "merchant": "merchant_123",
  "identifier": "shop_456",
  "reporting_webhook_url": "https://example.com/webhook"
}
```

**Response**:
```json
{
  "status": "queued",
  "lead_call_tracker_id": "uuid",
  "order_id": "12345",
  "message": "Call request added to queue for processing"
}
```

**Validation**:
- Validates payload against template's `expected_payload_schema`
- Checks template exists for merchant
- Verifies call execution config exists

##### `GET /lead/{lead_id}`
Retrieves lead by ID ([leads.py:25-55](app/api/routers/breeze_buddy/leads.py#L25-L55))

##### `POST /{merchant}/{template}`
Legacy endpoint for order confirmation ([leads.py:58-150](app/api/routers/breeze_buddy/leads.py#L58-L150))

#### 2. Template Router
**Location**: [api/routers/breeze_buddy/template.py](app/api/routers/breeze_buddy/template.py)

**Endpoints**:

##### `GET /template`
Retrieves template by merchant, shop, and name ([template.py:17-52](app/api/routers/breeze_buddy/template.py#L17-L52))

**Query Parameters**:
- `merchant_id`: Required
- `shop_identifier`: Optional
- `name`: Optional

##### `POST /template`
Creates new template from JSON ([template.py:55-123](app/api/routers/breeze_buddy/template.py#L55-L123))

**Request**:
```json
{
  "merchant": "merchant_123",
  "identifier": "shop_456",
  "template_name": "order-confirmation",
  "is_active": true,
  "expected_payload_schema": {...},
  "expected_callback_response_schema": {...},
  "flow": {
    "initial_node": "initial",
    "nodes": [...]
  }
}
```

**Validation**:
- Checks `initial_node` exists
- Validates `nodes` array is not empty
- Prevents duplicate templates
- Validates flow structure

### Database Layer

#### 1. Template Accessor
**Location**: [database/accessor/breeze_buddy/template.py](app/database/accessor/breeze_buddy/template.py)

**Functions**:

##### `get_template_by_merchant(merchant_id, shop_identifier, name)`
Retrieves template from database ([template.py:29-52](app/database/accessor/breeze_buddy/template.py#L29-L52))

**Process**:
1. Build parameterized query
2. Execute query via `run_parameterized_query`
3. Decode result to TemplateModel
4. Return TemplateModel or None

##### `create_template(...)`
Creates new template in database ([template.py:55-105](app/database/accessor/breeze_buddy/template.py#L55-L105))

**Process**:
1. Convert flow dict to JSON string
2. Convert schemas to JSON strings
3. Execute insert query
4. Return decoded TemplateModel

### Agent Layer

#### 1. Agent
**Location**: [ai/voice/agents/breeze_buddy/agent.py](app/ai/voice/agents/breeze_buddy/agent.py)

**Purpose**: Main entry point for voice agent, orchestrates the entire pipeline.

**Key Components**:
- Initializes Pipecat pipeline (STT, TTS, LLM)
- Loads and builds template configuration
- Manages FlowManager for conversation flow
- Handles WebSocket connections
- Processes tracing and metrics

**Initialization Flow**:
1. Connect WebSocket
2. Retrieve lead by call_sid
3. Load template for merchant
4. Build template variables from payload
5. Render template with variables
6. Build flow configuration
7. Initialize FlowManager
8. Start Pipecat pipeline

### Service Layer

#### 1. Telephony Providers
**Locations**:
- [services/telephony/twilio/](app/ai/voice/agents/breeze_buddy/services/telephony/twilio/)
- [services/telephony/exotel/](app/ai/voice/agents/breeze_buddy/services/telephony/exotel/)

**Purpose**: Handle provider-specific call operations.

**Functions**:
- Call initiation
- Audio streaming
- Call recording
- Callback handling
- Call status updates

#### 2. Callback Handlers
**Location**: [callbacks/service_callback.py](app/ai/voice/agents/breeze_buddy/callbacks/service_callback.py)

**Purpose**: Execute callbacks at end of conversation.

**Process**:
1. Extract callback response data
2. Validate against `expected_callback_response_schema`
3. Send webhook request to `reporting_webhook_url`

### Internal Handlers

**Location**: [handlers/internal/](app/ai/voice/agents/breeze_buddy/handlers/internal/)

#### 1. `end_conversation`
Terminates conversation and ends call.

#### 2. `mute_stt` / `unmute_stt`
Controls speech-to-text processing.

#### 3. `play_audio_sound`
Plays audio (e.g., dial tone, hold music).

---

## Data Flow

### Complete Call Flow

```
1. Lead Insertion
   ├─> POST /push/lead/v2
   ├─> Validate payload against template schema
   ├─> Get call execution config
   └─> Insert into lead_call_tracker table

2. Call Scheduler (Background Process)
   ├─> Query leads with next_attempt_at <= now
   ├─> Initiate call via telephony provider
   └─> Provider creates WebSocket connection

3. WebSocket Connection
   ├─> Agent receives connection
   ├─> Get lead by call_sid
   ├─> Update call_initiated_time
   └─> Load template

4. Template Processing
   ├─> FlowConfigLoader.load_template()
   │   ├─> Load from database
   │   ├─> Filter inactive nodes
   │   └─> Render task messages with variables
   └─> FlowConfigBuilder.build_flow_config()
       ├─> Convert nodes to NodeConfig
       ├─> Build function schemas
       └─> Wire up handlers

5. Pipeline Initialization
   ├─> Initialize STT service
   ├─> Initialize TTS service
   ├─> Initialize LLM service
   ├─> Create FlowManager with flow_config
   └─> Start pipeline

6. Conversation Execution
   ├─> FlowManager starts at initial_node
   ├─> Execute pre_actions
   ├─> Send task_messages to LLM
   ├─> LLM generates response
   ├─> TTS converts to speech
   ├─> Execute post_actions
   └─> Wait for user response

7. Function Call (Transition)
   ├─> LLM decides to call function
   ├─> transition_handler invoked
   ├─> Schedule hooks (async, fire-and-forget)
   │   └─> UpdateOutcomeInDatabaseHook
   │       ├─> Build final data
   │       ├─> Update lead in database
   │       └─> Refresh context.lead
   └─> Transition to next_node (immediate, synchronous)

8. Conversation End
   ├─> Reach end node
   ├─> Execute post_actions (end_conversation)
   ├─> Execute end_conversation_callbacks
   │   └─> Send webhook with callback data
   ├─> Close WebSocket
   └─> Update lead with final status
```

### Template Variable Flow

```
Lead Payload                Template Schema              Template Messages
─────────────              ────────────────              ─────────────────
{                          {                             "Hi {customer_name},
  "customer_name":           "customer_name": "string",   your order {order_id}
    "John Doe",              "order_id": "string",        for {total_price}
  "order_id": "12345",       "total_price": "number"      is ready"
  "total_price": 1500      }
}
      │                           │                              │
      └──────────────┬────────────┘                              │
                     ▼                                           │
              Build template_vars                                │
              {                                                  │
                "customer_name": "John Doe",                     │
                "order_id": "12345",                             │
                "total_price": 1500                              │
              }                                                  │
                     │                                           │
                     └───────────────────────────────────────────┤
                                                                 ▼
                                                    Rendered Message
                                                    ────────────────
                                                    "Hi John Doe,
                                                     your order 12345
                                                     for 1500
                                                     is ready"
```

### Hook Execution Flow

```
LLM Function Call
      │
      ├─> transition_handler(
      │     context,
      │     args={cancellation_reason: "Out of stock"},
      │     transition_to="order_cancellation_and_end_node",
      │     hooks=[{
      │       name: "update_outcome_in_database",
      │       expected_fields: {
      │         outcome: {source: "static", value: "cancelled"},
      │         cancellation_reason: {source: "llm"}
      │       }
      │     }]
      │   )
      │
      ├─> Async: asyncio.create_task(_execute_hooks_async)
      │           │
      │           ├─> Get hook from registry
      │           ├─> hook.safe_execute()
      │           │   │
      │           │   ├─> Build final_data
      │           │   │   {
      │           │   │     outcome: "cancelled",        # From static
      │           │   │     cancellation_reason: "Out of stock"  # From LLM
      │           │   │   }
      │           │   │
      │           │   ├─> Update database
      │           │   │   await update_lead_call_completion_details(
      │           │   │     outcome=LeadCallOutcome.CANCELLED,
      │           │   │     meta_data={cancellation_reason: "Out of stock"}
      │           │   │   )
      │           │   │
      │           │   └─> Update context.lead
      │           │
      │           └─> Complete (no blocking)
      │
      └─> Sync: Immediate transition
              │
              ├─> Create node "order_cancellation_and_end_node"
              └─> Return (next_node, {})
```

---

## Example: Order Confirmation Workflow

### Template Structure

**Location**: [examples/templates/order-confirmation.json](app/ai/voice/agents/breeze_buddy/examples/templates/order-confirmation.json)

### Flow Graph

```
                              ┌─────────────┐
                              │   initial   │
                              └──────┬──────┘
                                     │
              ┌──────────────────────┼──────────────────────┐
              │                      │                      │
         user_available         user_busy           cancel_order
              │                      │                      │
              ▼                      ▼                      ▼
   ┌──────────────────┐   ┌────────────────┐   ┌──────────────────┐
   │ verify_order     │   │ user_busy_and  │   │ order_cancel     │
   │ _detail_node     │   │ _end_node      │   │ _and_end_node    │
   └────────┬─────────┘   └────────────────┘   └──────────────────┘
            │
            ├─> confirm_order ───────────────────┐
            │                                    │
            ├─> confirm_order_with_question ─────┤
            │                                    │
            ├─> cancel_order ────────────────────┤
            │                                    │
            ├─> handle_unrelated_question        │
            │           │                        │
            │           ▼                        │
            │   ┌──────────────────┐             │
            │   │ handle_unrelated │             │
            │   │ _question_node   │             │
            │   └────────┬─────────┘             │
            │            │                       │
            │            ├─> confirm_order ──────┤
            │            ├─> cancel_order ───────┤
            │            └─> address_incorrect   │
            │                      │             │
            └─> address_incorrect  │             │
                       │           │             │
                       ▼           ▼             │
                ┌──────────────────┐             │
                │ update_address   │             │
                │ _node            │             │
                └────────┬─────────┘             │
                         │                       │
                         ├─> update_landmark     │
                         ├─> update_pincode      │
                         ├─> update_city         │
                         ├─> update_locality     │
                         └─> update_phone_number │
                                   │             │
                                   ▼             │
                         ┌──────────────────┐    │
                         │ confirm_address  │    │
                         │ _update_node     │    │
                         └────────┬─────────┘    │
                                  │              │
                                  ├─> confirm ───┤
                                  ├─> cancel ────┤
                                  └─> incorrect  │
                                                 │
                                                 ▼
                              ┌────────────────────────┐
                              │    End Nodes:          │
                              ├────────────────────────┤
                              │ - order_confirmation   │
                              │ - order_cancel         │
                              │ - user_busy            │
                              │ - order_confirmation   │
                              │   _with_question       │
                              └────────────────────────┘
```

### Node Breakdown

#### 1. Initial Node
**Purpose**: Greet customer and check availability.

**Task Message**:
```
"Hi {customer_name} Sir/Madam, Namaste. This is Rhea from {shop_name}.
I'm calling to confirm the order you placed with us.
Is it a good time to talk, Sir/Madam?"
```

**Functions**:
- `user_available` → verify_order_detail_node
- `user_busy` → user_busy_and_end_node (Hook: outcome=busy)
- `cancel_order` → order_cancellation_and_end_node (Hook: outcome=cancelled)
- `handle_unrelated_question` → handle_unrelated_question_node

#### 2. Verify Order Detail Node
**Purpose**: Confirm order items, price, and address.

**Pre-actions**:
1. `play_audio_sound` - Play dial tone
2. `mute_stt` - Prevent interruption

**Post-actions**:
1. `unmute_stt` - Resume listening

**Task Message**:
```
"The order contains {order_summary}.
The total price is {total_price_words} rupees.
The delivery address is {address}.
Please confirm."
```

**Functions**:
- `confirm_order` → order_confirmation_and_end_node (Hook: outcome=confirmed)
- `confirm_order_with_question` → order_confirmation_with_question_and_end_node (Hook: outcome=confirmed)
- `cancel_order` → order_cancellation_and_end_node (Hook: outcome=cancelled, reason from LLM)
- `handle_unrelated_question` → handle_unrelated_question_node
- `address_incorrect` → update_address_node

#### 3. Update Address Node
**Purpose**: Allow customer to update address components.

**Task Message**:
```
"Sure, I can help with that.
What part of the address would you like to update?
You can update the locality, landmark, pincode, city, or phone number."
```

**Functions**:
- `update_landmark` → confirm_address_update_node (Capture landmark from LLM)
- `update_pincode` → confirm_address_update_node (Capture pincode from LLM)
- `update_city` → confirm_address_update_node (Capture city from LLM)
- `update_locality` → confirm_address_update_node (Capture locality from LLM)
- `update_phone_number` → confirm_address_update_node (Capture phone from LLM)
- `handle_unrelated_question` → handle_unrelated_question_node

#### 4. Confirm Address Update Node
**Purpose**: Confirm updated address with customer.

**Task Message**:
```
"Got it. Your address has been updated.
Is there anything else you would like to update,
or should I go ahead and confirm the order?"
```

**Functions**:
- `confirm_order` → order_confirmation_and_end_node (Hook: outcome=address_updated, updated_address from LLM)
- `address_incorrect` → update_address_node
- `cancel_order` → order_cancellation_and_end_node (Hook: outcome=cancelled)

#### 5. Order Confirmation End Node
**Purpose**: Thank customer and end call.

**Task Message**:
```
"Thank you for confirming your order.
Your order will be delivered soon.
Have a good day."
```

**Pre-actions**:
1. `mute_stt` - Stop listening

**Post-actions**:
1. `end_conversation` - Terminate call

**Functions**: None (terminal node)

### Sample Execution Trace

**Scenario**: Customer confirms order with address update.

```
1. Call Start
   ├─> Node: initial
   ├─> TTS: "Hi John Doe Sir, Namaste. This is Rhea from MyShop..."
   ├─> User: "Yes, I'm available"
   └─> LLM calls: user_available()

2. Transition to verify_order_detail_node
   ├─> Pre-action: play_audio_sound
   ├─> Pre-action: mute_stt
   ├─> TTS: "The order contains 2 items: Product A (qty: 1)..."
   ├─> Post-action: unmute_stt
   ├─> User: "The pincode is wrong, it should be 560001"
   └─> LLM calls: address_incorrect()

3. Transition to update_address_node
   ├─> TTS: "Sure, I can help with that. What part..."
   ├─> User: "Pincode is 560001"
   └─> LLM calls: update_pincode(pincode="560001")

4. Transition to confirm_address_update_node
   ├─> TTS: "Got it. Your address has been updated..."
   ├─> User: "Yes, please confirm the order"
   └─> LLM calls: confirm_order(updated_address="...")

5. Hook Execution (Async)
   ├─> Hook: update_outcome_in_database
   ├─> Build final_data: {outcome: "address_updated", updated_address: "..."}
   ├─> Update database: outcome=ADDRESS_UPDATED
   └─> Update metadata: {updated_address: "..."}

6. Transition to order_confirmation_and_end_node
   ├─> Pre-action: mute_stt
   ├─> TTS: "Thank you for confirming your order..."
   ├─> Post-action: end_conversation
   └─> Call ends

7. End Conversation Callbacks
   ├─> Extract callback data: {updated_address: "..."}
   ├─> POST to reporting_webhook_url
   └─> Update final call status
```

### Payload Example

**Input Payload** (from `/push/lead/v2`):
```json
{
  "payload": {
    "order_id": "ORD-12345",
    "customer_name": "John Doe",
    "shop_name": "MyShop",
    "total_price": 1500,
    "customer_address": "123 Main St, Bangalore, 560000",
    "customer_mobile_number": "+919876543210",
    "items": [
      {"product_name": "Product A", "quantity": 1},
      {"product_name": "Product B", "quantity": 2}
    ]
  },
  "template": "order-confirmation",
  "merchant": "merchant_123",
  "identifier": "myshop.myshopify.com",
  "reporting_webhook_url": "https://myshop.com/webhooks/call-status"
}
```

**Template Variables** (built from payload):
```python
{
  "order_id": "ORD-12345",
  "customer_name": "John Doe",
  "shop_name": "MyShop",
  "total_price": 1500,
  "customer_address": "123 Main St, Bangalore, 560000",
  "customer_mobile_number": "+919876543210",
  "items": "2 items: Product A (qty: 1), Product B (qty: 2)"
}
```

**Database Updates** (via hooks):
```sql
-- After confirm_order hook
UPDATE lead_call_tracker
SET
  outcome = 'address_updated',
  meta_data = jsonb_set(meta_data, '{updated_address}', '"123 Main St, Bangalore, 560001"'),
  updated_at = NOW()
WHERE id = 'lead_uuid';
```

**Webhook Callback** (sent at end):
```http
POST https://myshop.com/webhooks/call-status
Content-Type: application/json

{
  "lead_id": "lead_uuid",
  "call_sid": "call_sid_123",
  "outcome": "address_updated",
  "updated_address": "123 Main St, Bangalore, 560001",
  "call_duration": 120,
  "timestamp": "2024-01-15T10:30:00Z"
}
```

---

## Key Design Principles

### 1. Separation of Concerns
- **Templates**: Define conversation flow (declarative)
- **Handlers**: Implement actions (imperative)
- **Hooks**: Execute side effects (asynchronous)
- **Context**: Provide state access (read-only interface)

### 2. Async Hook Execution
Hooks run asynchronously to prevent blocking conversation flow:
```python
# Transition is immediate
if transition_to:
    next_node = context.create_node_from_template(transition_to)
    return {}, next_node

# Hooks run in background (fire-and-forget)
asyncio.create_task(_execute_hooks_async(...))
```

**Benefits**:
- Conversation continues without database latency
- Better user experience (no pauses)
- Resilient to database failures

### 3. Template Variable Substitution
Variables are defined by `expected_payload_schema` and populated from lead payload:
```python
# Schema defines what variables are available
expected_payload_schema = {
    "customer_name": "string",
    "order_id": "string"
}

# Loader extracts only defined variables from payload
template_vars = {}
for field in expected_payload_schema.keys():
    if field in payload:
        template_vars[field] = payload[field]
```

### 4. Schema-Driven Validation
Both input and output are validated:
- **Input**: Lead payload validated against `expected_payload_schema`
- **Output**: Callback data validated against `expected_callback_response_schema`

### 5. Extensibility
New capabilities can be added without code changes:
- **New Templates**: Add via API without deployment
- **New Hooks**: Implement Hook class and register
- **New Handlers**: Add to handler_map
- **New Nodes**: Define in template JSON

---

## Best Practices

### Template Design

1. **Keep Nodes Focused**
   - Each node should have one clear purpose
   - Avoid complex multi-step logic in single node

2. **Use Descriptive Names**
   - Node names: `verify_order_detail_node`
   - Function names: `confirm_order`, `update_pincode`

3. **Leverage Pre/Post Actions**
   - Use `mute_stt` when playing important TTS
   - Use `play_audio_sound` for transitions

4. **Design Clear Function Descriptions**
   - LLM uses description to decide when to call
   - Be specific about when function should be invoked

5. **Use Hooks for Side Effects**
   - Database updates
   - External API calls
   - Analytics tracking

### Hook Development

1. **Always Use `safe_execute`**
   - Provides error handling
   - Logs exceptions without crashing

2. **Update Context After Changes**
   ```python
   updated_lead = await update_lead_call_completion_details(...)
   if updated_lead:
       context.bot.lead = updated_lead
   ```

3. **Use `expected_fields` Correctly**
   - `source: "static"` for enforced values
   - `source: "llm"` for LLM-extracted values

### Performance Optimization

1. **Minimize Database Queries**
   - Load template once at call start
   - Reuse template variables

2. **Use Async Hooks**
   - Don't block transitions
   - Fire-and-forget for non-critical operations

3. **Cache Static Data**
   - Template configurations
   - Call execution configs

### Error Handling

1. **Validate Early**
   - Check payload at insertion
   - Validate template structure at creation

2. **Graceful Degradation**
   - Continue call if hook fails
   - Log errors for debugging

3. **Use OpenTelemetry**
   - Trace entire call flow
   - Monitor hook execution times

---

## Conclusion

The Breeze Buddy template-based architecture provides a powerful and flexible system for building voice workflows. By separating concerns into templates, handlers, and hooks, it enables:

1. **Non-Technical Configuration**: Business users can design flows via JSON
2. **Developer Extensibility**: Engineers can add hooks and handlers as needed
3. **Scalability**: Async design handles high call volumes
4. **Maintainability**: Clear separation makes code easy to understand and modify
5. **Observability**: Built-in tracing and logging for debugging

The system successfully balances configurability with control, making it suitable for a wide range of voice automation use cases beyond order confirmation.
