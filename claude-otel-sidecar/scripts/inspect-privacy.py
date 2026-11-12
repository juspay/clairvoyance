import json

d = json.load(open('test/fixtures/002-logs.json'))
logs = d['resourceLogs'][0]['scopeLogs'][0]['logRecords']
for k in ['claude_code.user_prompt', 'claude_code.assistant_response']:
    for lr in logs:
        body = lr.get('body', {}).get('stringValue', '')
        if body == k:
            print("=" * 30, k)
            print(json.dumps(lr, indent=1)[:2500])
            break

# also list every attribute key seen on api_request events (privacy inventory)
keys = set()
for lr in logs:
    for a in lr.get('attributes', []):
        keys.add(a['key'])
print("\nALL attribute keys ever seen on events:", sorted(keys))
