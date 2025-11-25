# Breeze Buddy Workflow System Documentation

## Table of Contents
1. [Overview](#overview)
2. [Current Architecture](#current-architecture)
3. [Code-Defined Voice Flow Model](#code-defined-voice-flow-model)
4. [Key Components](#key-components)
5. [Workflow Execution Flow](#workflow-execution-flow)
6. [Vision: Template-Driven Workflow Builder](#vision-template-driven-workflow-builder)
7. [Transformation Path](#transformation-path)
8. [Implementation Roadmap](#implementation-roadmap)

---

## Overview

Breeze Buddy is a telephony-based voice agent designed to handle workflow-driven conversations such as order confirmations. It integrates with telephony providers (Twilio and Exotel) to make outbound calls and uses Pipecat's conversational AI pipeline with Azure OpenAI LLM and ElevenLabs TTS.

**Current State**: Workflows are **code-defined** using Python classes and Pipecat Flows framework.

**Future Vision**: Transform to a **visual, template-driven workflow builder** similar to n8n, enabling merchant-level customization, reusable templates, and faster iteration without code changes.

---

## Current Architecture

### High-Level Architecture

```
┌─────────────────┐
│   Merchant API  │
│   (FastAPI)     │
└────────┬────────┘
         │
         ├─── POST /breeze-buddy/{identity}/{workflow}
         │    (Trigger order confirmation)
         │
         ├─── WS /breeze-buddy/{provider}/callback/{workflow}
         │    (WebSocket for call handling)
         │
         └─── Cron /breeze-buddy/cron/initiate
              (Process backlog leads)

┌─────────────────────────────────────────────────┐
│           Breeze Buddy Components               │
├─────────────────────────────────────────────────┤
│                                                 │
│  ┌───────────────────────────────────────┐     │
│  │    Call Managers (calls.py)           │     │
│  │  - Process backlog leads              │     │
│  │  - Handle call completion             │     │
│  │  - Manage retry logic                 │     │
│  └───────────────────────────────────────┘     │
│                    │                            │
│                    ▼                            │
│  ┌───────────────────────────────────────┐     │
│  │  Telephony Services                   │     │
│  │  - Twilio Provider                    │     │
│  │  - Exotel Provider                    │     │
│  └───────────────────────────────────────┘     │
│                    │                            │
│                    ▼                            │
│  ┌───────────────────────────────────────┐     │
│  │  Order Confirmation Workflow          │     │
│  │  (websocket_bot.py)                   │     │
│  │  - FlowManager                        │     │
│  │  - NodeConfig                         │     │
│  │  - Function Handlers                  │     │
│  └───────────────────────────────────────┘     │
│                    │                            │
│                    ▼                            │
│  ┌───────────────────────────────────────┐     │
│  │  Pipecat Pipeline                     │     │
│  │  STT → LLM → TTS → Output             │     │
│  └───────────────────────────────────────┘     │
│                                                 │
└─────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────┐
│           External Services                     │
├─────────────────────────────────────────────────┤
│  - Azure OpenAI (LLM)                           │
│  - ElevenLabs (TTS)                             │
│  - STT Service                                  │
│  - Twilio/Exotel (Telephony)                    │
│  - Database (PostgreSQL)                        │
└─────────────────────────────────────────────────┘
```

### Database Schema

The system uses three primary database tables:

1. **`lead_call_tracker`**: Tracks individual call attempts for each lead
   - `id`: Unique identifier
   - `merchant_id`: Merchant identifier
   - `workflow`: Workflow type (e.g., "order-confirmation")
   - `shop_identifier`: Shop identifier
   - `payload`: JSON payload with order details
   - `next_attempt_at`: Timestamp for next retry
   - `attempt_count`: Number of attempts made
   - `status`: Call status (PENDING, IN_PROGRESS, FINISHED)
   - `outcome`: Call outcome (CONFIRM, CANCEL, NO_ANSWER, BUSY, ADDRESS_UPDATED)
   - `call_id`: External call ID from provider
   - `recording_url`: URL to call recording
   - `metaData`: JSON metadata including transcription

2. **`call_execution_config`**: Configuration per merchant/workflow
   - `id`: Unique identifier
   - `merchant_id`: Merchant identifier
   - `workflow`: Workflow type
   - `shop_identifier`: Shop identifier
   - `initial_offset`: Seconds to wait before first attempt
   - `retry_offset`: Seconds to wait between retries
   - `call_start_time`: Start of calling window (time of day)
   - `call_end_time`: End of calling window (time of day)
   - `max_retry`: Maximum number of retry attempts
   - `calling_provider`: Provider to use (TWILIO or EXOTEL)
   - `enable_international_call`: Allow international calls

3. **`outbound_number`**: Available phone numbers for outbound calls
   - `id`: Unique identifier
   - `number`: Phone number
   - `provider`: Provider (TWILIO or EXOTEL)
   - `status`: Availability status
   - `channels`: Current active channels (for Exotel)
   - `maximum_channels`: Max concurrent calls (for Exotel)

---

## Code-Defined Voice Flow Model

### Current Workflow Definition

The workflow is currently defined in Python code within `websocket_bot.py`. The `OrderConfirmationBot` class contains:

#### 1. **Flow Configuration** (`_get_flow_config` method)

```python
def _get_flow_config(self):
    return {
        "initial_node": "initial",
        "nodes": {
            "initial": {
                "name": "initial",
                "task_messages": [...],
                "functions": initial_functions,
            },
            "verify_order_details": {
                "name": "verify_order_details",
                "task_messages": [...],
                "functions": order_functions,
                "pre_actions": [{"type": "tts_say", "text": "Okay."}]
            },
            "order_confirmation_and_end": {
                "name": "order_confirmation_and_end",
                "pre_actions": [{"type": "function", "handler": self._mute_stt_handler}],
                "task_messages": [...],
                "post_actions": [{"type": "function", "handler": self._end_conversation_handler}]
            },
            # ... more nodes
        }
    }
```

#### 2. **Node Types**

Each node represents a conversation state with:
- **name**: Unique identifier
- **task_messages**: System prompts for the LLM at this node
- **functions**: Available function tools (what the LLM can call)
- **pre_actions**: Actions to execute before node activation
- **post_actions**: Actions to execute after node completion

#### 3. **Function Handlers**

Function handlers define the actions available to the LLM:

```python
FlowsFunctionSchema(
    name="confirm_order",
    description="Call this function if the customer confirms the order",
    handler=self._confirm_order_handler,
    properties={},
    required=[],
)
```

When called, handlers return a tuple:
```python
async def _confirm_order_handler(self):
    self.outcome = "confirmed"
    return {}, self._create_node_from_config("order_confirmation_and_end")
```

#### 4. **Node Transitions**

Transitions happen through:
- **Function Handler Returns**: Handler returns next node config
- **Pre/Post Actions**: Executed before/after node activation

### Example: Order Confirmation Flow

```
┌─────────────────┐
│    initial      │  ← Greet customer, ask availability
└────────┬────────┘
         │
         ├─── user_available() ──────────────┐
         │                                   │
         ├─── user_busy() ───────────────┐   │
         │                               │   │
         └─── cancel_order() ────────┐   │   │
                                    │   │   │
                                    ▼   ▼   ▼
                         ┌──────────────────────────┐
                         │  verify_order_details    │
                         │  (Ask order confirmation) │
                         └──────────┬───────────────┘
                                    │
         ┌──────────────────────────┼───────────────────────┐
         │                          │                       │
         │                          │                       │
    confirm_order()        address_incorrect()    cancel_order()
         │                          │                       │
         ▼                          ▼                       ▼
┌────────────────────┐  ┌─────────────────┐  ┌────────────────────┐
│ order_confirmation │  │  update_address │  │ order_cancellation │
│     _and_end       │  │                 │  │     _and_end       │
└────────────────────┘  └────────┬────────┘  └────────────────────┘
                                 │
                        update_landmark()
                        update_pincode()
                        update_city()
                        update_locality()
                        update_phone_number()
                                 │
                                 ▼
                    ┌─────────────────────────┐
                    │ confirm_address_update  │
                    └─────────────────────────┘
```

---

## Key Components

### 1. **FlowManager** (from pipecat-ai-flows)

The `FlowManager` orchestrates the conversation flow:

```python
self.flow_manager = FlowManager(
    task=self.task,
    llm=llm,
    context_aggregator=context_aggregator,
    transport=self.transport,
)

# Initialize with starting node
await self.flow_manager.initialize(self._create_initial_node())
```

**Key Responsibilities**:
- Manages current conversation state (node)
- Executes pre/post actions
- Routes function calls to handlers
- Transitions between nodes based on handler responses

### 2. **NodeConfig**

Represents a single conversation state:

```python
NodeConfig(
    name="verify_order_details",
    task_messages=[
        {"role": "system", "content": "Now verify the order details..."}
    ],
    functions=[confirm_order_func, cancel_order_func, ...],
    pre_actions=[{"type": "tts_say", "text": "Okay."}],
    post_actions=[{"type": "function", "handler": self._end_conversation_handler}]
)
```

### 3. **Function Handlers**

Python async functions that:
- Process user intent (detected by LLM)
- Update internal state
- Return next node configuration

```python
@auto_trace("confirm_order")
async def _confirm_order_handler(self):
    logger.info("Order confirmed. Transitioning to confirmation node.")
    if self.outcome != "address_updated":
        self.outcome = "confirmed"
    return {}, self._create_node_from_config("order_confirmation_and_end")
```

### 4. **Pipecat Pipeline**

The underlying conversational AI pipeline:

```
WebSocket Input
    ↓
STT (Speech-to-Text)
    ↓
STT Mute Filter
    ↓
Context Aggregator (User)
    ↓
LLM (Azure OpenAI) ← FlowManager injects context & functions
    ↓
TTS (ElevenLabs)
    ↓
WebSocket Output
    ↓
Context Aggregator (Assistant)
```

### 5. **Call Managers**

Background processes that:
- **`process_backlog_leads`**: Cron job to process queued calls
- **`handle_call_completion`**: Cleanup after call ends
- **`handle_unanswered_calls`**: Retry logic for failed calls
- **`update_call_recording`**: Save recording URLs

---

## Workflow Execution Flow

### 1. **Trigger Call** (API Request)

```
POST /agent/voice/breeze-buddy/{identity}/order-confirmation
{
    "order_id": "ORD123",
    "customer_name": "John Doe",
    "customer_mobile_number": "+919876543210",
    "shop_name": "Acme Store",
    "total_price": 1500,
    "customer_address": "123 Main St, Mumbai, 400001",
    "order_data": {
        "items": [
            {"product_name": "Product A", "quantity": 2}
        ]
    },
    "reporting_webhook_url": "https://merchant.com/webhook"
}
```

**Response**:
```json
{
    "status": "queued",
    "lead_call_tracker_id": "uuid",
    "order_id": "ORD123",
    "message": "Call request added to queue for processing"
}
```

### 2. **Lead Processing** (Cron Job)

```
GET /agent/voice/breeze-buddy/cron/initiate
```

The cron job:
1. Fetches pending leads from `lead_call_tracker` table
2. Checks calling hours and availability
3. Acquires outbound number from `outbound_number` table
4. Initiates call via telephony provider (Twilio/Exotel)

### 3. **Call Initiation**

```python
async def _initiate_call(lead, config, outbound_number):
    # Get telephony provider
    provider = get_voice_provider(config.calling_provider, session)
    
    # Make call with WebSocket callback URL
    call_response = await provider.make_call(
        customer_mobile_number=lead.payload["customer_mobile_number"],
        outbound_number=outbound_number.number
    )
    
    # Update lead with call_id
    await update_lead_call_details(lead.id, call_response.call_sid, ...)
```

### 4. **WebSocket Connection**

When the call connects, the provider opens a WebSocket to:
```
WS /agent/voice/breeze-buddy/{provider}/callback/order-confirmation
```

The `OrderConfirmationBot` is instantiated and runs:

```python
async def run(self):
    await self.ws.accept()
    
    # Receive call data (call_sid, stream_sid)
    call_data = await self.ws.receive_json()
    
    # Fetch lead details from database
    lead = await get_lead_by_call_id(call_sid)
    
    # Extract order details from payload
    order_id = lead.payload["order_id"]
    customer_name = lead.payload["customer_name"]
    # ...
    
    # Set up Pipecat pipeline
    transport = FastAPIWebsocketTransport(...)
    stt = get_stt_service()
    llm = AzureLLMService(...)
    tts = ElevenLabsTTSService(...)
    
    # Create FlowManager
    self.flow_manager = FlowManager(
        task=self.task,
        llm=llm,
        context_aggregator=context_aggregator,
        transport=self.transport,
    )
    
    # Initialize with initial node
    await self.flow_manager.initialize(self._create_initial_node())
    
    # Run pipeline
    await runner.run(self.task)
```

### 5. **Conversation Flow Execution**

The conversation progresses through nodes:

```
1. Initial Node: "Hi John Doe, this is Rhea from Acme Store..."
   ↓ [LLM detects user_available()]
   
2. Verify Order Details: "Your order contains Product A..."
   ↓ [LLM detects confirm_order()]
   
3. Order Confirmation and End: "Thank you for confirming..."
   ↓ [_end_conversation_handler()]
   
4. Finalize Call:
   - Extract transcription
   - Update database with outcome
   - Send webhook to merchant
   - Hangup call
```

### 6. **Call Completion**

When the conversation ends:

```python
async def _finalize_call(self):
    # Extract transcription from context
    transcription = [msg for msg in self.context.messages]
    
    # Determine outcome
    call_outcome = OUTCOME_TO_ENUM.get(self.outcome, LeadCallOutcome.BUSY)
    
    # Update database
    await update_lead_call_completion_details(
        call_id=self.call_sid,
        status=LeadCallStatus.FINISHED,
        outcome=call_outcome,
        transcription={"messages": transcription},
        updated_address=self.updated_address,
        cancellation_reason=self.cancellation_reason,
    )
    
    # Send webhook to merchant
    if self.reporting_webhook_url:
        await send_webhook_with_retry(
            url=self.reporting_webhook_url,
            payload={
                "order_id": self.order_id,
                "outcome": self.outcome,
                "transcription": filtered_transcript,
                "updated_fields": self.updated_fields,
                ...
            }
        )
    
    # Release outbound number
    await _release_number(outbound_number_id, provider)
```

---

## Vision: Template-Driven Workflow Builder

Transform Breeze Buddy into a **visual workflow builder** similar to n8n, where workflows are built using drag-and-drop nodes and compiled into executable Pipecat flows.

### Benefits

1. **Merchant-Level Customization**: Each merchant can customize their workflow
2. **Reusable Templates**: Pre-built workflow templates for common scenarios
3. **Faster Iteration**: Change workflows without code deployments
4. **Better Debugging**: Visual representation of conversation flows
5. **Versioning**: Track workflow changes over time
6. **Custom Checks**: Add merchant-specific validation before/after calls

### Architecture Vision

```
┌────────────────────────────────────────────────────┐
│         Workflow Builder UI (Frontend)             │
│                                                     │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐         │
│  │  Start   │→│  Greet   │→│  Verify  │         │
│  │  Node    │  │  Node    │  │  Node    │         │
│  └──────────┘  └──────────┘  └────┬─────┘         │
│                                    │               │
│                     ┌──────────────┴─────────┐     │
│                     │                        │     │
│              ┌──────▼─────┐          ┌───────▼────┐│
│              │  Confirm   │          │  Cancel    ││
│              │  Node      │          │  Node      ││
│              └────────────┘          └────────────┘│
│                                                     │
│  Drag-and-drop interface to build workflows        │
└─────────────────────┬──────────────────────────────┘
                      │
                      │ Save as JSON Template
                      ▼
┌─────────────────────────────────────────────────────┐
│         Workflow Template Storage (Database)        │
│                                                     │
│  {                                                  │
│    "workflow_id": "order-confirmation-v2",          │
│    "merchant_id": "merchant_123",                   │
│    "version": "2.0",                                │
│    "nodes": [                                       │
│      {                                              │
│        "id": "start",                               │
│        "type": "greeting",                          │
│        "config": {                                  │
│          "message": "Hi {{customer_name}}...",      │
│          "functions": ["user_available", ...]       │
│        },                                           │
│        "transitions": {                             │
│          "user_available": "verify_order",          │
│          "user_busy": "end_busy"                    │
│        }                                            │
│      },                                             │
│      ...                                            │
│    ]                                                │
│  }                                                  │
└─────────────────────┬───────────────────────────────┘
                      │
                      │ Compile at Runtime
                      ▼
┌─────────────────────────────────────────────────────┐
│       Workflow Compiler (Runtime Engine)            │
│                                                     │
│  1. Load template from database                     │
│  2. Validate template structure                     │
│  3. Generate NodeConfig objects                     │
│  4. Instantiate function handlers                   │
│  5. Build FlowManager configuration                 │
│  6. Execute with Pipecat                            │
└─────────────────────────────────────────────────────┘
```

### Workflow Template Format

```json
{
  "workflow_id": "order-confirmation-v2",
  "merchant_id": "merchant_123",
  "shop_identifier": "shop_001",
  "version": "2.0",
  "metadata": {
    "name": "Order Confirmation Workflow",
    "description": "Confirms COD orders with customers",
    "created_at": "2024-01-01T00:00:00Z",
    "updated_at": "2024-01-15T00:00:00Z",
    "created_by": "admin@merchant.com"
  },
  "variables": {
    "customer_name": "{{payload.customer_name}}",
    "shop_name": "{{payload.shop_name}}",
    "order_summary": "{{payload.order_summary}}",
    "total_price": "{{payload.total_price}}",
    "delivery_address": "{{payload.customer_address}}"
  },
  "nodes": [
    {
      "id": "start",
      "type": "greeting",
      "name": "initial",
      "config": {
        "system_prompt": "You are Rhea, a friendly customer care representative...",
        "message": "Hi {{customer_name}}, this is Rhea from {{shop_name}}. I'm calling to confirm your order. Is it a good time to talk?",
        "functions": [
          {
            "name": "user_available",
            "description": "Call when user confirms availability",
            "parameters": {}
          },
          {
            "name": "user_busy",
            "description": "Call when user is busy",
            "parameters": {}
          },
          {
            "name": "cancel_order",
            "description": "Call if customer wants to cancel",
            "parameters": {
              "reason": {"type": "string"}
            }
          }
        ]
      },
      "transitions": {
        "user_available": "verify_order",
        "user_busy": "end_busy",
        "cancel_order": "end_cancelled"
      },
      "pre_actions": [],
      "post_actions": []
    },
    {
      "id": "verify_order",
      "type": "verification",
      "name": "verify_order_details",
      "config": {
        "message": "Your order contains {{order_summary}} for ₹{{total_price}}. The delivery address is {{delivery_address}}. Can you confirm?",
        "functions": [
          {
            "name": "confirm_order",
            "description": "Call when customer confirms",
            "parameters": {}
          },
          {
            "name": "cancel_order",
            "description": "Call if customer wants to cancel",
            "parameters": {
              "reason": {"type": "string"}
            }
          },
          {
            "name": "address_incorrect",
            "description": "Call if address is wrong",
            "parameters": {}
          }
        ]
      },
      "transitions": {
        "confirm_order": "end_confirmed",
        "cancel_order": "end_cancelled",
        "address_incorrect": "update_address"
      },
      "pre_actions": [
        {
          "type": "tts_say",
          "text": "Okay."
        }
      ],
      "post_actions": []
    },
    {
      "id": "update_address",
      "type": "data_collection",
      "name": "update_address",
      "config": {
        "message": "Sure, I can help update the address. What would you like to change?",
        "functions": [
          {
            "name": "update_landmark",
            "parameters": {"landmark": {"type": "string"}}
          },
          {
            "name": "update_pincode",
            "parameters": {"pincode": {"type": "string"}}
          },
          {
            "name": "update_city",
            "parameters": {"city": {"type": "string"}}
          }
        ]
      },
      "transitions": {
        "update_landmark": "confirm_address_update",
        "update_pincode": "confirm_address_update",
        "update_city": "confirm_address_update"
      }
    },
    {
      "id": "confirm_address_update",
      "type": "verification",
      "name": "confirm_address_update",
      "config": {
        "message": "Got it. Your updated address is: {{updated_address}}. Should I confirm the order?",
        "functions": [
          {
            "name": "confirm_order",
            "parameters": {}
          },
          {
            "name": "cancel_order",
            "parameters": {"reason": {"type": "string"}}
          }
        ]
      },
      "transitions": {
        "confirm_order": "end_confirmed",
        "cancel_order": "end_cancelled"
      }
    },
    {
      "id": "end_confirmed",
      "type": "terminal",
      "name": "order_confirmation_and_end",
      "config": {
        "message": "Thank you for confirming your order. Your order will be delivered soon. Have a good day.",
        "outcome": "confirmed"
      },
      "pre_actions": [
        {
          "type": "function",
          "name": "mute_stt"
        }
      ],
      "post_actions": [
        {
          "type": "function",
          "name": "end_conversation"
        }
      ]
    },
    {
      "id": "end_cancelled",
      "type": "terminal",
      "name": "order_cancellation_and_end",
      "config": {
        "message": "I understand. I am cancelling your order. Thank you for your time.",
        "outcome": "cancelled"
      },
      "pre_actions": [
        {
          "type": "function",
          "name": "mute_stt"
        }
      ],
      "post_actions": [
        {
          "type": "function",
          "name": "end_conversation"
        }
      ]
    },
    {
      "id": "end_busy",
      "type": "terminal",
      "name": "user_busy_and_end",
      "config": {
        "message": "I understand. I will call you back later. Thank you.",
        "outcome": "busy"
      },
      "pre_actions": [
        {
          "type": "function",
          "name": "mute_stt"
        }
      ],
      "post_actions": [
        {
          "type": "function",
          "name": "end_conversation"
        }
      ]
    }
  ],
  "hooks": {
    "before_call": [
      {
        "type": "validation",
        "name": "validate_phone_number",
        "config": {
          "field": "customer_mobile_number",
          "pattern": "^\\+91[0-9]{10}$"
        }
      }
    ],
    "after_call": [
      {
        "type": "webhook",
        "name": "send_merchant_webhook",
        "config": {
          "url": "{{reporting_webhook_url}}",
          "method": "POST",
          "body": {
            "order_id": "{{order_id}}",
            "outcome": "{{outcome}}",
            "transcription": "{{transcription}}",
            "updated_fields": "{{updated_fields}}"
          }
        }
      }
    ]
  }
}
```

---

## Transformation Path

### Phase 1: Template Storage & Loading

**Goal**: Store workflows as JSON templates and load them at runtime.

**Changes Required**:

1. **Database Schema Updates**
   ```sql
   CREATE TABLE workflow_templates (
       id UUID PRIMARY KEY,
       workflow_id VARCHAR(255) NOT NULL,
       merchant_id VARCHAR(255) NOT NULL,
       shop_identifier VARCHAR(255),
       version VARCHAR(50) NOT NULL,
       name VARCHAR(255) NOT NULL,
       description TEXT,
       template_json JSONB NOT NULL,
       is_active BOOLEAN DEFAULT true,
       created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
       updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
       created_by VARCHAR(255),
       UNIQUE(merchant_id, shop_identifier, workflow_id, version)
   );
   
   CREATE INDEX idx_workflow_templates_merchant 
       ON workflow_templates(merchant_id, shop_identifier, is_active);
   ```

2. **Template Loader Service**
   ```python
   # app/agents/voice/breeze_buddy/services/workflow/template_loader.py
   
   class WorkflowTemplateLoader:
       async def load_template(self, merchant_id: str, workflow_id: str) -> dict:
           """Load workflow template from database"""
           template = await get_workflow_template(merchant_id, workflow_id)
           return self._validate_template(template)
       
       def _validate_template(self, template: dict) -> dict:
           """Validate template structure"""
           # Validate required fields
           # Validate node types
           # Validate transitions
           return template
   ```

3. **Template Compiler**
   ```python
   # app/agents/voice/breeze_buddy/services/workflow/compiler.py
   
   class WorkflowCompiler:
       def compile(self, template: dict, context: dict) -> dict:
           """Compile template into FlowManager configuration"""
           flow_config = {
               "initial_node": template["nodes"][0]["id"],
               "nodes": {}
           }
           
           for node in template["nodes"]:
               flow_config["nodes"][node["id"]] = self._compile_node(node, context)
           
           return flow_config
       
       def _compile_node(self, node: dict, context: dict) -> dict:
           """Compile a single node"""
           return {
               "name": node["name"],
               "task_messages": self._render_messages(node["config"]["message"], context),
               "functions": self._build_functions(node["config"]["functions"]),
               "pre_actions": self._compile_actions(node.get("pre_actions", [])),
               "post_actions": self._compile_actions(node.get("post_actions", [])),
           }
   ```

4. **Update OrderConfirmationBot**
   ```python
   class OrderConfirmationBot:
       def __init__(self, ws, ..., template_id=None):
           self.template_id = template_id or "default-order-confirmation"
           # ...
       
       async def run(self):
           # Load template
           loader = WorkflowTemplateLoader()
           template = await loader.load_template(merchant_id, self.template_id)
           
           # Compile template
           compiler = WorkflowCompiler()
           self.flow_config = compiler.compile(template, context={
               "customer_name": customer_name,
               "shop_name": self.shop_name,
               # ...
           })
           
           # Initialize FlowManager with compiled config
           await self.flow_manager.initialize(self._create_initial_node())
   ```

### Phase 2: Template Management API

**Goal**: Provide APIs to create, update, and manage workflow templates.

**New Endpoints**:

```python
# app/api/routers/workflow_templates.py

@router.post("/workflow-templates")
async def create_template(
    template: CreateWorkflowTemplateRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """Create a new workflow template"""
    pass

@router.get("/workflow-templates/{merchant_id}")
async def list_templates(
    merchant_id: str,
    current_user: TokenData = Depends(get_current_user),
):
    """List all templates for a merchant"""
    pass

@router.get("/workflow-templates/{merchant_id}/{workflow_id}")
async def get_template(
    merchant_id: str,
    workflow_id: str,
    version: Optional[str] = None,
    current_user: TokenData = Depends(get_current_user),
):
    """Get a specific template"""
    pass

@router.put("/workflow-templates/{template_id}")
async def update_template(
    template_id: str,
    template: UpdateWorkflowTemplateRequest,
    current_user: TokenData = Depends(get_current_user),
):
    """Update an existing template"""
    pass

@router.delete("/workflow-templates/{template_id}")
async def delete_template(
    template_id: str,
    current_user: TokenData = Depends(get_current_user),
):
    """Delete a template (soft delete)"""
    pass

@router.post("/workflow-templates/{template_id}/validate")
async def validate_template(
    template_id: str,
    current_user: TokenData = Depends(get_current_user),
):
    """Validate a template without executing it"""
    pass
```

### Phase 3: Visual Workflow Builder UI

**Goal**: Build a drag-and-drop interface for creating workflows.

**Components**:

1. **Frontend Application** (React + React Flow)
   ```
   /workflow-builder/
   ├── src/
   │   ├── components/
   │   │   ├── Canvas.tsx          # Main workflow canvas
   │   │   ├── NodePalette.tsx     # Available node types
   │   │   ├── NodeEditor.tsx      # Edit node properties
   │   │   ├── TransitionEditor.tsx # Configure transitions
   │   │   └── VariableEditor.tsx  # Manage variables
   │   ├── nodes/
   │   │   ├── GreetingNode.tsx
   │   │   ├── VerificationNode.tsx
   │   │   ├── DataCollectionNode.tsx
   │   │   └── TerminalNode.tsx
   │   ├── services/
   │   │   └── api.ts              # API client
   │   └── App.tsx
   ```

2. **Node Types**
   - **Greeting Node**: Initial greeting and availability check
   - **Verification Node**: Verify order details
   - **Data Collection Node**: Collect missing information
   - **Decision Node**: Branch based on conditions
   - **Terminal Node**: End conversation with outcome
   - **Custom Node**: Execute custom function

3. **Node Configuration Panel**
   - Message template editor
   - Function definitions
   - Pre/post actions
   - Transition rules
   - Validation rules

### Phase 4: Template Versioning & Rollback

**Goal**: Track changes and enable rollback to previous versions.

**Features**:
- Version history for each template
- Diff view between versions
- One-click rollback
- Deployment tracking (which version is active)
- Audit log of changes

**Database Updates**:
```sql
CREATE TABLE workflow_template_versions (
    id UUID PRIMARY KEY,
    template_id UUID REFERENCES workflow_templates(id),
    version VARCHAR(50) NOT NULL,
    template_json JSONB NOT NULL,
    change_summary TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_by VARCHAR(255)
);

CREATE TABLE workflow_deployments (
    id UUID PRIMARY KEY,
    template_id UUID REFERENCES workflow_templates(id),
    version VARCHAR(50) NOT NULL,
    deployed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    deployed_by VARCHAR(255),
    is_active BOOLEAN DEFAULT true
);
```

### Phase 5: Advanced Features

**Testing & Simulation**:
- Test workflow with simulated conversations
- Playback recorded conversations through new workflows
- A/B testing different workflow versions

**Analytics & Monitoring**:
- Node-level analytics (drop-off rates, transition frequencies)
- Conversation path visualization
- Performance metrics per node

**Custom Hooks & Extensions**:
- Before/after call hooks
- Custom validation functions
- Integration with external systems
- Merchant-specific plugins

---

## Implementation Roadmap

### Milestone 1: Foundation (Weeks 1-2)
- [ ] Design database schema for workflow templates
- [ ] Implement template storage and retrieval
- [ ] Create template loader service
- [ ] Build basic template compiler
- [ ] Update OrderConfirmationBot to use templates

### Milestone 2: Template Management (Weeks 3-4)
- [ ] Implement template management APIs
- [ ] Add validation logic for templates
- [ ] Create default templates for existing workflows
- [ ] Migrate existing code-based workflows to templates
- [ ] Add comprehensive error handling

### Milestone 3: Visual Builder MVP (Weeks 5-8)
- [ ] Set up React application with React Flow
- [ ] Implement basic node types (Greeting, Verification, Terminal)
- [ ] Build canvas for dragging and connecting nodes
- [ ] Create node configuration panels
- [ ] Implement template save/load functionality
- [ ] Add basic validation in UI

### Milestone 4: Advanced Builder Features (Weeks 9-12)
- [ ] Add all node types (Data Collection, Decision, Custom)
- [ ] Implement variable management
- [ ] Build transition editor
- [ ] Add pre/post action configuration
- [ ] Implement template validation with visual feedback
- [ ] Add template preview/test mode

### Milestone 5: Versioning & Production Ready (Weeks 13-16)
- [ ] Implement version control system
- [ ] Add diff view between versions
- [ ] Build rollback functionality
- [ ] Add audit logging
- [ ] Implement deployment tracking
- [ ] Comprehensive testing (unit, integration, E2E)
- [ ] Documentation and training materials

### Milestone 6: Advanced Features (Weeks 17-20)
- [ ] Add testing and simulation capabilities
- [ ] Implement analytics dashboard
- [ ] Build custom hooks system
- [ ] Add A/B testing framework
- [ ] Merchant-level customization controls
- [ ] Performance optimization

---

## Current Implementation Details

### File Structure

```
app/agents/voice/breeze_buddy/
├── managers/
│   └── calls.py                 # Call lifecycle management
├── services/
│   └── telephony/
│       ├── base_provider.py     # Abstract provider interface
│       ├── twilio/              # Twilio implementation
│       ├── exotel/              # Exotel implementation
│       └── utils.py             # Provider utilities
├── workflows/
│   └── order_confirmation/
│       ├── websocket_bot.py     # Main bot implementation
│       ├── types.py             # Data models
│       └── utils.py             # Helper functions
├── analytics/
│   └── tracing_setup.py         # OpenTelemetry tracing
└── stt/
    └── __init__.py              # STT service initialization
```

### Key Configuration

Environment variables for Breeze Buddy:

```bash
# Azure OpenAI
AZURE_OPENAI_API_KEY=...
AZURE_OPENAI_ENDPOINT=...
AZURE_BREEZE_BUDDY_OPENAI_MODEL=gpt-4o

# ElevenLabs TTS
ELEVENLABS_API_KEY=...
ELEVENLABS_BB_VOICE_ID=...
ELEVENLABS_MODEL_ID=eleven_turbo_v2_5
ELEVENLABS_VOICE_SPEED=1.0

# Telephony Providers
TWILIO_ACCOUNT_SID=...
TWILIO_AUTH_TOKEN=...
EXOTEL_API_KEY=...
EXOTEL_API_SECRET=...

# VAD Configuration
BREEZE_BUDDY_VAD_CONFIDENCE=0.7
BREEZE_BUDDY_VAD_START_SECS=0.3
BREEZE_BUDDY_VAD_STOP_SECS=0.8
BREEZE_BUDDY_VAD_MIN_VOLUME=0.6

# Features
ENABLE_BREEZE_BUDDY_TRACING=true
ENABLE_BREEZE_BUDDY_USER_INTERRUPTION=true
ENABLE_BREEZE_BUDDY_VERIFY_ORDER_PRE_ACTIONS=true
```

### Call States and Outcomes

**LeadCallStatus**:
- `PENDING`: Lead is in queue, waiting to be called
- `IN_PROGRESS`: Call is currently active
- `FINISHED`: Call has completed

**LeadCallOutcome**:
- `CONFIRM`: Customer confirmed the order
- `CANCEL`: Customer cancelled the order
- `NO_ANSWER`: Customer didn't answer
- `BUSY`: Customer was busy/unavailable
- `ADDRESS_UPDATED`: Customer updated their address and confirmed

### Retry Logic

```python
# From calls.py
async def _retry_call(lead, config, outcome):
    if lead.attempt_count < config.max_retry - 1:
        next_attempt_at = datetime.now(timezone.utc) + timedelta(
            seconds=config.retry_offset
        )
        await create_lead_call_tracker(
            id=str(uuid.uuid4()),
            merchant_id=lead.merchant_id,
            workflow=lead.workflow,
            shop_identifier=lead.shop_identifier,
            next_attempt_at=next_attempt_at,
            payload=lead.payload,
            attempt_count=lead.attempt_count + 1,
        )
```

**Retry Configuration** (per merchant):
- `initial_offset`: Delay before first call (seconds)
- `retry_offset`: Delay between retries (seconds)
- `max_retry`: Maximum number of retry attempts
- `call_start_time`: Start of calling window (time)
- `call_end_time`: End of calling window (time)

---

## Conclusion

Breeze Buddy currently implements a **code-defined workflow system** using Pipecat Flows with `FlowManager` and `NodeConfig`. The workflow logic is embedded in Python classes and requires code changes for any modifications.

The **future vision** is to transform this into a **visual, template-driven workflow builder** where:
- Merchants can customize workflows through a UI
- Workflows are stored as JSON templates in the database
- Templates are compiled at runtime into Pipecat flows
- No code changes are needed for workflow modifications
- Versioning, testing, and analytics are built-in

This transformation will unlock significant value:
- **Faster iteration**: Change workflows in minutes, not days
- **Merchant empowerment**: Allow merchants to tailor experiences
- **Reduced engineering overhead**: No deployments for workflow changes
- **Better observability**: Visual representation of conversation flows
- **Scalability**: Support thousands of custom workflows

The implementation roadmap spans approximately 20 weeks and involves database changes, API development, compiler implementation, and a full-featured visual workflow builder UI.

---

## Quick Reference

### Key Files

| File Path | Description |
|-----------|-------------|
| `app/api/routers/breeze_buddy.py` | FastAPI router for Breeze Buddy endpoints |
| `app/agents/voice/breeze_buddy/workflows/order_confirmation/websocket_bot.py` | Main bot implementation with workflow logic |
| `app/agents/voice/breeze_buddy/managers/calls.py` | Call lifecycle management and retry logic |
| `app/agents/voice/breeze_buddy/services/telephony/` | Telephony provider implementations |
| `app/database/queries/breeze_buddy/` | Database queries for lead tracking |
| `app/schemas.py` | Data models and enums |

### Key Classes

| Class | Purpose |
|-------|---------|
| `OrderConfirmationBot` | Main bot class that manages conversation flow |
| `FlowManager` | Pipecat Flows manager that orchestrates node transitions |
| `NodeConfig` | Configuration for a single conversation node |
| `VoiceCallProvider` | Abstract base for telephony providers |
| `TwilioProvider` / `ExotelProvider` | Concrete telephony implementations |

### Key Database Tables

| Table | Purpose |
|-------|---------|
| `lead_call_tracker` | Tracks individual call attempts |
| `call_execution_config` | Per-merchant call configuration |
| `outbound_number` | Available phone numbers for calls |
| `workflow_templates` (future) | Stores workflow templates |

### Key Endpoints

| Endpoint | Method | Purpose |
|----------|--------|---------|
| `/agent/voice/breeze-buddy/{identity}/{workflow}` | POST | Trigger a workflow call |
| `/agent/voice/breeze-buddy/{provider}/callback/{workflow}` | WS | WebSocket for call handling |
| `/agent/voice/breeze-buddy/cron/initiate` | GET | Process backlog leads |
| `/agent/voice/breeze-buddy/call-execution-config` | GET/POST/PUT | Manage call configs |
| `/agent/voice/breeze-buddy/outbound-number` | GET/POST/DELETE | Manage phone numbers |

### Environment Variables

| Variable | Purpose | Example |
|----------|---------|---------|
| `AZURE_OPENAI_API_KEY` | Azure OpenAI authentication | `sk-...` |
| `AZURE_BREEZE_BUDDY_OPENAI_MODEL` | Model for conversations | `gpt-4o` |
| `ELEVENLABS_API_KEY` | ElevenLabs TTS authentication | `el_...` |
| `ELEVENLABS_BB_VOICE_ID` | Voice ID for TTS | `21m00Tcm...` |
| `BREEZE_BUDDY_VAD_CONFIDENCE` | Voice activity detection threshold | `0.7` |
| `ENABLE_BREEZE_BUDDY_TRACING` | Enable OpenTelemetry tracing | `true` |

### Common Workflows

#### Adding a New Node Type

1. Define node in `_get_flow_config()`:
   ```python
   "my_new_node": {
       "name": "my_new_node",
       "task_messages": [{"role": "system", "content": "..."}],
       "functions": [function_schema, ...],
       "pre_actions": [...],
       "post_actions": [...]
   }
   ```

2. Add function handler:
   ```python
   async def _my_handler(self):
       # Update state
       self.outcome = "my_outcome"
       # Return next node
       return {}, self._create_node_from_config("next_node")
   ```

3. Add function schema to appropriate function list:
   ```python
   FlowsFunctionSchema(
       name="my_function",
       description="When to call this",
       handler=self._my_handler,
       properties={},
       required=[]
   )
   ```

#### Testing a Workflow Change

1. Update workflow definition in `websocket_bot.py`
2. Restart the server: `python run.py`
3. Trigger a test call via API or dashboard
4. Monitor logs: `tail -f logs/breeze_buddy.log`
5. Check database for call outcome: `SELECT * FROM lead_call_tracker WHERE call_id = '...'`

#### Debugging Call Issues

1. Check lead status: `SELECT * FROM lead_call_tracker WHERE id = '...'`
2. Verify call config: `SELECT * FROM call_execution_config WHERE merchant_id = '...'`
3. Check outbound numbers: `SELECT * FROM outbound_number WHERE status = 'AVAILABLE'`
4. Review logs with tracing enabled: `ENABLE_BREEZE_BUDDY_TRACING=true`
5. Listen to call recording if available

---

## Additional Resources

### Related Documentation
- [Pipecat AI Documentation](https://docs.pipecat.ai/)
- [Pipecat Flows Guide](https://github.com/pipecat-ai/pipecat-flows)
- [n8n Workflow Examples](https://docs.n8n.io/workflows/examples/)

### External Services
- [Azure OpenAI Service](https://learn.microsoft.com/en-us/azure/ai-services/openai/)
- [ElevenLabs API Documentation](https://docs.elevenlabs.io/)
- [Twilio Voice API](https://www.twilio.com/docs/voice)
- [Exotel API Documentation](https://developer.exotel.com/)

### Contact & Support
For questions or issues with Breeze Buddy workflows, please contact the development team or file an issue in the repository.
