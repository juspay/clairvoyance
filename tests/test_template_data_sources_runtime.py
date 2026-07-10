from typing import Any, cast

import pytest

from app.ai.voice.agents.breeze_buddy.template.types import (
    DatasetUse,
    FlowMode,
    GlobalCustomFunction,
    TemplateDataSourceRef,
)
from app.schemas.breeze_buddy.core import LeadCallTracker
from app.services.data_sources.models import RawData
from app.services.data_sources.normalizers import normalize


def test_dataset_model_defaults_to_runtime_csv():
    dataset = DatasetUse(
        target="faq",
        selector={"sheet_name": "FAQ"},
    )

    assert dataset.format == "csv"
    assert dataset.variable_name is None


def test_dataset_model_requires_sheet_name():
    with pytest.raises(ValueError):
        DatasetUse(
            target="faq",
            selector={},
        )


def test_dataset_model_rejects_removed_mode_field():
    with pytest.raises(ValueError):
        DatasetUse.model_validate(
            {
                "target": "overview",
                "selector": {"sheet_name": "Overview"},
                "mode": "template_var",
            }
        )


def test_template_data_source_ref_rejects_duplicate_targets():
    dataset = DatasetUse(
        target="faq",
        selector={"sheet_name": "FAQ"},
    )

    with pytest.raises(ValueError):
        TemplateDataSourceRef(
            name="health_sheet",
            data_source_id="ds_123",
            datasets=[dataset, dataset],
        )


@pytest.mark.asyncio
async def test_template_data_source_validation_reports_inactive_source(monkeypatch):
    from app.core.config import static

    monkeypatch.setattr(static, "JWT_SECRET_KEY", "test-secret")
    monkeypatch.setattr(static, "JWT_ALGORITHM", "HS256")

    from app.api.routers.breeze_buddy.templates import handlers

    class _DataSource:
        is_active = False
        reseller_id = "reseller_1"
        merchant_id = None

    called_with: dict[str, Any] = {}

    async def fake_get_data_source(data_source_id, include_inactive=False):
        called_with["data_source_id"] = data_source_id
        called_with["include_inactive"] = include_inactive
        return _DataSource()

    monkeypatch.setattr(handlers, "get_data_source_by_id", fake_get_data_source)

    ref = TemplateDataSourceRef(
        name="health_sheet",
        data_source_id="ds_123",
        datasets=[DatasetUse(target="faq", selector={"sheet_name": "FAQ"})],
    )

    with pytest.raises(ValueError, match="inactive"):
        await handlers._validate_template_data_sources([ref], "reseller_1", None)

    assert called_with == {"data_source_id": "ds_123", "include_inactive": True}


def test_cache_signature_changes_when_selected_tab_changes():
    from app.services.data_sources.cache import build_ref_signature

    first = TemplateDataSourceRef(
        name="health_sheet",
        data_source_id="ds_123",
        datasets=[DatasetUse(target="faq", selector={"sheet_name": "FAQ"})],
    )
    second = TemplateDataSourceRef(
        name="health_sheet",
        data_source_id="ds_123",
        datasets=[DatasetUse(target="faq", selector={"sheet_name": "FAQ V2"})],
    )

    assert build_ref_signature(first) != build_ref_signature(second)


def test_cache_signature_changes_when_source_fingerprint_changes():
    from app.services.data_sources.cache import build_ref_signature

    ref = TemplateDataSourceRef(
        name="health_sheet",
        data_source_id="ds_123",
        datasets=[DatasetUse(target="faq", selector={"sheet_name": "FAQ"})],
    )

    assert build_ref_signature(ref, "source_a") != build_ref_signature(ref, "source_b")


def test_get_data_source_by_id_query_defaults_to_active_sources():
    from app.database.queries.breeze_buddy.data_source import (
        get_data_source_by_id_query,
    )

    active_query, active_values = get_data_source_by_id_query("ds_123")
    all_query, all_values = get_data_source_by_id_query("ds_123", include_inactive=True)

    assert '"is_active" = TRUE' in active_query
    assert '"is_active" = TRUE' not in all_query
    assert active_values == ["ds_123"]
    assert all_values == ["ds_123"]


def test_list_data_sources_query_restricts_merchant_ids_and_keeps_global():
    from app.database.queries.breeze_buddy.data_source import list_data_sources_query

    query, values = list_data_sources_query(
        reseller_id="r1",
        merchant_ids=["m1", "m2"],
    )

    assert '"reseller_id" = $1' in query
    assert '"merchant_id" = ANY($2::text[]) OR "merchant_id" IS NULL' in query
    assert '"is_active" = TRUE' in query
    assert values == ["r1", ["m1", "m2"]]


def test_list_data_sources_query_supports_reseller_id_batch():
    from app.database.queries.breeze_buddy.data_source import list_data_sources_query

    query, values = list_data_sources_query(
        reseller_ids=["r1", "r2"],
        merchant_ids=["m1"],
    )

    assert '"reseller_id" = ANY($1::text[])' in query
    assert '"merchant_id" = ANY($2::text[]) OR "merchant_id" IS NULL' in query
    assert values == [["r1", "r2"], ["m1"]]


def test_list_data_sources_query_empty_merchant_filter_returns_global_only():
    from app.database.queries.breeze_buddy.data_source import list_data_sources_query

    query, values = list_data_sources_query(
        reseller_id="r1",
        merchant_ids=[],
    )

    assert '"merchant_id" IS NULL' in query
    assert '"merchant_id" = ANY' not in query
    assert values == ["r1"]


def test_google_sheets_column_labels_support_wide_sheets():
    from app.services.data_sources.adapters.google_sheets import _column_label

    assert _column_label(1) == "A"
    assert _column_label(26) == "Z"
    assert _column_label(27) == "AA"
    assert _column_label(702) == "ZZ"
    assert _column_label(703) == "AAA"


def test_google_sheets_selector_defaults_to_whole_tab():
    from app.services.data_sources.adapters.google_sheets import GoogleSheetsAdapter

    adapter = GoogleSheetsAdapter()

    assert adapter._range_for_selector({"sheet_name": "Clinical Protocol"}) == (
        "'Clinical Protocol'"
    )
    assert (
        adapter._range_for_selector({"sheet_name": "Clinical Protocol", "max_rows": 25})
        == "'Clinical Protocol'!1:25"
    )
    assert (
        adapter._range_for_selector(
            {"sheet_name": "Clinical Protocol", "range": "A1:C10"}
        )
        == "'Clinical Protocol'!A1:C10"
    )


def test_google_sheets_selector_rejects_range_with_max_rows():
    from app.services.data_sources.adapters.google_sheets import GoogleSheetsAdapter

    adapter = GoogleSheetsAdapter()

    with pytest.raises(ValueError, match="range and selector.max_rows"):
        DatasetUse(
            target="clinical_protocol",
            selector={
                "sheet_name": "Clinical Protocol",
                "range": "A1:C10",
                "max_rows": 25,
            },
        )

    with pytest.raises(Exception, match="range and selector.max_rows"):
        adapter._range_for_selector(
            {
                "sheet_name": "Clinical Protocol",
                "range": "A1:C10",
                "max_rows": 25,
            }
        )


def test_google_sheets_credentials_use_json_when_configured(monkeypatch):
    import app.services.data_sources.adapters.google_sheets as sheets

    class _Credentials:
        pass

    captured: dict[str, Any] = {}

    def fake_from_service_account_info(info, scopes):
        captured["info"] = info
        captured["scopes"] = scopes
        return _Credentials()

    def fail_default(*args, **kwargs):
        raise AssertionError(
            "ADC should not be used when GOOGLE_CREDENTIALS_JSON is set"
        )

    monkeypatch.setattr(
        sheets,
        "GOOGLE_CREDENTIALS_JSON",
        '{"type":"service_account","client_email":"svc@example.com"}',
    )
    monkeypatch.setattr(
        sheets.service_account.Credentials,
        "from_service_account_info",
        fake_from_service_account_info,
    )
    monkeypatch.setattr(sheets.google_auth, "default", fail_default)

    credentials = sheets.GoogleSheetsAdapter()._credentials()

    assert isinstance(credentials, _Credentials)
    assert captured == {
        "info": {"type": "service_account", "client_email": "svc@example.com"},
        "scopes": [sheets.SHEETS_SCOPE],
    }


def test_google_sheets_credentials_fall_back_to_adc(monkeypatch):
    import app.services.data_sources.adapters.google_sheets as sheets

    class _Credentials:
        pass

    adc_credentials = _Credentials()
    captured: dict[str, Any] = {}

    def fake_default(scopes):
        captured["scopes"] = scopes
        return adc_credentials, "project-id"

    monkeypatch.setattr(sheets, "GOOGLE_CREDENTIALS_JSON", "")
    monkeypatch.setattr(sheets.google_auth, "default", fake_default)

    credentials = sheets.GoogleSheetsAdapter()._credentials()

    assert credentials is adc_credentials
    assert captured == {"scopes": [sheets.SHEETS_SCOPE]}


def test_google_sheets_raw_values_disambiguate_duplicate_headers():
    from app.services.data_sources.adapters.google_sheets import GoogleSheetsAdapter

    raw = GoogleSheetsAdapter()._raw_from_values(
        [
            ["Name", "Name", ""],
            ["Aarokya", "Pitch", "note"],
        ]
    )

    assert raw.columns == ["Name", "Name_2", "column_3"]
    assert raw.rows == [{"Name": "Aarokya", "Name_2": "Pitch", "column_3": "note"}]


def test_google_sheets_batch_fetch_rejects_mismatched_response_lengths():
    from app.services.data_sources.adapters.google_sheets import GoogleSheetsAdapter

    class _Response:
        def raise_for_status(self):
            return None

        def json(self):
            return {"valueRanges": [{"values": [["id"], ["A"]]}]}

    class _Session:
        def get(self, *args, **kwargs):
            return _Response()

    adapter = GoogleSheetsAdapter()
    adapter._cached_session = cast(Any, _Session())

    with pytest.raises(Exception, match="did not match requested tabs"):
        adapter._fetch_datasets_sync(
            {"spreadsheet_url": "sheet-id"},
            [{"sheet_name": "A"}, {"sheet_name": "B"}],
        )


def test_normalize_renders_csv_content():
    dataset = DatasetUse(
        target="faq",
        selector={"sheet_name": "FAQ"},
        format="csv",
    )
    raw = RawData(
        columns=["question", "answer"],
        rows=[{"question": "What documents?", "answer": "Carry Aadhaar"}],
    )

    normalized = normalize(raw, dataset)

    assert normalized["format"] == "csv"
    assert "question,answer" in normalized["content"]
    assert "What documents?,Carry Aadhaar" in normalized["content"]


def test_normalize_csv_ignores_extra_row_keys():
    dataset = DatasetUse(
        target="faq",
        selector={"sheet_name": "FAQ"},
        format="csv",
    )
    raw = RawData(
        columns=["question"],
        rows=[{"question": "What documents?", "internal_note": "hidden"}],
    )

    normalized = normalize(raw, dataset)

    assert normalized["content"] == "question\r\nWhat documents?\r\n"


def test_normalize_renders_markdown_content_with_variable_name():
    dataset = DatasetUse(
        target="overview",
        selector={"sheet_name": "Overview"},
        format="markdown",
        variable_name="health_overview",
    )
    raw = RawData(
        columns=["topic", "value"],
        rows=[{"topic": "tone", "value": "warm"}],
    )

    normalized = normalize(raw, dataset)

    assert normalized == {
        "format": "markdown",
        "content": "| topic | value |\n| --- | --- |\n| tone | warm |",
        "variable_name": "health_overview",
    }


def test_normalize_markdown_renders_none_as_blank():
    dataset = DatasetUse(
        target="overview",
        selector={"sheet_name": "Overview"},
        format="markdown",
    )
    raw = RawData(columns=["topic", "value"], rows=[{"topic": "tone", "value": None}])

    normalized = normalize(raw, dataset)

    assert normalized["content"] == "| topic | value |\n| --- | --- |\n| tone |  |"


@pytest.mark.asyncio
async def test_loader_skips_data_sources_for_ivr_templates(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader

    async def fail_fetch(*args, **kwargs):
        raise AssertionError("IVR templates should not fetch data sources")

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_or_fetch_bundle",
        fail_fetch,
    )

    template = type(
        "Template",
        (),
        {
            "id": "template_123",
            "data_sources": [
                type(
                    "Ref",
                    (),
                    {
                        "is_active": True,
                    },
                )()
            ],
            "flow": {
                "mode": FlowMode.IVR.value,
                "nodes": {"root": {"prompt": "Hello"}},
            },
        },
    )()

    await FlowConfigLoader().load_data_sources(template, {})


@pytest.mark.asyncio
async def test_loader_adds_runtime_text_without_registering_builtin(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader

    async def fake_fetch(*args, **kwargs):
        return {
            "source": {"name": "health_sheet"},
            "datasets": {
                "faq": {
                    "format": "csv",
                    "content": "question,answer\nWhat documents?,Carry Aadhaar",
                }
            },
        }

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_or_fetch_bundle",
        fake_fetch,
    )

    flow: dict[str, Any] = {
        "mode": FlowMode.FLOW.value,
        "initial_node": "intake",
        "nodes": [{"node_name": "intake", "task_messages": []}],
        "global_functions": [],
    }
    template = type(
        "T",
        (),
        {
            "id": "t1",
            "data_sources": [
                TemplateDataSourceRef(
                    name="health_sheet",
                    data_source_id="ds1",
                    datasets=[
                        DatasetUse(
                            target="faq",
                            selector={"sheet_name": "FAQ"},
                        )
                    ],
                )
            ],
            "flow": flow,
        },
    )()

    await FlowConfigLoader().load_data_sources(template, {})

    assert flow["global_functions"] == []
    assert flow["_runtime_data"]["health_sheet"]["faq"]["content"] == (
        "question,answer\nWhat documents?,Carry Aadhaar"
    )


@pytest.mark.asyncio
async def test_loader_exposes_template_var(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader

    async def fake_fetch(*args, **kwargs):
        return {
            "source": {"name": "health_sheet"},
            "datasets": {
                "overview": {
                    "format": "markdown",
                    "variable_name": "health_overview",
                    "content": "Use this overview.",
                }
            },
        }

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_or_fetch_bundle",
        fake_fetch,
    )

    flow: dict[str, Any] = {
        "mode": FlowMode.FLOW.value,
        "initial_node": "intake",
        "nodes": [{"node_name": "intake", "task_messages": []}],
        "global_functions": [],
    }
    template = type(
        "T",
        (),
        {
            "id": "t1",
            "data_sources": [
                TemplateDataSourceRef(
                    name="health_sheet",
                    data_source_id="ds1",
                    datasets=[
                        DatasetUse(
                            target="overview",
                            selector={"sheet_name": "Overview"},
                            format="markdown",
                            variable_name="health_overview",
                        )
                    ],
                )
            ],
            "flow": flow,
        },
    )()

    template_vars = {}
    await FlowConfigLoader().load_data_sources(template, template_vars)

    assert template_vars["health_overview"] == "Use this overview."
    assert flow["global_functions"] == []


@pytest.mark.asyncio
async def test_loader_does_not_overwrite_existing_template_var(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader

    async def fake_fetch(*args, **kwargs):
        return {
            "source": {"name": "health_sheet"},
            "datasets": {
                "overview": {
                    "format": "csv",
                    "variable_name": "customer_name",
                    "content": "Overwritten",
                }
            },
        }

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_or_fetch_bundle",
        fake_fetch,
    )

    flow: dict[str, Any] = {
        "mode": FlowMode.FLOW.value,
        "initial_node": "intake",
        "nodes": [{"node_name": "intake", "task_messages": []}],
        "global_functions": [],
    }
    template = type(
        "T",
        (),
        {
            "id": "t1",
            "data_sources": [
                TemplateDataSourceRef(
                    name="health_sheet",
                    data_source_id="ds1",
                    datasets=[
                        DatasetUse(
                            target="overview",
                            selector={"sheet_name": "Overview"},
                            variable_name="customer_name",
                        )
                    ],
                )
            ],
            "flow": flow,
        },
    )()

    template_vars = {"customer_name": "Harsh"}
    await FlowConfigLoader().load_data_sources(template, template_vars)

    assert template_vars["customer_name"] == "Harsh"
    assert flow["_runtime_data"]["health_sheet"]["overview"]["content"] == "Overwritten"


@pytest.mark.asyncio
async def test_load_template_renders_flow_with_data_source_variable(monkeypatch):
    from app.ai.voice.agents.breeze_buddy.template.loader import FlowConfigLoader
    from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

    flow: dict[str, Any] = {
        "mode": FlowMode.FLOW.value,
        "initial_node": "intake",
        "nodes": [
            {
                "node_name": "intake",
                "task_messages": [
                    {
                        "role": "system",
                        "content": "Customer={customer_name}; KB={health_overview}",
                    }
                ],
                "role_messages": [],
            }
        ],
        "global_functions": [],
    }
    db_template = TemplateModel(
        id="t1",
        reseller_id="r1",
        name="template",
        flow=flow,
        data_sources=[
            TemplateDataSourceRef(
                name="health_sheet",
                data_source_id="ds1",
                datasets=[
                    DatasetUse(
                        target="overview",
                        selector={"sheet_name": "Overview"},
                        format="markdown",
                        variable_name="health_overview",
                    ),
                    DatasetUse(target="faq", selector={"sheet_name": "FAQ"}),
                ],
            )
        ],
    )

    async def fake_get_template(*args, **kwargs):
        return db_template

    async def fake_credentials(*args, **kwargs):
        return {}

    async def fake_fetch(*args, **kwargs):
        return {
            "source": {"name": "health_sheet"},
            "datasets": {
                "overview": {
                    "format": "markdown",
                    "variable_name": "health_overview",
                    "content": "Use this overview.",
                },
                "faq": {
                    "format": "csv",
                    "content": "question,answer\nWhat documents?,Carry Aadhaar",
                },
            },
        }

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_template_by_id_with_fallback",
        fake_get_template,
    )
    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_credentials_as_template_vars",
        fake_credentials,
    )
    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_or_fetch_bundle",
        fake_fetch,
    )

    template, template_vars = await FlowConfigLoader().load_template(
        reseller_id="r1",
        template="template",
        call_payload={"customer_name": "Harsh"},
        template_id="t1",
    )

    assert template_vars["customer_name"] == "Harsh"
    assert template_vars["health_overview"] == "Use this overview."
    assert template.flow["nodes"][0]["task_messages"][0]["content"] == (
        "Customer=Harsh; KB=Use this overview."
    )
    assert template.flow["_runtime_data"]["health_sheet"]["faq"]["content"] == (
        "question,answer\nWhat documents?,Carry Aadhaar"
    )


@pytest.mark.asyncio
async def test_custom_python_handler_can_read_runtime_data():
    from app.ai.voice.agents.breeze_buddy.handlers.internal.custom_python_code_handler import (
        custom_python_code_handler,
    )
    from app.ai.voice.agents.breeze_buddy.template.context import TemplateContext

    def handler(args, context):
        rows = context["data"]["health_sheet"]["faq"]["content"].splitlines()
        return {"first_row": rows[1], "asked": args["question"]}

    function_config = GlobalCustomFunction(
        name="answer_faq",
        description="Answer from fetched data.",
        python_code="def handler(args, context): return {}",
    )
    function_config.compiled_handler = handler

    bot = type(
        "Bot",
        (),
        {
            "lead": None,
            "call_sid": None,
            "runtime_data": {
                "health_sheet": {
                    "faq": {
                        "format": "csv",
                        "content": "question,answer\nWhat documents?,Carry Aadhaar",
                    }
                }
            },
        },
    )()

    result, transition = await custom_python_code_handler(
        TemplateContext(bot),
        {"question": "documents"},
        function_config=function_config,
    )

    assert transition is None
    assert result == {
        "status": "success",
        "data": {"first_row": "What documents?,Carry Aadhaar", "asked": "documents"},
    }


@pytest.mark.asyncio
async def test_chat_data_source_preparation_loads_runtime_data_without_cache_mutation(
    monkeypatch,
):
    from app.ai.voice.agents.breeze_buddy.chat.turn_core import (
        prepare_template_data_sources_for_chat,
    )
    from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel

    async def fake_fetch(*args, **kwargs):
        return {
            "source": {"name": "health_sheet"},
            "datasets": {
                "faq": {
                    "format": "csv",
                    "content": "question,answer\nWhat documents?,Carry Aadhaar",
                },
                "overview": {
                    "format": "markdown",
                    "variable_name": "health_overview",
                    "content": "Use this overview.",
                },
            },
        }

    monkeypatch.setattr(
        "app.ai.voice.agents.breeze_buddy.template.loader.get_or_fetch_bundle",
        fake_fetch,
    )

    template = TemplateModel(
        id="t1",
        reseller_id="r1",
        name="template",
        flow={
            "mode": FlowMode.FLOW.value,
            "initial_node": "intake",
            "nodes": [{"node_name": "intake", "task_messages": []}],
        },
        data_sources=[
            TemplateDataSourceRef(
                name="health_sheet",
                data_source_id="ds1",
                datasets=[
                    DatasetUse(target="faq", selector={"sheet_name": "FAQ"}),
                    DatasetUse(
                        target="overview",
                        selector={"sheet_name": "Overview"},
                        variable_name="health_overview",
                    ),
                ],
            )
        ],
    )

    template_vars = {}
    prepared, runtime_data = await prepare_template_data_sources_for_chat(
        template, template_vars
    )

    assert prepared is not template
    assert "_runtime_data" not in template.flow
    assert "_runtime_data" not in prepared.flow
    assert runtime_data["health_sheet"]["faq"]["content"] == (
        "question,answer\nWhat documents?,Carry Aadhaar"
    )
    assert template_vars["health_overview"] == "Use this overview."


def test_playground_flow_override_preserves_runtime_data():
    from app.ai.voice.agents.breeze_buddy.template.types import TemplateModel
    from app.ai.voice.agents.breeze_buddy.utils.playground import (
        apply_playground_overrides,
    )

    template = TemplateModel(
        id="t1",
        reseller_id="r1",
        name="template",
        flow={
            "mode": FlowMode.FLOW.value,
            "initial_node": "intake",
            "nodes": [{"node_name": "intake", "task_messages": []}],
            "_runtime_data": {"health_sheet": {"faq": {"content": "csv"}}},
        },
    )
    lead = LeadCallTracker(
        id="lead1",
        reseller_id="r1",
        template="template",
        metaData={
            "playground": True,
            "flow_override": {
                "mode": FlowMode.FLOW.value,
                "initial_node": "override",
                "nodes": [{"node_name": "override", "task_messages": []}],
            },
        },
    )

    updated = apply_playground_overrides(lead, template, {})

    assert updated.flow["_runtime_data"] == {
        "health_sheet": {"faq": {"content": "csv"}}
    }


@pytest.mark.asyncio
async def test_get_or_fetch_bundle_skips_cache_for_inactive_source(monkeypatch):
    from app.services.data_sources import runtime

    class _T:
        value = "google_sheet"

    class _DS:
        id = "ds1"
        is_active = False
        config: dict = {}
        source_type = _T()
        updated_at = None

    async def fake_get_ds(_id):
        return _DS()

    async def fail_cache(*args, **kwargs):
        raise AssertionError("inactive sources must not read from cache")

    monkeypatch.setattr(runtime, "get_data_source_by_id", fake_get_ds)
    monkeypatch.setattr(runtime, "get_cached_bundle", fail_cache)

    ref = TemplateDataSourceRef(
        name="src",
        data_source_id="ds1",
        datasets=[DatasetUse(target="faq", selector={"sheet_name": "FAQ"})],
    )

    assert await runtime.get_or_fetch_bundle("template_1", ref) is None


@pytest.mark.asyncio
async def test_fetch_bundle_isolates_failing_dataset(monkeypatch):
    from app.services.data_sources import runtime

    class _T:
        value = "google_sheet"

    class _DS:
        id = "ds1"
        is_active = True
        config: dict = {}
        source_type = _T()

    async def fake_get_ds(_id):
        return _DS()

    class _Adapter:
        async def fetch_dataset(self, config, selector):
            if selector["sheet_name"] == "Bad":
                raise RuntimeError("source failed")
            return RawData(columns=["id"], rows=[{"id": "B"}])

    monkeypatch.setattr(runtime, "get_data_source_by_id", fake_get_ds)
    monkeypatch.setattr(runtime, "get_adapter", lambda _t: _Adapter())

    ref = TemplateDataSourceRef(
        name="src",
        data_source_id="ds1",
        datasets=[
            DatasetUse(
                target="bad",
                selector={"sheet_name": "Bad"},
            ),
            DatasetUse(
                target="good",
                selector={"sheet_name": "Good"},
            ),
        ],
    )

    bundle = await runtime.fetch_bundle_for_ref(ref)
    assert bundle is not None
    assert "good" in bundle["datasets"]
    assert "bad" not in bundle["datasets"]
    assert bundle["datasets"]["good"]["content"] == "id\r\nB\r\n"


@pytest.mark.asyncio
async def test_fetch_bundle_uses_batch_fetch_when_available(monkeypatch):
    from app.services.data_sources import runtime

    class _T:
        value = "google_sheet"

    class _DS:
        id = "ds1"
        is_active = True
        config: dict = {}
        source_type = _T()

    async def fake_get_ds(_id):
        return _DS()

    class _Adapter:
        selectors = None

        async def fetch_datasets(self, config, selectors):
            self.selectors = selectors
            return [
                RawData(columns=["id"], rows=[{"id": "A"}]),
                RawData(columns=["id"], rows=[{"id": "B"}]),
            ]

        async def fetch_dataset(self, config, selector):
            raise AssertionError("single dataset fetch should not be used")

    adapter = _Adapter()
    monkeypatch.setattr(runtime, "get_data_source_by_id", fake_get_ds)
    monkeypatch.setattr(runtime, "get_adapter", lambda _t: adapter)

    ref = TemplateDataSourceRef(
        name="src",
        data_source_id="ds1",
        datasets=[
            DatasetUse(target="a", selector={"sheet_name": "A"}),
            DatasetUse(target="b", selector={"sheet_name": "B"}),
        ],
    )

    bundle = await runtime.fetch_bundle_for_ref(ref)

    assert adapter.selectors == [{"sheet_name": "A"}, {"sheet_name": "B"}]
    assert bundle is not None
    assert bundle["datasets"]["a"]["content"] == "id\r\nA\r\n"
    assert bundle["datasets"]["b"]["content"] == "id\r\nB\r\n"


@pytest.mark.asyncio
async def test_fetch_bundle_falls_back_when_batch_returns_partial_data(monkeypatch):
    from app.services.data_sources import runtime

    class _T:
        value = "google_sheet"

    class _DS:
        id = "ds1"
        is_active = True
        config: dict = {}
        source_type = _T()

    async def fake_get_ds(_id):
        return _DS()

    class _Adapter:
        single_fetches = []

        async def fetch_datasets(self, config, selectors):
            return [RawData(columns=["id"], rows=[{"id": "A"}])]

        async def fetch_dataset(self, config, selector):
            self.single_fetches.append(selector["sheet_name"])
            return RawData(columns=["id"], rows=[{"id": selector["sheet_name"]}])

    adapter = _Adapter()
    monkeypatch.setattr(runtime, "get_data_source_by_id", fake_get_ds)
    monkeypatch.setattr(runtime, "get_adapter", lambda _t: adapter)

    ref = TemplateDataSourceRef(
        name="src",
        data_source_id="ds1",
        datasets=[
            DatasetUse(target="a", selector={"sheet_name": "A"}),
            DatasetUse(target="b", selector={"sheet_name": "B"}),
        ],
    )

    bundle = await runtime.fetch_bundle_for_ref(ref)

    assert adapter.single_fetches == ["A", "B"]
    assert bundle is not None
    assert bundle["datasets"]["a"]["content"] == "id\r\nA\r\n"
    assert bundle["datasets"]["b"]["content"] == "id\r\nB\r\n"
