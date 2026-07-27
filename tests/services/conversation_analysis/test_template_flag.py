from app.ai.voice.agents.breeze_buddy.template.types import ConfigurationModel


def test_topic_evaluation_requires_explicit_template_flag() -> None:
    assert "enable_topic_evaluation" not in ConfigurationModel().model_dump(
        exclude_none=True
    )
    assert ConfigurationModel(enable_topic_evaluation=True).enable_topic_evaluation
