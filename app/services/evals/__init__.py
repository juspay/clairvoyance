"""The eval engine: validator, adapter and pluggable engines.

Generic by design — any Buddy service may judge anything through it, and
the evaluation type is the caller's: the row handed to the adapter names
it. Finished conversations are the first caller.

  definition.py         validates an eval's configuration before it is stored
  evaluator.py          the adapter: resolve the engine, run it, store the verdict
  engines/              one engine per kind of model: structured (Jev on
                        TypeSafe), prompt (chat models)
  preset/               the preset evals: global defaults an agent's own row
                        overrides, enabled or disabled
    outcome_correctness/  which of the agent's own outcome words the call
                          should have ended with (run at the end of a call by
                          Buddy's conversation_analysis/preset/outcome_eval.py)
  custom/               the custom evals an agent's merchant makes, run after
                        the call in its job, after its topics: batch.py runs
                        several in one judge request
  shared/               what preset and custom evals share
    utils/                extract_agent_outcomes: an agent's outcome words,
                          read from its template
"""
