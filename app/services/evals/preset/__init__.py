"""The preset evals: the ones every agent gets out of the box, as
opposed to an agent's custom evals.

Each is a global ``evaluation_config`` row (no template) seeded by a
migration: the default for every agent, which an agent's own row of the
same name overrides, enabled or disabled. An agent's custom evals (made by the merchant)
are its template's rows instead.

  outcome_correctness  which of the agent's own outcome words the call
                       should have ended with (migration 083)
"""
