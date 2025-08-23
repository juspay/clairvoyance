# Delete Euler Offer Implementation Summary

## Overview

Successfully implemented delete and pause Euler offer functionality in the Clairvoyance voice agent system, following the same patterns as the existing create offer functionality.

## Implementation Details

### Files Modified
- `app/agents/voice/automatic/tools/juspay/analytics.py` - Added delete and pause offer functions

### New Functions Added

#### 1. `find_offer_by_code(offer_code: str) -> str | None`
- **Purpose**: Helper function to find an offer ID by its offer code
- **API Call**: `POST /api/offers/dashboard/dashboard-list`
- **Returns**: offer_id if found, None otherwise
- **Authentication**: Uses euler_token and merchant_id from global context

#### 2. `delete_euler_offer(params: FunctionCallParams)`
- **Purpose**: Permanently deletes a promotional offer from the Euler platform
- **Process**: 
  1. Find offer by code using `find_offer_by_code()`
  2. Delete offer using `POST /api/offers/dashboard/{offer_id}/delete`
- **Parameters**: `offerCode` (required)
- **Response**: Success/error message with offer details

#### 3. `pause_euler_offer(params: FunctionCallParams)`
- **Purpose**: Temporarily pauses a promotional offer (can be reactivated later)
- **Process**: 
  1. Find offer by code using `find_offer_by_code()`
  2. Pause offer using `POST /api/offers/dashboard/{offer_id}/pause`
- **Parameters**: `offerCode` (required)
- **Response**: Success/error message with offer details

### Function Schemas Added

#### 1. `delete_euler_offer_function`
```python
FunctionSchema(
    name="delete_euler_offer",
    description="Permanently deletes a promotional offer from the Euler platform based on its offer code. This action cannot be undone. Use this when you need to completely remove an offer from the system.",
    properties={
        "offerCode": {
            "type": "string",
            "description": "The unique offer code of the offer to delete. Examples: SAVE20, WELCOME10, NEWYEAR2025"
        }
    },
    required=["offerCode"]
)
```

#### 2. `pause_euler_offer_function`
```python
FunctionSchema(
    name="pause_euler_offer",
    description="Temporarily pauses a promotional offer in the Euler platform based on its offer code. This disables the offer but allows it to be reactivated later. Use this when you want to temporarily stop an offer without permanently deleting it.",
    properties={
        "offerCode": {
            "type": "string",
            "description": "The unique offer code of the offer to pause. Examples: SAVE20, WELCOME10, NEWYEAR2025"
        }
    },
    required=["offerCode"]
)
```

## API Integration Details

### Authentication Headers
```json
{
  "Content-Type": "application/json",
  "x-web-logintoken": "{euler_token}",
  "x-tenant-id": "jt_29bd8266cbdc4e76938cfaa2d80db4d6"
}
```

### API Endpoints Used

#### 1. Search Offer by Code
- **Endpoint**: `POST {EULER_DASHBOARD_API_URL}/api/offers/dashboard/dashboard-list?merchant_id={merchant_id}`
- **Payload**:
```json
{
  "merchant_id": "merchant_id",
  "offer_code": ["OFFER_CODE"],
  "limit": 1,
  "start_time": "2020-01-01T00:00:00Z",
  "end_time": "2050-01-01T00:00:00Z",
  "created_at": {
    "gte": "2020-01-01T00:00:00Z",
    "lte": "2050-01-01T00:00:00Z"
  },
  "sort_offers": {
    "order": "DESCENDING",
    "field": "CREATED_AT"
  }
}
```

#### 2. Delete Offer
- **Endpoint**: `POST {EULER_DASHBOARD_API_URL}/api/offers/dashboard/{offer_id}/delete`
- **Payload**:
```json
{
  "merchant_id": "merchant_id",
  "offer_id": "offer_id"
}
```

#### 3. Pause Offer
- **Endpoint**: `POST {EULER_DASHBOARD_API_URL}/api/offers/dashboard/{offer_id}/pause`
- **Payload**:
```json
{
  "merchant_id": "merchant_id",
  "offer_id": "offer_id"
}
```

## Error Handling

### Comprehensive Error Handling Implemented
1. **Authentication Validation**: Checks for euler_token and merchant_id
2. **Offer Not Found**: Clear error message when offer code doesn't exist
3. **API Failures**: Detailed HTTP error reporting
4. **Timeout Handling**: 10-second timeout with retry suggestion
5. **Exception Handling**: Catches and logs all unexpected errors

### Example Error Responses
```json
{
  "error": "Offer with code 'INVALID123' not found. Please verify the offer code and try again."
}
```

```json
{
  "error": "Authentication token is missing. Cannot delete offer."
}
```

## Success Response Format

### Delete Success Response
```json
{
  "status": "success",
  "message": "Successfully deleted offer 'SUMMER20'",
  "details": {
    "offerCode": "SUMMER20",
    "offerId": "offer_123456789",
    "action": "deleted"
  }
}
```

### Pause Success Response
```json
{
  "status": "success",
  "message": "Successfully paused offer 'SUMMER20'",
  "details": {
    "offerCode": "SUMMER20",
    "offerId": "offer_123456789",
    "action": "paused"
  }
}
```

## Integration with Existing System

### Tools Export Updated
```python
tools = ToolsSchema(
    standard_tools=[
        # ... existing tools ...
        create_euler_offer_function,
        delete_euler_offer_function,
        pause_euler_offer_function,
    ]
)
```

### Function Mapping Updated
```python
tool_functions = {
    # ... existing mappings ...
    "create_euler_offer": create_euler_offer,
    "delete_euler_offer": delete_euler_offer,
    "pause_euler_offer": pause_euler_offer,
}
```

## Usage Examples

### Delete an Offer
```python
# Voice agent can now process commands like:
# "Delete the offer SUMMER20"
# "Remove the promotional code WELCOME10"

# Function call:
delete_euler_offer({
    "offerCode": "SUMMER20"
})
```

### Pause an Offer
```python
# Voice agent can now process commands like:
# "Pause the offer WINTER25"
# "Temporarily disable the code NEWYEAR2025"

# Function call:
pause_euler_offer({
    "offerCode": "WINTER25"
})
```

## Key Features

1. **Two-Step Process**: Follows the documented pattern of finding offer by code, then performing the action
2. **Consistent Error Handling**: Same patterns as create_euler_offer function
3. **Comprehensive Logging**: Detailed logging for debugging and monitoring
4. **Authentication Integration**: Uses existing euler_token and merchant_id context
5. **Timeout Management**: 10-second timeout with proper error messages
6. **Response Formatting**: Consistent JSON response format

## Testing Verification

- ✅ Python syntax validation passed
- ✅ Function schemas properly defined
- ✅ Tool exports updated correctly
- ✅ Error handling implemented
- ✅ API integration follows documented patterns
- ✅ Logging and monitoring included

## Next Steps

The implementation is now ready for:
1. **Integration Testing**: Test with actual Euler API endpoints
2. **Voice Agent Testing**: Test voice commands for deleting/pausing offers
3. **Error Scenario Testing**: Test various error conditions
4. **Performance Testing**: Verify timeout and response handling

## Summary

The delete and pause Euler offer functionality has been successfully implemented following the exact same patterns as the existing create offer functionality. The implementation includes:

- Complete API integration with proper authentication
- Comprehensive error handling and logging
- Consistent response formatting
- Proper function schema definitions
- Integration with the existing tool system

Users can now delete or pause Euler offers using simple voice commands, with the same level of reliability and error handling as the create offer functionality.
