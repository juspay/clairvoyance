UPDATE evaluation_config
SET configuration = configuration || jsonb_build_object(
    'model', 'glm-5.3-flash',
    'settings', COALESCE(configuration -> 'settings', '{}'::jsonb)
        || '{"stream": true}'::jsonb
)
WHERE evaluation_type = 'TOPIC'
  AND COALESCE(configuration ->> 'provider', 'openai') = 'openai';
