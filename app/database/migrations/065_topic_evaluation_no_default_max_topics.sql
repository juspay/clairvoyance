UPDATE evaluation_config
SET configuration = jsonb_set(
    jsonb_set(
        configuration #- '{settings,max_topics}',
        '{model}',
        '"open-large-sa"'
    ),
    '{settings,max_output_tokens}',
    '16384'
)
WHERE evaluation_type = 'TOPIC'
  AND (
    template_id IS NULL
    OR configuration ->> 'model' = 'minimaxai/minimax-m2'
  );
