import json

import pytest
from pydantic import ValidationError

from app.models.schemas import Clip, ClipPlan
from app.services.llm import MuseClient, _extract_json


def test_clip_duration_validation_pass():
    c = Clip(start_time=0, end_time=30, hook_text="hook", virality_score=90)
    assert c.end_time == 30


def test_clip_duration_validation_fail_short():
    with pytest.raises(ValidationError):
        Clip(start_time=0, end_time=14, hook_text="hook", virality_score=90)


def test_clip_duration_validation_fail_long():
    with pytest.raises(ValidationError):
        Clip(start_time=0, end_time=91, hook_text="hook", virality_score=90)


def test_clipplan_valid():
    plan = ClipPlan.model_validate(
        {
            "clips": [{"start_time": 0, "end_time": 20, "hook_text": "hook", "virality_score": 90}],
            "dead_air": [{"start": 5, "end": 5.6}],
            "broll_cues": [{"timestamp": 10, "keywords_en": "burning money", "fallback_en": "office"}],
        }
    )
    assert len(plan.clips) == 1


def test_extract_json_with_fence():
    raw = "```json\n{\"clips\": [{\"start_time\": 0, \"end_time\": 20, \"hook_text\": \"hi\", \"virality_score\": 90}], \"dead_air\": [], \"broll_cues\": []}\n```"
    cleaned = _extract_json(raw)
    data = json.loads(cleaned)
    assert "clips" in data


def test_muse_mock_plan_duration():
    client = MuseClient(api_key="your_muse_spark_key")
    from app.models.schemas import Transcript, Word

    t = Transcript(text="hello world", words=[Word(word="hello", start=0, end=0.5)], duration=40)
    plan = client.analyze(t, duration=40)
    assert len(plan.clips) >= 1
    for c in plan.clips:
        assert 15 <= c.end_time - c.start_time <= 90


def test_muse_mock_short_duration():
    client = MuseClient(api_key="your_muse_spark_key")
    from app.models.schemas import Transcript

    t = Transcript(text="hi", words=[], duration=5)
    plan = client.analyze(t, duration=5)
    # Even short input should produce valid 15s clip due to mock padding
    assert plan.clips[0].end_time - plan.clips[0].start_time >= 15
