"""The custom evals: an agent's own evals, made by the merchant, as opposed
to the preset evals every agent can have. Each is one of the template's
CONVERSATION_EVALS rows. Not live yet: their API, queueing and worker come
next (see conversation_analysis/queue.py for what enabling them takes).

  batch.py  several custom evals in one judge request, one per engine and
            model
"""
