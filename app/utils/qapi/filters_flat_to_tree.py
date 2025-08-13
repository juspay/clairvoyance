"""
Utilities for translating the public *flat* filter (Option-B) sent by Gemini
into the recursive *tree* filter that the /q API already understands.
"""

from __future__ import annotations
from typing import List, Dict, Any
import re
from pydantic import BaseModel


def flat_filter_to_tree(flat_filter) -> Dict[str, Any]:
    """
    Convert a FlatFilter (with .clauses: List[Clause], .logic: string like "(0 AND 1 AND 2)")
    into a nested AND/OR tree of plain dicts.
    """
    from app.agents.voice.automatic.types.models import FlatFilter, Clause

    def clause_to_dict(clause: Clause) -> Dict[str, Any]:
        val = clause.val
        # If it's a Pydantic model, dump it to primitives:
        if isinstance(val, BaseModel):
            val = val.model_dump(mode="json")
        return {
            "field": clause.field,
            "condition": clause.condition,
            "val": val,
        }

    clauses = flat_filter.clauses
    # split on AND/OR, keep the operators
    tokens = re.split(r"\s+(AND|OR)\s+", flat_filter.logic)
    # tokens might be e.g. ["(0", "AND", "1)", "OR", "2"]
    # strip parentheses from each token
    cleaned = [tok.strip("()") for tok in tokens if tok.strip("()") != ""]
    # cleaned = ["0", "AND", "1", "OR", "2"]

    # start with the first clause
    current = clause_to_dict(clauses[int(cleaned[0])])

    # fold left-associatively over the rest
    i = 1
    while i < len(cleaned):
        op = cleaned[i].lower()    # "and" or "or"
        idx = int(cleaned[i+1])    # next clause index
        right = clause_to_dict(clauses[idx])
        current = {
            op: {
                "left": current,
                "right": right
            }
        }
        i += 2

    return current
