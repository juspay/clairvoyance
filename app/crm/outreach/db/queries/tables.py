"""The physical table names outreach owns, in one leaf — so a builder file
that joins across tables imports the name it touches instead of the sibling
module that owns it (which is how two per-table files would import each
other). A reader sees a cross-table join by this file's import list.
"""

WORKFLOW_TABLE = "crm_workflow"
ENROLLMENT_TABLE = "crm_workflow_enrollment"
VERSION_TABLE = "crm_workflow_version"
STEP_TABLE = "crm_workflow_step"
SEGMENT_TABLE = "crm_segment"
SEGMENT_MEMBER_TABLE = "crm_segment_member"
CAMPAIGN_TABLE = "crm_campaign"
BROADCAST_TABLE = "crm_broadcast"
BROADCAST_RECIPIENT_TABLE = "crm_broadcast_recipient"
