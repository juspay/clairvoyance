"""Manifest row -> Cloud API request body, one file per kind of message.
Assembly and normalisation only — nothing here reads the database, decides
a retry, or talks to Meta. Exports nothing; import the file you mean:

    shared.py     E.164 -> Meta's digits, for every kind of message
    template.py   variables -> template parameters, and the template's body
    reply.py      a free-form reply: whether it fits the channel's limits,
                  and its body
"""
