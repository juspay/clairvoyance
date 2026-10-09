"""The custom evals: an agent's own evals, made by the merchant, as opposed
to the preset evals every agent can have. Each is one of the template's
CONVERSATION_EVALS rows, run after the call by the post-call worker in the
call's job, after its topics (conversation_analysis/custom/agent_evals.py).

  batch.py  several custom evals in one judge request, one per engine and
            model
"""
