"""The studio's field list, read as a merchant would meet it.

Two rules live here. Every question put to a merchant must be one a
shopkeeper can answer, with an example beside it — that is what took this
list from eighteen fields to twelve. And everything taken off the screen
must survive being off it: a hidden field is still in the prompt, and a save
that never mentions it must not blank it.
"""

from app.ai.voice.agents.breeze_buddy.assist.commerce.parse import fields_from_prompt
from app.ai.voice.agents.breeze_buddy.assist.commerce.slots import PROFILE
from app.ai.voice.agents.breeze_buddy.assist.commerce.vertical import vertical

# Everything the platform or the research already settled. On the screen
# these were questions a shopkeeper could not answer; off it they still
# reach the prompt.
KNOWN_NOT_ASKED = {
    "domain",
    "currency",
    "cart_url",
    "tagline",
    "hero_note",
    "compliance",
    "trusted_extra",
}


def _shown(section_key: str):
    section = next(s for s in PROFILE.sections if s.key == section_key)
    return [f for f in section.fields if not f.hidden]


def _merchant_sections():
    # `surface` is the widget's first screen, edited under Appearance.
    return [s for s in PROFILE.sections if s.key != "surface"]


class TestWhatAMerchantIsAsked:
    def test_every_question_carries_an_example_and_an_editor(self):
        for section in _merchant_sections():
            assert section.brief, f"{section.key} has no brief"
            for entry in section.fields:
                if entry.hidden:
                    continue
                assert entry.kind, f"{entry.key} does not say how to edit it"
                assert entry.example, f"{entry.key} has no example answer"
                if entry.kind == "list":
                    assert entry.many and entry.item, f"{entry.key} is a list of what?"

    def test_the_prompt_authors_fields_are_no_longer_questions(self):
        asked = {
            entry.key
            for section in PROFILE.sections
            for entry in section.fields
            if not entry.hidden
        }
        assert not (asked & KNOWN_NOT_ASKED)

    def test_they_are_hidden_rather_than_deleted(self):
        declared = {
            entry.key for section in PROFILE.sections for entry in section.fields
        }
        assert KNOWN_NOT_ASKED <= declared

    def test_the_shop_is_asked_twelve_things_across_four_sections(self):
        # A number worth failing on: this list grows one well-meant field at
        # a time, and the screen it produced is what this pass removed. The
        # deciding question counts once — its second and third instances are
        # the same question asked again, not two more questions.
        assert len(_merchant_sections()) == 4
        asked = sum(
            len([f for f in _shown(s.key) if f.index <= 1])
            for s in _merchant_sections()
        )
        assert asked == 12


class TestUpToThreeDecidingQuestions:
    """A shop can have more than one thing a shopper must settle.

    They live inside ONE ``###`` block, the second and third as ``####``
    sub-headings, because every part of the pipeline locates this section by
    the LAST ``### `` before the skeleton's end marker: three top-level
    headings would strand two of them outside the replaceable region, where
    the next save cannot find them and the fleet hash stops normalising them
    away.
    """

    def _three(self):
        return {
            "vertical_heading": ["Which weight and size?"],
            "vertical_body": ["True to chest.\nSize up for the 260gsm."],
            "vertical_heading_2": ["Will it survive a wash?"],
            "vertical_body_2": ["Cold gentle, dry flat."],
            "vertical_heading_3": ["Warm enough for the trek?"],
            "vertical_body_3": ["160gsm to 5C with a shell."],
        }

    def test_the_set_is_declared_as_a_group_the_console_can_repeat(self):
        # The console repeats whatever declares itself repeatable. It must
        # not learn that `vertical_heading_2` is the second of anything by
        # reading key names.
        section = next(s for s in PROFILE.sections if s.key == "vertical")
        assert {f.group for f in section.fields} == {"question"}
        assert sorted({f.index for f in section.fields}) == [1, 2, 3]
        shape: dict = {}
        for entry in section.fields:
            shape.setdefault(entry.index, []).append(entry.kind)
        assert list(shape.values()) == [["line", "text"]] * 3

    def test_only_the_first_is_a_top_level_heading(self):
        lines = (vertical.vertical_section(self._three()) or "").splitlines()
        assert len([line for line in lines if line.startswith("### ")]) == 1
        assert len([line for line in lines if line.startswith("#### ")]) == 2

    def test_all_three_survive_the_round_trip(self):
        prompt = (
            "## Brand identity\n\nb\n\n## Operating\n\n"
            + (vertical.vertical_section(self._three()) or "")
            + "\n### UI emission\nrules\n"
        )
        read_back = fields_from_prompt(prompt, {})
        for key, value in self._three().items():
            assert read_back[key] == value

    def test_one_question_writes_one_block_and_reads_back_alone(self):
        one = {"vertical_heading": ["Which size?"], "vertical_body": ["True to chest."]}
        written = vertical.vertical_section(one) or ""
        assert "####" not in written
        prompt = (
            f"## Brand identity\n\nb\n\n## Operating\n\n{written}\n### UI emission\nx\n"
        )
        read_back = fields_from_prompt(prompt, {})
        assert read_back["vertical_heading"] == ["Which size?"]
        assert "vertical_heading_2" not in read_back

    def test_a_question_with_no_answer_is_not_written(self):
        # Half a question in the prompt is a heading the assistant cannot
        # act on — it reads as an instruction and has nothing behind it.
        written = vertical.vertical_section(
            {
                "vertical_heading": ["Which size?"],
                "vertical_body": ["True to chest."],
                "vertical_heading_2": ["Will it wash?"],
            }
        )
        assert written is not None and "Will it wash?" not in written


class TestNothingTakenOffTheScreenIsLost:
    def _prompt(self, fields):
        brand = vertical.brand_block_from_fields(
            fields,
            assistant_name="Kosha",
            brand_name="Kosha",
            site_host="kosha.example",
        )
        section = vertical.vertical_section(fields) or ""
        return f"{brand}\n### {section}\n### UI emission\nrules\n"

    def test_a_save_that_never_mentions_them_keeps_them(self):
        built = {
            "brand_line": ["Kosha"],
            "tagline": ["Warm things, honestly made"],
            "domain": ["kosha.example"],
            "currency": ["INR (₹)"],
            "compliance": ["*(none — no vertical-specific guardrail required)*"],
            "hero_note": ["Names only — ask the catalogue for prices."],
            "hero_items": ["Merino Crew"],
        }
        prompt = self._prompt(built)

        # What the studio sends back: the four questions it showed, edited.
        live = dict(fields_from_prompt(prompt, {}))
        live.update({"brand_line": ["Kosha — merino since 2016"]})
        rebuilt = vertical.brand_block_from_fields(
            live, assistant_name="Kosha", brand_name="Kosha", site_host=""
        )

        assert "Kosha — merino since 2016" in rebuilt
        for kept in (
            "Warm things, honestly made",
            "kosha.example",
            "INR (₹)",
            "Names only — ask the catalogue for prices.",
            "no vertical-specific guardrail required",
        ):
            assert kept in rebuilt


class TestALinkTypedOnceIsTrusted:
    def test_an_address_in_an_escalation_line_becomes_a_button(self):
        values = vertical.widget_values(
            {"escalation_extra": ["Track your order — https://kosha.example/track."]}
        )
        assert values["render_ui"] == {
            "trusted_link_urls": ["https://kosha.example/track"]
        }

    def test_deleting_the_line_stops_trusting_the_address(self):
        # The address would otherwise come back as `trusted_extra` on the
        # next read — a hidden field nobody can see, holding a link the
        # merchant thought they had removed.
        configurations = {
            "render_ui": {"trusted_link_urls": ["https://kosha.example/track"]}
        }
        prompt = (
            "## Brand identity\n\n### Escalation channel\n\n"
            "- Track your order — https://kosha.example/track\n"
        )
        read_back = fields_from_prompt(prompt, configurations)
        assert "trusted_extra" not in read_back

        emptied = dict(read_back)
        emptied["escalation_extra"] = []
        assert vertical.widget_values(emptied)["render_ui"] == {"trusted_link_urls": []}
