import configparser

from generate_website import generate_html, get_randomline_commands


def _build_config() -> configparser.ConfigParser:
    config = configparser.ConfigParser()
    config.add_section("Bot")
    config.set("Bot", "command_prefix", "!")
    config.add_section("RandomLine")
    config.set("RandomLine", "prefix.default", "")
    return config


def test_get_randomline_commands_defaults_to_fun_category():
    config = _build_config()
    config.set("RandomLine", "triggers.momjoke", "momjoke,mom joke")
    config.set("RandomLine", "file.momjoke", "data/randomlines/momjokes.txt")

    randomline_commands = get_randomline_commands(config)
    command = randomline_commands["randomline.momjoke"]

    assert command.name == "momjoke"
    assert command.category == "fun"
    assert command.keywords == ["momjoke", "mom joke"]
    assert command.get_usage_info()["usage"] == "!momjoke"


def test_get_randomline_commands_applies_category_override():
    config = _build_config()
    config.set("RandomLine", "triggers.funfact", "funfact,fun fact")
    config.set("RandomLine", "file.funfact", "data/randomlines/funfacts.txt")
    config.set("RandomLine", "category.funfact", "Games And Entertainment")

    randomline_commands = get_randomline_commands(config)
    command = randomline_commands["randomline.funfact"]

    assert command.category == "games_and_entertainment"


def test_generate_html_includes_randomline_commands_in_fun_section():
    config = _build_config()
    config.set("RandomLine", "triggers.momjoke", "momjoke,mom joke")
    config.set("RandomLine", "file.momjoke", "data/randomlines/momjokes.txt")

    randomline_commands = get_randomline_commands(config)
    html_content = generate_html(
        bot_name="TestBot",
        title="TestBot - Command Reference",
        introduction="Intro",
        commands=list(randomline_commands.items()),
        monitor_channels=[],
        channels_data={},
        style="default",
    )

    assert "Fun Commands" in html_content
    assert "momjoke" in html_content
