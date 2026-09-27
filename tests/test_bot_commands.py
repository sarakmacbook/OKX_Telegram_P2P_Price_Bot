"""Telegram's private command menu is installed for users and bot admins."""

import asyncio
from unittest.mock import AsyncMock

from telegram import BotCommandScopeAllPrivateChats, BotCommandScopeChat


def test_private_command_menu_shows_start_to_everyone_and_admin_tools_to_admins(bot):
    api = type("BotAPI", (), {})()
    api.set_my_commands = AsyncMock()

    asyncio.run(bot.configure_bot_commands(api))

    assert api.set_my_commands.await_count == 2
    public_call, admin_call = api.set_my_commands.await_args_list
    assert isinstance(public_call.kwargs["scope"], BotCommandScopeAllPrivateChats)
    assert [command.command for command in public_call.args[0]] == ["start"]
    assert isinstance(admin_call.kwargs["scope"], BotCommandScopeChat)
    assert admin_call.kwargs["scope"].chat_id == 424242
    assert {command.command for command in admin_call.args[0]} == {
        "start", "setgroup", "setchannel", "forwardfrom", "stopforward",
        "preview", "database", "cancel",
    }


def test_serverless_menu_includes_reconfigure_command(bot):
    api = type("BotAPI", (), {})()
    api.set_my_commands = AsyncMock()

    asyncio.run(bot.configure_bot_commands(api, include_setup=True))

    commands = api.set_my_commands.await_args_list[-1].args[0]
    assert any(command.command == "setup" for command in commands)


def test_command_menu_api_failure_does_not_stop_bot(bot, caplog):
    api = type("BotAPI", (), {})()
    api.set_my_commands = AsyncMock(side_effect=RuntimeError("temporary Telegram outage"))

    asyncio.run(bot.configure_bot_commands(api))

    assert "Could not publish" in caplog.text
