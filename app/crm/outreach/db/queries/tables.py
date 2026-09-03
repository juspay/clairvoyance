"""The three physical table names outreach owns, in one leaf — so a builder
file that joins across tables imports the name it touches instead of the
sibling module that owns it (which is how two per-table files would import
each other). A reader sees a cross-table join by this file's import list.
"""

WORKFLOW_TABLE = "crm_workflow"
ENROLLMENT_TABLE = "crm_workflow_enrollment"
VERSION_TABLE = "crm_workflow_version"
