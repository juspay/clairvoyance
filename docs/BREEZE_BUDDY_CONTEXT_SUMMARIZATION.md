# Breeze Buddy Context Summarization

## Overview

Context summarization has been implemented for Breeze Buddy to automatically manage conversation context and prevent context window overflow in long conversations. 

**Important:** Context summarization is **opt-in only**. It is **NOT enabled by default** and must be **explicitly configured in the template** to be active.

## What is Context Summarization?

Context summarization automatically condenses older conversation messages into a summary after a specified number of user turns, while keeping recent messages in full detail. This allows the AI to maintain conversation memory without exceeding token limits.

## Configuration (Template-Level Only)

Context summarization is configured **exclusively at the template level**. Add to your template's `configurations` object:

```json
{
  "configurations": {
    "context_summarization": {
      "enabled": true,
      "max_turns_before_summary": 10,
      "keep_recent_turns": 2
    }
  }
}
```

**Parameters:**
- `enabled` (boolean, default: `false`) - **Must be explicitly set to `true`** to enable summarization
- `max_turns_before_summary` (integer, default: `10`) - Number of user turns after which summarization is triggered
- `keep_recent_turns` (integer, default: `2`) - Number of recent conversation turns to keep in full detail

**No Configuration = No Summarization**

If `context_summarization` is not specified in the template configurations, the feature will **NOT be enabled**. This is intentional to give templates full control over their behavior.

**Example Configurations:**

```json
// Enable with default settings
{
  "context_summarization": {
    "enabled": true
  }
}

// Short feedback collection - aggressive summarization
{
  "context_summarization": {
    "enabled": true,
    "max_turns_before_summary": 5,
    "keep_recent_turns": 1
  }
}

// Complex troubleshooting - keep more context
{
  "context_summarization": {
    "enabled": true,
    "max_turns_before_summary": 20,
    "keep_recent_turns": 5
  }
}

// Explicitly disable (same as not configuring at all)
{
  "context_summarization": {
    "enabled": false
  }
}
```

## How It Works

### Workflow

1. **Turn Tracking**: Each time a user message is added to the context, the turn counter increments
2. **Threshold Check**: When the turn count reaches the configured threshold, summarization is triggered
3. **Message Separation**: 
   - Recent messages (last `keep_recent_turns` user-assistant pairs) are preserved in full
   - Older messages are sent to the LLM for summarization
4. **Summary Generation**: The LLM generates a comprehensive summary focusing on:
   - Customer's NAME (never forget - it is critical)
   - ORDER DETAILS (IDs, products, quantities, prices)
   - CUSTOMER DETAILS (preferences, requests, complaints)
   - ADDRESSES (complete delivery/pickup addresses)
   - Contact INFORMATION (phone, email)
   - Dates, times, and SCHEDULES
   - CONFIRMATIONS and VERIFICATIONS
   - PROMISES and COMMITMENTS
   - ISSUES and CONCERNS
   - RESOLUTIONS and actions taken
5. **Context Reconstruction**: 
   - All system messages preserved (template instructions, flow steps)
   - Summary added as a new system message
   - Recent messages kept in full detail
6. **Turn Reset**: Turn counter resets to 0 for the next summarization cycle

### Enhanced Summarization Prompt

The summarizer uses an enhanced prompt that ensures:

- ✅ **Customer name is ALWAYS preserved** - never forget the person's name
- ✅ **Order details are kept intact** - IDs, products, quantities, prices
- ✅ **Complete addresses are maintained** - delivery/pickup addresses
- ✅ **Contact information is saved** - phone, email
- ✅ **Dates, times, and schedules are preserved**
- ✅ **Confirmations and verifications are noted**
- ✅ **Promises and commitments are recorded**
- ✅ **Issues and concerns are documented**
- ✅ **Resolutions and actions taken are captured**

### Example

For a conversation with 12 user turns and `max_turns_before_summary=10`, `keep_recent_turns=2`:

**Before Summarization (12 messages):**
```
[System] You are Rhea from Freshbus...
[System] Politely greet the customer using their name Yugesh...
[User 1] Hi, I placed an order
[Assistant 1] Great! Can you provide your order ID?
[User 2] It's ORDER123
[Assistant 2] Thank you. Let me verify...
...
[User 10] Yes, that's correct
[Assistant 10] Perfect. Your address is 123 Main St, Apt 4B
[User 11] Can you repeat the delivery time?
[Assistant 11] It will arrive tomorrow at 2 PM
[User 12] Great, thank you
```

**After Summarization (6 messages):**
```
[System] You are Rhea from Freshbus...
[System] Politely greet the customer using their name Yugesh...
[System Summary] Customer Yugesh placed order ORDER123. Delivery address verified as 123 Main St, Apt 4B. Customer confirmed delivery time of tomorrow at 2 PM. Customer was polite and appreciative.
[User 11] Can you repeat the delivery time?
[Assistant 11] It will arrive tomorrow at 2 PM
[User 12] Great, thank you
```

## Implementation Details

### Files Created

1. **`app/ai/voice/agents/breeze_buddy/features/summarizer/context_summarizer.py`**
   - Main implementation of the `ContextSummarizer` class
   - Extends `OpenAILLMContext` from Pipecat
   - Automatically tracks user turns and triggers summarization
   - Uses enhanced prompt to preserve all critical details
   - Keeps ALL system messages (template instructions, flow guidance)

2. **`app/ai/voice/agents/breeze_buddy/features/summarizer/__init__.py`**
   - Module initialization file
   - Exports `ContextSummarizer` for easy imports

### Files Modified

1. **`app/ai/voice/agents/breeze_buddy/template/types.py`**
   - Added `ContextSummarizationConfig` class for template-level configuration
   - Added `context_summarization` field to `ConfigurationModel`
   - Default `enabled` is `False` - must be explicitly enabled

2. **`app/ai/voice/agents/breeze_buddy/agent/pipeline.py`**
   - Imported `ContextSummarizer`
   - Modified `build_pipeline()` to:
     - Only enable summarization when `context_summarization.enabled=true` in template config
     - No fallback to global config - opt-in only
   - Added logging for summarization configuration status

## Monitoring and Logging

The implementation includes detailed logging:

```python
# Configuration status
"Template context summarization ENABLED: max_turns=10, keep_turns=2"
"No template context summarization config - using standard context"

# Before summarization
"=== BREEZE BUDDY SUMMARIZATION START ==="
"Total messages in context BEFORE summarization: 22"
"System messages: 4, Conversation messages: 18"
"Messages to summarize: 14, Messages to keep: 4"

# After summarization
"✅ Generated summary (245 characters): Customer Yugesh..."
"=== BREEZE BUDDY SUMMARIZATION COMPLETE ==="
"Total messages in context AFTER summarization: 9"
"Messages reduced from 22 to 9"
"Space saved: 13 messages"
"Turn counter reset from 10 to 0"

# Turn tracking
"--- Breeze Buddy Summarizer: Turn count incremented to: 5 ---"

# Errors
"Breeze Buddy Summarizer: Error during summarization: <error>"
"Breeze Buddy Summarizer: Summary generation resulted in empty content."
```

## Benefits

1. **Extended Conversations**: Supports much longer conversations without context window overflow
2. **Complete Memory Preservation**: Critical information (names, orders, addresses) is retained via enhanced prompt
3. **Respectful Tone Maintained**: Summary ensures AI remembers to be polite and respectful
4. **Token Efficiency**: Reduces token usage while maintaining conversation coherence
5. **Performance**: Prevents slowdowns from excessively long context windows
6. **Cost Optimization**: Lower token usage means reduced API costs
7. **Template Flexibility**: Different templates can have different summarization strategies

## Testing Recommendations

1. **Basic Functionality**
   - Create a long conversation (>10 turns)
   - Verify summarization triggers at the correct turn count
   - Check that recent messages are preserved
   - Verify ALL system messages are preserved

2. **Data Preservation**
   - Verify customer name is always in the summary
   - Check order details are preserved accurately
   - Confirm addresses are maintained completely
   - Ensure phone numbers and emails are kept

3. **Configuration**
   - Test with template-level config
   - Test with summarization disabled
   - Test with different turn thresholds

4. **Edge Cases**
   - Very short conversations (< keep_recent_turns * 2)
   - Rapid-fire messages
   - Multiple system messages (template flow steps)
   - Conversations with many tool calls

## Troubleshooting

### Summarization Not Triggering

1. Check template config exists: `configurations.context_summarization`
2. Verify `enabled` is set to `true`: `configurations.context_summarization.enabled = true`
3. Check logs for configuration status message
4. Verify turn count in logs
5. Ensure conversation has enough messages (at least `keep_recent_turns * 2`)

### Missing Information in Summary

1. Check LLM service connectivity
2. Verify the summarization prompt is using enhanced version
3. Review logs for summary content
4. Check if critical details are in messages being summarized

### System Messages Being Lost

1. Verify you're using the latest implementation
2. Check logs for "System messages: X" count
3. Ensure all system messages are preserved after summarization

## Future Enhancements

Potential improvements for future iterations:

1. **Incremental Summarization**: Update summaries rather than regenerating
2. **Summary Quality Metrics**: Track and score summary quality automatically
3. **Domain-Specific Prompts**: Customize summary prompts per industry/use case
4. **Summary Caching**: Cache summaries to avoid redundant LLM calls
5. **Multi-Level Summaries**: Hierarchical summaries for very long conversations