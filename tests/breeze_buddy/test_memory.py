import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, cast
from uuid import uuid4

import pytest

from app.ai.voice.agents.breeze_buddy.template.types import (
    ConfigurationModel,
    MemoryConfig,
)
from app.database.decoder.breeze_buddy.user_memory import decode_user_memory
from app.database.queries.breeze_buddy.customer_identity import upsert_alias_query
from app.database.queries.breeze_buddy.user_memory import (
    insert_user_memory_query,
    list_active_memories_query,
    purge_expired_memories_query,
    repoint_memory_key_query,
    search_active_memories_query,
    supersede_memory_query,
)
from app.database.vector import vector_literal
from app.schemas.breeze_buddy.knowledge_base import (
    EmbeddingConfig as KnowledgeBaseEmbeddingConfig,
)
from app.schemas.breeze_buddy.memory import MemoryEngineConfig
from app.schemas.embeddings import EmbeddingConfig


def test_template_memory_config_is_enablement_only():
    configurations = ConfigurationModel(memory=MemoryConfig(enabled=True))
    assert configurations.memory is not None
    assert configurations.memory.enabled

    with pytest.raises(ValueError):
        MemoryConfig.model_validate({"enabled": True, "backend": "pgvector"})
    with pytest.raises(ValueError):
        MemoryConfig.model_validate({"enabled": True, "retention_days": 45})


def test_global_engine_config_validates_policy_and_normalizes_region():
    config = MemoryEngineConfig(phone_default_region="in")
    assert config.backend == "pgvector"
    assert config.phone_default_region == "IN"
    assert config.retention_days == 180
    assert config.max_facts == 100
    assert config.embedding.dimensions == 768

    invalid = [
        {"backend": "unknown"},
        {"identity_field": " "},
        {"phone_default_region": "IND"},
        {"retention_days": 0},
        {"max_facts": 0},
        {"embedding": {"provider": "unknown"}},
    ]
    for values in invalid:
        with pytest.raises(ValueError):
            MemoryEngineConfig.model_validate(values)


def test_embedding_config_is_shared_with_knowledge_base():
    assert KnowledgeBaseEmbeddingConfig is EmbeddingConfig


def test_vector_literal_has_stable_parameter_shape():
    assert vector_literal([0.1, -0.25]) == "[0.10000000,-0.25000000]"


def test_insert_query_is_parameterized_halfvec_and_idempotent():
    expires_at = datetime.now(timezone.utc)
    query, values = insert_user_memory_query(
        reseller_id="reseller",
        merchant_id="merchant",
        customer_key="customer",
        key_type="customer_id",
        fact="Prefers morning calls",
        category="preference",
        structured={"window": "morning"},
        embedding=[0.1, 0.2],
        source_channel="voice",
        confidence=0.75,
        operation_key="lead:1:0",
        expires_at=expires_at,
    )

    assert "$8::halfvec(768)" in query
    assert "ON CONFLICT" in query
    assert "Prefers morning calls" not in query
    assert values[:3] == ["reseller", "merchant", "customer"]
    assert json.loads(values[6]) == {"window": "morning"}
    assert values[7] == "[0.10000000,0.20000000]"
    assert values[10:] == ["lead:1:0", expires_at]


def test_profile_and_search_queries_are_tenant_scoped_and_expiry_aware():
    profile_query, profile_values = list_active_memories_query(
        "reseller", "merchant", "customer", 20
    )
    search_query, search_values = search_active_memories_query(
        "reseller", "merchant", "customer", [0.1, 0.2], 5
    )

    for query in (profile_query, search_query):
        assert "reseller_id = $1" in query
        assert "merchant_id = $2" in query
        assert "customer_key = $3" in query
        assert "superseded_at IS NULL" in query
        assert "expires_at > now()" in query

    assert "ORDER BY confidence DESC" in profile_query
    assert "ORDER BY embedding <=> $4::halfvec(768)" in search_query
    assert profile_values == ["reseller", "merchant", "customer", 20]
    assert search_values[:3] == ["reseller", "merchant", "customer"]


def test_update_queries_preserve_tenant_scope():
    supersede_query, supersede_values = supersede_memory_query(
        "reseller", "merchant", "customer", "memory-id"
    )
    repoint_query, repoint_values = repoint_memory_key_query(
        "reseller", "merchant", "phone:+14155550100", "customer"
    )

    for query in (supersede_query, repoint_query):
        assert "reseller_id = $1" in query
        assert "merchant_id = $2" in query
        assert "customer_key = $3" in query

    assert "id = $4" in supersede_query
    assert supersede_values == [
        "reseller",
        "merchant",
        "customer",
        "memory-id",
    ]
    assert repoint_values[-2:] == ["customer", "customer_id"]


def test_alias_upsert_marks_conflict_without_overwriting_original_customer():
    query, values = upsert_alias_query(
        "reseller", "merchant", "+14155550100", "customer"
    )
    assert "DO UPDATE SET customer_id =" not in query
    assert "CONFLICTED" in query
    assert "conflicting_customer_id" in query
    assert values == ["reseller", "merchant", "+14155550100", "customer"]


def test_decoder_preserves_zero_confidence_and_structured_json():
    now = datetime.now(timezone.utc)
    memory = decode_user_memory(
        cast(
            Any,
            {
                "id": uuid4(),
                "reseller_id": "reseller",
                "merchant_id": "merchant",
                "customer_key": "customer",
                "key_type": "customer_id",
                "fact": "Prefers morning calls",
                "category": "preference",
                "structured": '{"window":"morning"}',
                "embedding": None,
                "source_channel": "voice",
                "confidence": 0,
                "operation_key": None,
                "expires_at": None,
                "superseded_at": None,
                "created_at": now,
                "updated_at": now,
            },
        )
    )
    assert memory is not None
    assert memory.confidence == 0
    assert memory.structured == {"window": "morning"}


def test_expiry_purge_is_bounded():
    query, values = purge_expired_memories_query(0)
    assert "expires_at <= now()" in query
    assert "LIMIT $1" in query
    assert values == [1]


def test_memory_migration_creates_one_complete_final_schema():
    root = Path(__file__).parents[2]
    migration_dir = root / "app/database/migrations"
    memory_migrations = sorted(migration_dir.glob("*memory*.sql"))
    assert [path.name for path in memory_migrations] == ["042_create_memory_tables.sql"]

    migration = memory_migrations[0].read_text()
    assert "halfvec(768)" in migration
    assert "operation_key" in migration
    assert "expires_at" in migration
    assert "CONFLICTED" in migration
    assert "halfvec_cosine_ops" in migration
    assert "CREATE EXTENSION" not in migration
    assert "ALTER TABLE" not in migration
    assert "DROP COLUMN" not in migration
