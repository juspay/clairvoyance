# WebRTC Warm Handoff Flow Documentation

> [!NOTE]
> **This document applies to the Automatic Agent (Pipecat + Daily WebRTC).**
> For telephony-specific (Twilio, Plivo, Exotel) warm transfer architectures utilized by the Breeze Buddy agent, please see the individual documents located in the `docs/warm-transfer/` directory.

## Overview

This system implements a voice-based customer service bot that can transfer WebRTC customers to human agents via Daily.co links when unable to fulfill requests. The bot uses Pipecat Flows for conversation management and Daily for real-time communications.

## Flow Architecture

### Node Flow Diagram

```
                     ┌─────────────────────────────┐
                     │ initial_customer_interaction│
                     │ (Bot greets & offers help)  │
                     └──────────────┬──────────────┘
                                    │
                     ┌──────────────┴──────────────┐
                     │                             │
               Task Success                  Task Failure
                     │                             │
                     ▼                             ▼
        ┌─────────────────────────┐   ┌──────────────────────────┐
        │ continued_customer      │   │ transferring_to_human    │
        │ _interaction            │   │ (Hold music plays)       │
        │ (Ask if need more help) │   └───────────┬──────────────┘
        └──────┬──────────┬───────┘               │
               │          │                  Agent joins
         Task Success  Task Failure               │
               │          │                        ▼
               │          │           ┌──────────────────────────┐
               │          └──────────►│ human_agent_interaction  │
               │                      │ (Bot briefs agent)       │
               │                      └───────────┬──────────────┘
               │                                  │
               └──────────┐                 Agent ready
                          │                       │
                Customer says goodbye             ▼
                          │            ┌─────────────────────────┐
                          │            │ end_human_agent_conv    │
                          │            │ (Connect humans & exit) │
                          │            └─────────────────────────┘
                          ▼
                 ┌────────────────────┐
                 │ end_customer_conv  │
                 │ (Say goodbye & exit)│
                 └────────────────────┘
```

### Functional Flow Diagram

#### Success Path (Store Hours Request)

```
┌─────────────────────────────────┐
│ Bot starts                      │
│ • Create Daily room & tokens    │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Customer joins room             │
│ • on_first_participant_joined   │
│ • Bot initializes flow          │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot: "Hi! How can I help?       │
│      Store hours or order?"     │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Customer: "What are your hours?"│
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ LLM calls function:             │
│ check_store_location_and_hours  │
│ Returns: {status: "success"}    │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot: "123 Main St, 9am-5pm M-F. │
│      Anything else?"            │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Customer: "No, that's all"      │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ LLM calls:                      │
│ end_customer_conversation()     │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot: "Thanks for calling!"      │
│ Post-action: end_conversation   │
│ Bot terminates                  │
└─────────────────────────────────┘
```

#### Failure Path (Order Request → Agent Transfer)

```
┌─────────────────────────────────┐
│ Bot starts + Customer joins     │
│ (same as success path)          │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot: "Hi! How can I help?       │
│      Store hours or order?"     │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Customer: "I'd like to order"   │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ LLM calls: start_order()        │
│ Returns: {status: "error"}      │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ PRE-ACTIONS:                    │
│ • mute_customer()               │
│   canSend: [] (muted)           │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot: "Sorry! Transferring you   │
│      to an agent. Please hold." │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ POST-ACTIONS:                   │
│ • start_hold_music()            │
│ • make_customer_hear_only_music │
│ • print_human_agent_join_url()  │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Customer hears hold music 🎵    │
│ Bot waits for agent...          │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Agent clicks URL & joins        │
│ on_participant_joined fires     │
│ • Transition to agent briefing  │
│ • Context: RESET_WITH_SUMMARY   │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot → Agent: "Customer tried to │
│ order but system failed. Ready?"│
│                                 │
│ (Customer still hears music 🎵) │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Agent: "Yes, I'm ready"         │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ LLM calls:                      │
│ connect_human_agent_and_customer│
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot → Agent: "Patching you      │
│              through now..."    │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ POST-ACTIONS:                   │
│ • unmute_customer()             │
│ • connect customer ↔ agent audio│
│ • end_conversation              │
└────────────┬────────────────────┘
             │
             ▼
┌─────────────────────────────────┐
│ Bot exits                       │
│ Customer ↔ Agent talking 👥     │
└─────────────────────────────────┘
```

## Node Details

### 1. initial_customer_interaction

**Purpose:** First interaction with customer

**System Prompts:**

- Role: "You are an assistant for ABC Widget Company..."
- Task: Greet customer and offer two options:
  - Check store location and hours (always succeeds)
  - Start placing an order (always fails)

**Available Functions:**

- `check_store_location_and_hours_of_operation()` - Returns success
- `start_order()` - Returns error (intentional)
- `end_customer_conversation()` - Graceful exit

**Transitions:**

- Success → `continued_customer_interaction`
- Failure → `transferring_to_human_agent`

---

### 2. continued_customer_interaction

**Purpose:** Handle additional customer requests after first success

**System Prompt:**

- Ask if customer needs anything else
- Re-offer the same two options

**Available Functions:**

- Same as initial_customer_interaction

**Transitions:**

- Success → loops back to self
- Failure → `transferring_to_human_agent`

---

### 3. transferring_to_human_agent

**Purpose:** Prepare customer for agent handoff

**System Prompt:**

- Apologize for the issue
- Inform about transfer
- Ask to hold

**Pre-Actions:**

- `mute_customer()` - Revoke canSend permissions

**Post-Actions:**

- `start_hold_music()` - Spawn hold music subprocess
- `make_customer_hear_only_hold_music()` - Set canReceive to only hold-music user
- `print_human_agent_join_url()` - Log URL for agent to join

**Automatic Transition:**

- When agent joins → `human_agent_interaction`

---

### 4. human_agent_interaction

**Purpose:** Brief the agent about customer's issue

**System Prompt:**

- Greet agent
- Explain what customer tried to do
- Share error details
- Ask if agent is ready to connect

**Context Strategy:**

- Type: `RESET_WITH_SUMMARY`
- Summarizes entire customer conversation before briefing agent

**Available Functions:**

- `connect_human_agent_and_customer()` - Actually calls `end_human_agent_conversation`

**Transitions:**

- Agent ready → `end_human_agent_conversation`

---

### 5a. end_customer_conversation

**Purpose:** Graceful exit without agent involvement

**System Prompt:**

- Thank customer warmly
- Mention they can call back anytime

**Post-Actions:**

- `end_conversation` - Terminate bot

---

### 5b. end_human_agent_conversation

**Purpose:** Connect customer and agent, then exit

**System Prompt:**

- Tell agent they're being patched through

**Post-Actions:**

- `unmute_customer_and_make_humans_hear_each_other()` - Update permissions
- `end_conversation` - Bot leaves, humans remain connected

## Permission Management

### Bot Permissions (on join)

```json
{
  "owner": true,
  "canReceive": {
    "base": false,
    "byUserId": {
      "customer": true,
      "agent": true
    }
  }
}
```

Bot only hears customer and agent, not hold music.

### Customer Permissions Timeline

**Initial:**

```json
{
  "canReceive": {
    "base": false,
    "byUserId": { "bot": true }
  }
}
```

**During Transfer:**

```json
{
  "canSend": [],  // Muted
  "canReceive": {
    "byUserId": { "hold-music": true }
  }
}
```

**After Connection:**

```json
{
  "canSend": ["microphone"],  // Unmuted
  "canReceive": {
    "byUserId": { "agent": true }
  },
  "inputsEnabled": { "microphone": true }
}
```

### Agent Permissions Timeline

**Initial:**

```json
{
  "canReceive": {
    "base": false,
    "byUserId": { "bot": true }
  }
}
```

**After Connection:**

```json
{
  "canReceive": {
    "byUserId": { "customer": true }
  }
}
```

## Event Handlers

### on_first_participant_joined

- Assumes first participant is customer (not agent)
- Enables transcription capture
- Initializes flow with `initial_customer_interaction` node

### on_participant_joined

- Checks if joined participant is agent (`userId == "agent"`)
- If currently in `transferring_to_human_agent` node:
  - Transitions to `human_agent_interaction`

### on_participant_left

- Checks if all human participants (customer + agent) have left
- If none remain, cancels bot task

## Token Generation

All participants need Daily meeting tokens with specific `userId` values:

| User ID | Purpose | Owner | Initial Audio Permissions |
|---------|---------|-------|---------------------------|
| `bot` | AI Assistant | Yes | Hears customer + agent only |
| `customer` | Caller | No | Hears bot only |
| `agent` | Human support | No | Hears bot only |
| `hold-music` | Audio playback | No | Default |

## Hold Music System

Hold music is played via a separate subprocess:

```python
asyncio.create_subprocess_exec(
    sys.executable,
    "hold_music.py",
    "-m", room_url,
    "-t", token,
    "-i", "hold_music.wav"
)
```

The hold music player joins with `userId: "hold-music"` and plays audio that only the customer can hear during transfer.

## Function Handlers

### check_store_location_and_hours_of_operation()

- **Always succeeds**
- Returns hardcoded store info
- Next node determined by success status

### start_order()

- **Always fails** (intentional for demo)
- Returns error status
- Triggers agent transfer flow

### end_customer_conversation()

- Transitions to end node
- Used when customer is done and doesn't need agent

### end_human_agent_conversation()

- Transitions to end node
- Used after agent is briefed and ready to talk to customer

## Key Design Decisions

1. **User ID System**: Permissions are managed via Daily's `userId` feature, allowing granular control over who hears whom.

2. **Context Summarization**: When transitioning to agent briefing, the entire conversation is summarized so the agent gets context without hearing the full call.

3. **Mute via Permissions**: Customer is muted by revoking `canSend` permissions, preventing them from unmuting themselves.

4. **Bot as Owner**: Bot has owner permissions to manage other participants' permissions dynamically.

5. **Manual Agent Join**: Agent must manually click a URL printed to console—could be enhanced with automated notifications/paging.

## Pipeline Architecture

```
Audio Input → STT → Context Aggregator (User) → LLM → TTS → Audio Output → Context Aggregator (Assistant)
     ↑                                                                              ↓
     └──────────────────────────── Transport ─────────────────────────────────────┘
```

- **STT**: Deepgram
- **TTS**: Cartesia (voice: Newsman)
- **LLM**: Configurable (OpenAI, Anthropic, Google, AWS Bedrock)
- **Transport**: Daily WebRTC

## Environment Variables Required

```bash
DAILY_API_KEY=<daily_api_key>
DEEPGRAM_API_KEY=<deepgram_api_key>
CARTESIA_API_KEY=<cartesia_api_key>
LLM_PROVIDER=openai  # or anthropic, google, aws
# Provider-specific API keys (e.g., OPENAI_API_KEY)
```

## Potential Enhancements

1. **Database Integration**: Log conversations and transfer reasons
2. **Agent Queue System**: Support multiple agents with availability status
3. **Automated Paging**: Send SMS/email to on-call agent instead of console URL
4. **Customer Abandonment**: Handle customer leaving during hold
5. **Multiple Transfer Attempts**: Support escalation to different agent tiers
6. **Analytics**: Track transfer reasons, resolution times, customer satisfaction
