import lambda_function


def test_launch_without_a_token_sends_a_link_card(monkeypatch):
    def explode():
        raise AssertionError("the timetable should not load before stations are chosen")

    monkeypatch.setattr(lambda_function, "get_schedule", explode)
    response = lambda_function.handle({"request": {"type": "LaunchRequest"}, "context": {}})
    assert response["response"]["card"]["type"] == "LinkAccount"
    speech = response["response"]["outputSpeech"]["ssml"].lower()
    assert "station" in speech


def test_bad_token_also_sends_a_link_card(monkeypatch):
    monkeypatch.setattr(
        lambda_function,
        "get_schedule",
        lambda: (_ for _ in ()).throw(AssertionError("no")),
    )
    event = {
        "request": {"type": "LaunchRequest"},
        "context": {"System": {"user": {"accessToken": "not-a-token"}}},
    }
    response = lambda_function.handle(event)
    assert response["response"]["card"]["type"] == "LinkAccount"


def test_change_stations_and_help():
    change = lambda_function.handle(
        {
            "request": {
                "type": "IntentRequest",
                "intent": {"name": "ChangeStationsIntent"},
            }
        }
    )
    assert change["response"]["card"]["type"] == "LinkAccount"
    assert "disable" in change["response"]["outputSpeech"]["ssml"].lower()
    help_response = lambda_function.handle(
        {"request": {"type": "IntentRequest", "intent": {"name": "AMAZON.HelpIntent"}}}
    )
    assert "card" not in help_response["response"]
    speech = help_response["response"]["outputSpeech"]["ssml"]
    assert "departures" in speech
    assert "arrivals" in speech
    assert "Kuranowski" not in speech


def test_departures_and_arrivals_route(monkeypatch):
    seen = []

    def fake(event, kind="departure"):
        seen.append(kind)
        return {"version": "1.0", "response": {}}

    monkeypatch.setattr(lambda_function, "upcoming", fake)
    lambda_function.handle({"request": {"type": "LaunchRequest"}})
    lambda_function.handle(
        {"request": {"type": "IntentRequest", "intent": {"name": "DeparturesIntent"}}}
    )
    lambda_function.handle(
        {"request": {"type": "IntentRequest", "intent": {"name": "ArrivalsIntent"}}}
    )
    lambda_function.handle(
        {"request": {"type": "IntentRequest", "intent": {"name": "UpcomingTrainsIntent"}}}
    )
    assert seen == ["departure", "departure", "arrival", "departure"]
    fallback = lambda_function.handle(
        {"request": {"type": "IntentRequest", "intent": {"name": "AMAZON.FallbackIntent"}}}
    )
    text = fallback["response"]["outputSpeech"]["ssml"]
    assert "departures" in text
    assert "arrivals" in text
