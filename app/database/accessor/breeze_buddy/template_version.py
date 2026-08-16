"""
Accessor functions for template version history (read side).

Writes happen inside the template accessor's transactions — see
create_template / replace_template in accessor/breeze_buddy/template.py.
"""

from typing import List, Optional, Tuple

from app.core.logger import logger
from app.database.decoder.breeze_buddy.template_version import (
    decode_template_version,
    decode_template_version_metadata_list,
)
from app.database.queries import run_parameterized_query
from app.database.queries.breeze_buddy.template_version import (
    count_template_versions_query,
    get_template_version_query,
    list_template_versions_query,
)
from app.schemas.breeze_buddy.template import (
    TemplateVersionDetail,
    TemplateVersionMetadata,
)


async def list_template_versions(
    template_id: str, limit: int, offset: int
) -> Tuple[List[TemplateVersionMetadata], int]:
    """Newest-first metadata page + total count for the history panel."""
    try:
        query, values = list_template_versions_query(template_id, limit, offset)
        rows = await run_parameterized_query(query, values)
        count_query, count_values = count_template_versions_query(template_id)
        count_rows = await run_parameterized_query(count_query, count_values)
        total = count_rows[0]["total"] if count_rows else 0
        return decode_template_version_metadata_list(rows), total
    except Exception as e:
        logger.error(f"Error listing template versions for {template_id}: {e}")
        raise


async def get_template_version_by_number(
    template_id: str, version_number: int
) -> Optional[TemplateVersionDetail]:
    try:
        query, values = get_template_version_query(template_id, version_number)
        rows = await run_parameterized_query(query, values)
        if rows:
            return decode_template_version(rows[0])
        return None
    except Exception as e:
        logger.error(
            f"Error getting template version {version_number} for {template_id}: {e}"
        )
        raise
