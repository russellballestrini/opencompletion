"""
Unit tests for activity_utils.py v2.0 features

Tests cover:
- Template variable rendering ({{variable}})
- Condition evaluation (gte, lt, contains, regex, etc.)
- Content block filtering (conditional show_if)
- Conditional navigation (if/elif/else)
- Weighted random selection
- Progressive hints system
- Template context creation
"""

import pytest
import re
from activity_utils import (
    render_template,
    evaluate_condition,
    check_conditions,
    filter_content_blocks,
    resolve_conditional_navigation,
    select_weighted_random,
    get_progressive_hint,
    create_template_context,
)


class TestRenderTemplate:
    """Test template variable rendering with {{variable}} syntax"""

    def test_simple_variable(self):
        """Test simple variable substitution"""
        context = {"score": 100}
        result = render_template("Score: {{score}}", context)
        assert result == "Score: 100"

    def test_metadata_variable(self):
        """Test metadata.key syntax"""
        context = {"metadata": {"player_name": "Alice", "level": 5}}
        result = render_template(
            "Player: {{metadata.player_name}}, Level: {{metadata.level}}", context
        )
        assert result == "Player: Alice, Level: 5"

    def test_built_in_variables(self):
        """Test built-in variables (current_attempt, max_attempts, etc.)"""
        context = {
            "current_attempt": 2,
            "max_attempts": 3,
            "attempts_remaining": 1,
            "current_section": "intro",
            "current_step": "welcome",
            "username": "Bob",
        }
        result = render_template(
            "Attempt {{current_attempt}}/{{max_attempts}} ({{attempts_remaining}} left) - {{username}}",
            context,
        )
        assert result == "Attempt 2/3 (1 left) - Bob"

    def test_missing_variable(self):
        """Test that missing variables are preserved in output"""
        context = {"score": 100}
        result = render_template("Score: {{score}}, Level: {{level}}", context)
        assert result == "Score: 100, Level: {{level}}"

    def test_missing_metadata_key(self):
        """Test missing metadata key is preserved"""
        context = {"metadata": {"score": 50}}
        result = render_template("{{metadata.score}} - {{metadata.missing}}", context)
        assert result == "50 - {{metadata.missing}}"

    def test_non_string_values(self):
        """Test rendering non-string values"""
        context = {"score": 0, "active": True, "metadata": {"value": None}}
        result = render_template("{{score}} {{active}} {{metadata.value}}", context)
        assert result == "0 True "

    def test_no_variables(self):
        """Test text with no variables"""
        result = render_template("Plain text", {})
        assert result == "Plain text"

    def test_multiple_same_variable(self):
        """Test same variable used multiple times"""
        context = {"name": "Test"}
        result = render_template("{{name}} says {{name}}", context)
        assert result == "Test says Test"

    def test_non_string_input(self):
        """Test non-string input returns unchanged"""
        assert render_template(123, {}) == 123
        assert render_template(None, {}) is None


class TestEvaluateCondition:
    """Test single condition evaluation with various operators"""

    def test_equality(self):
        """Test simple equality check"""
        assert evaluate_condition({"level": 5}, "level", 5) is True
        assert evaluate_condition({"level": 5}, "level", 4) is False

    def test_not_equal(self):
        """Test not equal operator (_ne)"""
        assert evaluate_condition({"status": "active"}, "status_ne", "inactive") is True
        assert evaluate_condition({"status": "active"}, "status_ne", "active") is False

    def test_greater_than(self):
        """Test greater than operator (_gt)"""
        assert evaluate_condition({"score": 100}, "score_gt", 99) is True
        assert evaluate_condition({"score": 100}, "score_gt", 100) is False
        assert evaluate_condition({"score": 100}, "score_gt", 101) is False

    def test_greater_than_or_equal(self):
        """Test greater than or equal operator (_gte)"""
        assert evaluate_condition({"score": 100}, "score_gte", 99) is True
        assert evaluate_condition({"score": 100}, "score_gte", 100) is True
        assert evaluate_condition({"score": 100}, "score_gte", 101) is False

    def test_less_than(self):
        """Test less than operator (_lt)"""
        assert evaluate_condition({"score": 50}, "score_lt", 51) is True
        assert evaluate_condition({"score": 50}, "score_lt", 50) is False
        assert evaluate_condition({"score": 50}, "score_lt", 49) is False

    def test_less_than_or_equal(self):
        """Test less than or equal operator (_lte)"""
        assert evaluate_condition({"score": 50}, "score_lte", 51) is True
        assert evaluate_condition({"score": 50}, "score_lte", 50) is True
        assert evaluate_condition({"score": 50}, "score_lte", 49) is False

    def test_between(self):
        """Test between operator (_between)"""
        assert evaluate_condition({"level": 5}, "level_between", [1, 10]) is True
        assert evaluate_condition({"level": 5}, "level_between", [5, 5]) is True
        assert evaluate_condition({"level": 5}, "level_between", [1, 4]) is False
        assert evaluate_condition({"level": 5}, "level_between", [6, 10]) is False

    def test_contains(self):
        """Test contains operator (_contains) for comma-separated lists"""
        assert (
            evaluate_condition(
                {"inventory": "sword,shield,potion"}, "inventory_contains", "sword"
            )
            is True
        )
        assert (
            evaluate_condition(
                {"inventory": "sword,shield,potion"}, "inventory_contains", "axe"
            )
            is False
        )
        assert (
            evaluate_condition({"inventory": "sword"}, "inventory_contains", "sword")
            is True
        )
        assert (
            evaluate_condition({"inventory": ""}, "inventory_contains", "sword")
            is False
        )

    def test_not_contains(self):
        """Test not contains operator (_not_contains)"""
        assert (
            evaluate_condition(
                {"inventory": "sword,shield"}, "inventory_not_contains", "axe"
            )
            is True
        )
        assert (
            evaluate_condition(
                {"inventory": "sword,shield"}, "inventory_not_contains", "sword"
            )
            is False
        )

    def test_matches(self):
        """Test regex match operator (_matches)"""
        assert evaluate_condition({"name": "Alice"}, "name_matches", r"^[A-Z]") is True
        assert evaluate_condition({"name": "alice"}, "name_matches", r"^[A-Z]") is False
        assert (
            evaluate_condition(
                {"email": "test@example.com"}, "email_matches", r".*@.*\.com"
            )
            is True
        )

    def test_exists(self):
        """Test existence check operator (_exists)"""
        assert evaluate_condition({"has_key": True}, "has_key_exists", True) is True
        assert evaluate_condition({"has_key": True}, "has_key_exists", False) is False
        assert evaluate_condition({}, "missing_exists", True) is False
        assert evaluate_condition({}, "missing_exists", False) is True

    def test_not_exists(self):
        """Test non-existence check operator (_not_exists)"""
        assert evaluate_condition({}, "missing_not_exists", True) is True
        assert (
            evaluate_condition({"has_key": True}, "has_key_not_exists", True) is False
        )

    def test_invalid_number_comparison(self):
        """Test numeric comparison with non-numeric values"""
        assert evaluate_condition({"value": "text"}, "value_gt", 5) is False
        assert evaluate_condition({}, "missing_gte", 5) is False

    def test_invalid_between(self):
        """Test between with invalid format"""
        assert evaluate_condition({"value": 5}, "value_between", [1]) is False
        assert evaluate_condition({"value": 5}, "value_between", "invalid") is False

    def test_invalid_regex(self):
        """Test matches with invalid regex"""
        assert (
            evaluate_condition({"value": "test"}, "value_matches", "[invalid") is False
        )


class TestCheckConditions:
    """Test multiple condition evaluation (AND logic)"""

    def test_empty_conditions(self):
        """Test empty conditions returns True"""
        assert check_conditions({}, {}) is True

    def test_all_conditions_met(self):
        """Test all conditions must be met"""
        metadata = {"score": 100, "level": 5, "inventory": "sword,shield"}
        conditions = {"score_gte": 100, "level": 5, "inventory_contains": "sword"}
        assert check_conditions(metadata, conditions) is True

    def test_some_conditions_not_met(self):
        """Test fails if any condition not met"""
        metadata = {"score": 50, "level": 5}
        conditions = {"score_gte": 100, "level": 5}
        assert check_conditions(metadata, conditions) is False

    def test_mixed_operators(self):
        """Test mix of different operators"""
        metadata = {"score": 75, "status": "active", "name": "Alice"}
        conditions = {
            "score_gte": 50,
            "score_lt": 100,
            "status_ne": "inactive",
            "name_matches": r"^[A-Z]",
        }
        assert check_conditions(metadata, conditions) is True


class TestFilterContentBlocks:
    """Test conditional content block filtering"""

    def test_simple_strings(self):
        """Test that simple strings are always shown"""
        blocks = ["Always shown", "Another one"]
        context = {"metadata": {}}
        result = filter_content_blocks(blocks, {}, context)
        assert result == ["Always shown", "Another one"]

    def test_conditional_block_shown(self):
        """Test conditional block shown when condition met"""
        blocks = [{"text": "High score!", "show_if": {"score_gte": 50}}]
        metadata = {"score": 100}
        context = {"metadata": metadata}
        result = filter_content_blocks(blocks, metadata, context)
        assert result == ["High score!"]

    def test_conditional_block_hidden(self):
        """Test conditional block hidden when condition not met"""
        blocks = [{"text": "High score!", "show_if": {"score_gte": 50}}]
        metadata = {"score": 20}
        context = {"metadata": metadata}
        result = filter_content_blocks(blocks, metadata, context)
        assert result == []

    def test_mixed_blocks(self):
        """Test mix of simple strings and conditional blocks"""
        blocks = [
            "Always shown",
            {"text": "High score!", "show_if": {"score_gte": 50}},
            {"text": "Low score", "show_if": {"score_lt": 50}},
            "Also always shown",
        ]
        metadata = {"score": 75}
        context = {"metadata": metadata}
        result = filter_content_blocks(blocks, metadata, context)
        assert result == ["Always shown", "High score!", "Also always shown"]

    def test_template_rendering_in_blocks(self):
        """Test that templates are rendered in filtered blocks"""
        blocks = [
            "Score: {{metadata.score}}",
            {"text": "Level: {{metadata.level}}", "show_if": {"level_gte": 1}},
        ]
        metadata = {"score": 100, "level": 5}
        context = {"metadata": metadata}
        result = filter_content_blocks(blocks, metadata, context)
        assert result == ["Score: 100", "Level: 5"]

    def test_empty_blocks(self):
        """Test empty block list"""
        result = filter_content_blocks([], {}, {})
        assert result == []


class TestResolveConditionalNavigation:
    """Test if/elif/else conditional navigation resolution"""

    def test_simple_string(self):
        """Test simple string navigation (pass-through)"""
        result = resolve_conditional_navigation("section:step", {})
        assert result == "section:step"

    def test_if_branch_matches(self):
        """Test if branch when condition matches"""
        nav = [
            {"if": {"score_gte": 100}, "goto": "expert:challenge"},
            {"else": {}, "goto": "beginner:tutorial"},
        ]
        metadata = {"score": 150}
        result = resolve_conditional_navigation(nav, metadata)
        assert result == "expert:challenge"

    def test_elif_branch_matches(self):
        """Test elif branch when if fails but elif matches"""
        nav = [
            {"if": {"score_gte": 100}, "goto": "expert:challenge"},
            {"elif": {"score_gte": 50}, "goto": "intermediate:lesson"},
            {"else": {}, "goto": "beginner:tutorial"},
        ]
        metadata = {"score": 75}
        result = resolve_conditional_navigation(nav, metadata)
        assert result == "intermediate:lesson"

    def test_else_branch(self):
        """Test else branch when all conditions fail"""
        nav = [
            {"if": {"score_gte": 100}, "goto": "expert:challenge"},
            {"elif": {"score_gte": 50}, "goto": "intermediate:lesson"},
            {"else": {}, "goto": "beginner:tutorial"},
        ]
        metadata = {"score": 20}
        result = resolve_conditional_navigation(nav, metadata)
        assert result == "beginner:tutorial"

    def test_no_match_no_else(self):
        """Test returns None when no conditions match and no else"""
        nav = [
            {"if": {"score_gte": 100}, "goto": "expert:challenge"},
            {"elif": {"score_gte": 50}, "goto": "intermediate:lesson"},
        ]
        metadata = {"score": 20}
        result = resolve_conditional_navigation(nav, metadata)
        assert result is None

    def test_multiple_conditions_in_branch(self):
        """Test branch with multiple conditions (AND logic)"""
        nav = [
            {"if": {"score_gte": 100, "level_gte": 10}, "goto": "expert:challenge"},
            {"else": {}, "goto": "beginner:tutorial"},
        ]
        metadata = {"score": 100, "level": 10}
        result = resolve_conditional_navigation(nav, metadata)
        assert result == "expert:challenge"

    def test_first_matching_branch_wins(self):
        """Test that first matching branch is used"""
        nav = [
            {"if": {"score_gte": 50}, "goto": "first:path"},
            {"elif": {"score_gte": 50}, "goto": "second:path"},
        ]
        metadata = {"score": 75}
        result = resolve_conditional_navigation(nav, metadata)
        assert result == "first:path"


class TestSelectWeightedRandom:
    """Test weighted random selection"""

    def test_weighted_selection(self):
        """Test basic weighted selection (statistical test)"""
        options = [
            {"value": "common", "weight": 70},
            {"value": "rare", "weight": 25},
            {"value": "legendary", "weight": 5},
        ]

        # Run multiple times and check distribution is roughly correct
        results = [select_weighted_random(options) for _ in range(1000)]
        common_count = results.count("common")
        rare_count = results.count("rare")
        legendary_count = results.count("legendary")

        # Allow 10% variance from expected distribution
        assert 600 < common_count < 800  # Expected ~700
        assert 150 < rare_count < 350  # Expected ~250
        assert 0 < legendary_count < 100  # Expected ~50

    def test_single_option(self):
        """Test selection with single option"""
        options = [{"value": "only_choice", "weight": 100}]
        result = select_weighted_random(options)
        assert result == "only_choice"

    def test_equal_weights(self):
        """Test equal weights distribution"""
        options = [
            {"value": "a", "weight": 1},
            {"value": "b", "weight": 1},
            {"value": "c", "weight": 1},
        ]
        results = [select_weighted_random(options) for _ in range(300)]
        # Each should appear roughly 100 times (allow variance)
        assert 50 < results.count("a") < 150
        assert 50 < results.count("b") < 150
        assert 50 < results.count("c") < 150

    def test_empty_list(self):
        """Test empty options list"""
        result = select_weighted_random([])
        assert result is None

    def test_missing_weight(self):
        """Test option with missing weight defaults to 1"""
        options = [{"value": "a", "weight": 10}, {"value": "b"}]  # No weight
        # Should not crash
        result = select_weighted_random(options)
        assert result in ["a", "b"]


class TestGetProgressiveHint:
    """Test progressive hints retrieval"""

    def test_exact_attempt_match(self):
        """Test hint for exact attempt number"""
        hints = [
            {"attempt": 1, "text": "First hint", "counts_as_attempt": False},
            {"attempt": 2, "text": "Second hint", "counts_as_attempt": False},
            {"attempt": 3, "text": "Third hint", "counts_as_attempt": False},
        ]
        context = {}
        result = get_progressive_hint(hints, 2, context)
        assert result == {"text": "Second hint", "counts_as_attempt": False}

    def test_no_hint_for_attempt(self):
        """Test returns None when no hint for attempt"""
        hints = [{"attempt": 1, "text": "First hint", "counts_as_attempt": False}]
        result = get_progressive_hint(hints, 2, {})
        assert result is None

    def test_empty_hints_list(self):
        """Test empty hints list returns None"""
        result = get_progressive_hint([], 1, {})
        assert result is None

    def test_template_rendering_in_hint(self):
        """Test that templates are rendered in hint text"""
        hints = [
            {
                "attempt": 1,
                "text": "Attempt {{current_attempt}} of {{max_attempts}}",
                "counts_as_attempt": False,
            }
        ]
        context = {"current_attempt": 1, "max_attempts": 3}
        result = get_progressive_hint(hints, 1, context)
        assert result["text"] == "Attempt 1 of 3"

    def test_counts_as_attempt_field(self):
        """Test counts_as_attempt field is preserved"""
        hints = [{"attempt": 1, "text": "Hint", "counts_as_attempt": True}]
        result = get_progressive_hint(hints, 1, {})
        assert result["counts_as_attempt"] is True

    def test_missing_counts_as_attempt(self):
        """Test missing counts_as_attempt defaults to False"""
        hints = [{"attempt": 1, "text": "Hint"}]
        result = get_progressive_hint(hints, 1, {})
        assert result["counts_as_attempt"] is False


class TestCreateTemplateContext:
    """Test template context creation"""

    def test_all_fields_present(self):
        """Test all fields are in context"""
        metadata = {"score": 100, "level": 5}
        context = create_template_context(
            metadata=metadata,
            current_attempt=2,
            max_attempts=3,
            current_section="intro",
            current_step="welcome",
            username="Alice",
        )

        assert context["metadata"] == metadata
        assert context["current_attempt"] == 2
        assert context["max_attempts"] == 3
        assert context["attempts_remaining"] == 1
        assert context["current_section"] == "intro"
        assert context["current_step"] == "welcome"
        assert context["username"] == "Alice"

    def test_attempts_remaining_calculation(self):
        """Test attempts_remaining is calculated correctly"""
        context = create_template_context(
            metadata={},
            current_attempt=1,
            max_attempts=3,
            current_section="s",
            current_step="st",
            username="User",
        )
        assert context["attempts_remaining"] == 2

    def test_attempts_remaining_zero(self):
        """Test attempts_remaining doesn't go negative"""
        context = create_template_context(
            metadata={},
            current_attempt=5,
            max_attempts=3,
            current_section="s",
            current_step="st",
            username="User",
        )
        assert context["attempts_remaining"] == 0

    def test_default_username(self):
        """Test username defaults"""
        context = create_template_context(
            metadata={},
            current_attempt=1,
            max_attempts=3,
            current_section="s",
            current_step="st",
        )
        assert context["username"] == "User"


class TestIntegration:
    """Integration tests combining multiple features"""

    def test_template_and_conditions_together(self):
        """Test templates work with conditions in content blocks"""
        blocks = [
            {
                "text": "Welcome {{metadata.player_name}}!",
                "show_if": {"player_name_exists": True},
            },
            {"text": "Score: {{metadata.score}}", "show_if": {"score_gte": 0}},
        ]
        metadata = {"player_name": "Alice", "score": 50}
        context = create_template_context(
            metadata=metadata,
            current_attempt=1,
            max_attempts=3,
            current_section="intro",
            current_step="welcome",
            username="Alice",
        )

        # Add exists condition to metadata for testing
        metadata["player_name_exists"] = True

        result = filter_content_blocks(blocks, metadata, context)
        assert "Welcome Alice!" in result
        assert "Score: 50" in result

    def test_conditional_nav_with_complex_conditions(self):
        """Test conditional navigation with multiple conditions"""
        nav = [
            {
                "if": {"score_gte": 100, "level_gte": 10, "inventory_contains": "key"},
                "goto": "secret:room",
            },
            {"elif": {"score_gte": 50}, "goto": "intermediate:level"},
            {"else": {}, "goto": "beginner:start"},
        ]

        # Test first branch
        metadata1 = {"score": 100, "level": 10, "inventory": "sword,key,shield"}
        assert resolve_conditional_navigation(nav, metadata1) == "secret:room"

        # Test second branch
        metadata2 = {"score": 75, "level": 5, "inventory": "sword"}
        assert resolve_conditional_navigation(nav, metadata2) == "intermediate:level"

        # Test else branch
        metadata3 = {"score": 20, "level": 1, "inventory": ""}
        assert resolve_conditional_navigation(nav, metadata3) == "beginner:start"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
