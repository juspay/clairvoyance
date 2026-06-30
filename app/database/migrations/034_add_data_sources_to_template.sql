ALTER TABLE template ADD COLUMN data_sources JSONB DEFAULT NULL;

COMMENT ON COLUMN template.data_sources IS
    'Array of inline DataSourceRef: [{name, spreadsheet_url, sheet_name?, columns?, format?, is_active}]';
