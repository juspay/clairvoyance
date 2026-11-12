import json, glob, sys

print("== exported logs ==")
for f in sorted(glob.glob('/tmp/ootlp-sink/*-logs.json')):
    d = json.load(open(f))
    for rl in d['resourceLogs']:
        svc = next((a['value']['stringValue'] for a in rl['resource']['attributes'] if a['key'] == 'service.name'), '?')
        for sl in rl['scopeLogs']:
            for lr in sl['logRecords']:
                attrs = {}
                for a in lr['attributes']:
                    attrs[a['key']] = next(iter(a['value'].values()))
                if attrs.get('proxy.record_kind') == 'request_final':
                    print(f, '| service:', svc, '| scope:', sl['scope']['name'])
                    for k in sorted(attrs):
                        print(f"   {k} = {attrs[k]}")

print("\n== exported span ==")
for f in sorted(glob.glob('/tmp/ootlp-sink/*-traces.json')):
    d = json.load(open(f))
    for rs in d['resourceSpans']:
        for ss in rs['scopeSpans']:
            for s in ss['spans']:
                print(f, '|', s['name'], '| kind', s['kind'], '| status', s['status'])
                for a in s['attributes']:
                    print(f"   {a['key']} = {next(iter(a['value'].values()))}")

print("\n== exported metrics ==")
for f in sorted(glob.glob('/tmp/ootlp-sink/*-metrics.json')):
    d = json.load(open(f))
    for rm in d['resourceMetrics']:
        svc = next((a['value']['stringValue'] for a in rm['resource']['attributes'] if a['key'] == 'service.name'), '?')
        for sm in rm['scopeMetrics']:
            for m in sm['metrics']:
                kind = 'sum' if 'sum' in m else 'histogram'
                if svc != 'neurolink_proxy':
                    print(f, '| passthrough:', m['name'])
                    continue
                print(f, '| {svc} | {m["name"]} ({kind})'.replace('{m["name"]}', m['name']))
                data = m[kind]['dataPoints']
                for dp in data:
                    labels = {a['key']: a['value'].get('stringValue') for a in dp['attributes']}
                    if 'asDouble' in dp:
                        print(f"     {labels} = {dp['asDouble']}")
                    else:
                        print(f"     {labels} count={dp.get('count')} sum={dp.get('sum')}")
