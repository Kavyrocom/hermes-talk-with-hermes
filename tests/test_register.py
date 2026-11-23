def test_register_exposes_talk_and_refuses_off_discord(monkeypatch):
    import talk_with_hermes
    from talk_with_hermes import discord_presence

    commands = {}
    started = []
    monkeypatch.setattr(discord_presence, "start_in_gateway", lambda: started.append(True))

    class Ctx:
        def register_command(self, name, handler, description="", args_hint="", **kwargs):
            commands[name] = handler

    talk_with_hermes.register(Ctx())
    assert "talk" in commands and started == [True]
    assert "salon vocal" in commands["talk"]("").lower()  # no Discord session here
